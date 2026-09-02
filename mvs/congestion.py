import logging
import threading
import time

log = logging.getLogger("browser_screencast")


# ---------------------------------------------------------------------------
# AdaptiveController — per-client fps + bitrate management
# ---------------------------------------------------------------------------
class AdaptiveController:

    # --- ramp measurement constants -----------------------------------------
    # Window over which _recent_bps() measures what we actually sent.
    _BPS_WINDOW = 1.0
    # Floor on the measurement's denominator, plus a minimum sample count (see
    # _recent_bps). Together they stop one or two fat keyframes at the start of
    # a burst from reading as many Mbps of proven link capacity; the per-tick
    # growth cap below absorbs whatever distortion is left.
    _BPS_MIN_ELAPSED = 0.25
    _BPS_MIN_SAMPLES = 3
    # How far above the measured send rate the encoder target may be pushed.
    # This is the slow-start assumption: "the link carried X while the client
    # reported clear, so 2X is a reasonable next probe". It is the ONLY thing
    # that lets the target grow — an idle screen measures ~0 and therefore
    # cannot grow the target at all, no matter how many ticks elapse.
    _PROBE_GROWTH = 2.0
    # Hardest single-tick growth. Ticks are >=100ms, so this still allows
    # ~300kbps -> ~2.4Mbps in three ticks (~0.3s) when the measurement agrees.
    _MAX_TICK_GROWTH = 2.0

    def __init__(self, cfg):
        self.fps = float(cfg.max_fps)
        self.max_fps = float(cfg.max_fps)
        self.bitrate = max(300_000, int(getattr(cfg, "initial_bitrate", 1_000_000)))
        self.jpeg_quality = 85
        self.client_w = 1920
        self.client_h = 1080
        self.cap_h   = 0    # 0 = auto (use canvas physical size); >0 = explicit height cap
        self.fps_cap = 0    # 0 = use max_fps; >0 = explicit fps ceiling
        self.canvas_phys_w = 0
        self.canvas_phys_h = 0
        self._min_br = 300_000
        self._min_fps = 5.0          # fps floor — only reduced after bitrate hits minimum
        self._max_br = 50_000_000   # 50Mbps cap — plenty for any screenshare quality
        self.user_bw_cap = 0        # hard send-level cap in bits/sec; 0 = unlimited
        self.lag_budget_override = 0  # user-specified lag budget in ms; 0 = auto (50ms floor)
        # Congestion ceiling: bitrate at the moment the last backoff fired.
        # 0 = not yet measured — on_fresh probes slowly until first congestion event.
        self._ceil_bitrate = 0
        self._conn_time = time.monotonic()  # suppress ceiling recording during initial keyframe burst
        self._last_slow = 0.0
        self._last_fast = 0.0
        self._drain_until = 0.0     # monotonic deadline: stop sending until this time
        self._last_clear_t = 0.0   # monotonic time of last client "clear" (low-lag) confirmation
        self._lock = threading.Lock()
        self._ping_smooth = 0.0     # EWA-smoothed video ping RTT (jitter suppression)
        self._ping_history = []     # last 4 smoothed samples for gradient computation
        self._metric_rtt = 0.0      # EWA of unloaded metric-channel RTT; 0 = not measured yet
        # Rolling throughput tracker: (timestamp, bytes) ring for the last 3s.
        # Used to gate on_lag() — congestion can only exist when actual wire
        # throughput >= _min_br (300kbps).  Below that the encoder/CPU is the
        # bottleneck, not the network; backoff would only make things worse.
        self._sent_ring: list = []  # [(monotonic, bytes), ...]

    def report_sent(self, nbytes: int) -> None:
        """Record bytes written to the WebSocket. Called after every frame send.
        Feeds the rolling throughput window used to gate on_lag()."""
        now = time.monotonic()
        self._sent_ring.append((now, nbytes))
        # Keep only the last 3 seconds
        cutoff = now - 3.0
        while self._sent_ring and self._sent_ring[0][0] < cutoff:
            self._sent_ring.pop(0)

    def _rolling_bps(self) -> float:
        """Actual wire throughput (bps) over the last 3 s. 0 if no data yet."""
        if len(self._sent_ring) < 2:
            return 0.0
        elapsed = self._sent_ring[-1][0] - self._sent_ring[0][0]
        if elapsed < 0.1:
            return 0.0
        total = sum(b for _, b in self._sent_ring)
        return total * 8 / elapsed

    def _recent_bps(self, window: float = 1.0) -> float:
        """Bits/s actually handed to the socket over the last `window` seconds.

        This is the ramp's measurement of "how much did we really push through
        the link recently".  Two deliberate choices:

        * The denominator is the time since the FIRST send inside the window,
          not the whole window.  A burst that starts right after an idle period
          is then measured on its own timescale instead of being diluted by the
          preceding silence — which is what makes an idle→active ramp fast.
        * That elapsed value is floored at `_BPS_MIN_ELAPSED` and at least
          `_BPS_MIN_SAMPLES` frames are required, so one or two fat keyframes
          cannot read as a huge instantaneous rate.

        `now` (not the last sample time) is the window end, so the number decays
        on its own as soon as sending stops — an idle screen reads ~0 without
        any explicit static/active state.
        """
        now = time.monotonic()
        cutoff = now - window
        sel = [(t, b) for t, b in self._sent_ring if t >= cutoff]
        if len(sel) < self._BPS_MIN_SAMPLES:
            return 0.0
        elapsed = max(now - sel[0][0], self._BPS_MIN_ELAPSED)
        return sum(b for _, b in sel) * 8 / elapsed

    @property
    def draining(self):
        """True while a transmit pause is active (waiting for downstream buffers to drain)."""
        return time.monotonic() < self._drain_until

    def end_drain_if_clear(self, write_buf):
        """Allow the sender to short-circuit the drain pause once the local
        write buffer has actually cleared. The drain pause was sized for the
        worst-case queue; if the link drained faster than estimated we don't
        need to wait the full window."""
        if write_buf < self.lag_wb_budget() // 2:
            with self._lock:
                if self._drain_until > time.monotonic():
                    self._drain_until = 0.0

    @property
    def frame_interval(self):
        return 1.0 / max(1.0, self.fps)

    def on_resolution(self, w, h):
        with self._lock:
            self.client_w = max(1, w)
            self.client_h = max(1, h)
            # w/h are physical canvas pixels (canvas.width × canvas.height after DPR scaling)
            self.canvas_phys_w = max(1, w)
            self.canvas_phys_h = max(1, h)

    def on_quality(self, cap_h: int, fps_cap: int, max_kbps: int = 0, lag_ms: int = 0):
        with self._lock:
            self.cap_h   = max(0, cap_h)
            self.fps_cap = max(0, fps_cap)
            ceil = float(self.fps_cap) if self.fps_cap > 0 else self.max_fps
            self.fps = min(self.fps, ceil)
            if max_kbps > 0:
                # Realtime encoders often overshoot the bit_rate target on
                # complex content. To make "Max BW = 2 Mbps" match what the
                # user actually sees on the wire, target the encoder at 0.65x
                # the user value so typical overshoot lands near the setting.
                # This is a soft cap; for a strict cap we'd need a software
                # encoder (libx264/libx265 with VBV).
                _VBR_HEADROOM_FACTOR = 0.65
                self.user_bw_cap = max_kbps * 1000   # for explicit-drop path (JPEG only)
                self._max_br = max(self._min_br, int(max_kbps * 1000 * _VBR_HEADROOM_FACTOR))
                self.bitrate = max(self._min_br, min(self.bitrate, self._max_br))
            else:
                self._max_br = 50_000_000
                self.user_bw_cap = 0
            self.lag_budget_override = max(0, lag_ms)

    def effective_target(self, native_w: int, native_h: int):
        """Return (tw, th) — the target encode resolution.
        Never upscales; always preserves the source aspect ratio; dimensions are even."""
        with self._lock:
            if self.cap_h > 0:
                th = min(self.cap_h, native_h)
            else:
                # Auto: encode at native resolution. The browser scales the decoded
                # frame to fit the canvas — no quality is lost when zooming in or
                # going full-screen, and the explicit cap_h presets still work.
                th = native_h
            tw = round(native_w * th / native_h) if native_h else native_w
            return (tw & ~1), (th & ~1)

    def _backoff(self, severe):
        """Reduce quality. Must be called with _lock held; enforces 300ms debounce.

        Priority: cut bitrate (quality) first — preserves fps (input responsiveness).
        fps is only reduced as a last resort when bitrate is already at the floor.
        fps floor is derived from lag_budget_ms so it never goes below one frame per
        budget period — e.g. at 50ms budget, min fps = 20 (not 5).

        _last_slow is ONLY updated when something actually changes. When both bitrate
        and fps are already at their floors, calling _backoff is a no-op and must not
        poison the settle timer — otherwise on_fresh() can never ramp after congestion
        clears while the controller is pinned at the floor."""
        now = time.monotonic()
        if now - self._last_slow < 0.3:
            return
        factor = 0.5 if severe else 0.75
        # fps floor: never slower than one frame per budget window
        min_fps = max(self._min_fps, 1000.0 / self.lag_budget_ms())
        changed = False
        if self.bitrate > self._min_br:
            # Save congestion point before reducing — this is the network ceiling (SSTHRESH).
            # On recovery, ramp fast back to here, probe slowly above.
            # Skip recording the ceiling during the first 5s: initial-connect keyframe bursts
            # always trigger a lag spike but don't reflect the steady-state link capacity.
            # Without this gate, the first keyframe sets a low ceiling (e.g. 750k) and the
            # frame_bytes probe guard then keeps the controller pinned there indefinitely.
            if time.monotonic() - self._conn_time > 5.0:
                self._ceil_bitrate = self.bitrate
            self.bitrate = max(self._min_br, int(self.bitrate * factor))
            self.jpeg_quality = max(10, int(self.jpeg_quality * factor))
            changed = True
        elif self.fps > min_fps:
            self.fps = max(min_fps, self.fps * factor)
            changed = True
        if changed:
            self._last_slow = now
            self._last_fast = 0.0
        log.info("backoff: fps=%.1f br=%dk ceil=%dk severe=%s min_fps=%.1f changed=%s",
                 self.fps, self.bitrate // 1000, self._ceil_bitrate // 1000, severe, min_fps, changed)

    def lag_budget_ms(self):
        """Allowed in-flight delay before congestion backoff fires.

        Auto: 1 frame interval (floors at 50ms at high fps, caps at 500ms).
        Override: user-specified value — higher = smoother video, more input latency.
        """
        if self.lag_budget_override > 0:
            return float(self.lag_budget_override)
        return max(50.0, min(1000.0 / max(1.0, self.fps), 500.0))

    def lag_wb_budget(self):
        """Write-buffer byte equivalent of lag_budget_ms at current bitrate.
        Scales with lag_budget_ms so a higher lag budget also tolerates a larger
        TCP send buffer before triggering backoff.

        Floor is 8× average frame size. H.264 VBR scene-change frames routinely
        hit 8-16× the average P-frame size (window open/close, rapid motion).
        The old 2× floor caused these normal VBR bursts to register as severe
        congestion, immediately halving the bitrate and ratcheting the ceiling down
        every time the screen changed — quality got WORSE with more motion.
        8× tolerates a typical burst without backoff; the RTT-budget term still
        fires when the buffer stays elevated across multiple frames (real congestion)."""
        avg_frame = int(self.bitrate / max(1.0, self.fps) / 8)  # bytes per average frame
        return max(16 * avg_frame, 64 * 1024, int(self.lag_budget_ms() * self.bitrate / 8000))

    def on_lag(self, age_ms, write_buf=0):
        # Throughput guard: if actual wire throughput is below the bitrate floor
        # the encoder/CPU is the bottleneck, not the network.  Backoff in this
        # state would lower an already-floored bitrate target with no effect on
        # the real constraint (encoder speed), and prevents recovery when the
        # encoder catches up.  Only skip when we have enough history (>= 1s).
        if len(self._sent_ring) >= 2:
            window = self._sent_ring[-1][0] - self._sent_ring[0][0]
            if window >= 1.0 and self._rolling_bps() < self._min_br * 0.9:
                log.debug("on_lag skipped: throughput %.0f bps < floor %d bps",
                          self._rolling_bps(), self._min_br)
                return

        budget = self.lag_budget_ms()
        # write_buf == 0: the local TCP socket was empty when the frame was queued —
        # no backpressure from the link.  In this state age_ms reflects frame
        # transmission time + base network latency, not sustained congestion.
        # Hardware VBR encoders (NVENC) produce I-frames 5–50× the average P-frame
        # size; these push age > budget on high-RTT links with no real congestion.
        # The ping-gradient signal handles sustained RTT rises; write_buf handles
        # actual TCP backpressure.  Suppress age-only backoff when wb == 0.
        if write_buf == 0:
            # age > budget*3: queue is sitting in the wifi/path layer, not visible via wb.
            # Soft backoff to reduce I-frame sizes rather than silently absorbing the spike.
            if age_ms > budget * 3:
                log.info("on_lag: age=%.0fms wb=0 budget=%.0fms — path-layer queue, soft backoff br=%dk",
                         age_ms, budget, self.bitrate // 1000)
                with self._lock:
                    self._backoff(False)
            elif age_ms > 0 and age_ms < budget:
                with self._lock:
                    self._last_clear_t = time.monotonic()
            else:
                log.debug("on_lag: age=%.0fms wb=0 budget=%.0fms — I-frame spike / base latency, no backoff br=%dk",
                          age_ms, budget, self.bitrate // 1000)
            return
        if age_ms > 0 and age_ms < budget and write_buf < self.lag_wb_budget():
            return
        if age_ms == 0 and write_buf < self.lag_wb_budget():
            return
        severe = age_ms > budget * 3 or write_buf > self.lag_wb_budget() * 6
        log.info("on_lag: age=%.0fms wb=%dB budget=%.0fms wb_budget=%dB severe=%s br=%dk",
                 age_ms, write_buf, budget, self.lag_wb_budget(), severe, self.bitrate // 1000)
        with self._lock:
            self._backoff(severe)
            # Transmit pause: when severe lag is reported (either via browser
            # age_ms or via local wb), the queue is sitting in some downstream
            # buffer. Halving bitrate helps long-term but doesn't drain the
            # existing queue fast. Pause sending so it can drain before we
            # resume — screen freezes briefly but recovers clean.
            #
            # Trigger on EITHER age (browser-side queue) OR wb (server-side
            # TCP backpressure). The wb-only path was previously gated by
            # age_ms > 300, which never fires when the lag signal is wb=0
            # because TCP backpressure reached us before the lag report did.
            wb_severe = write_buf > self.lag_wb_budget() * 12
            if (age_ms > 300 or wb_severe) and severe and not self.draining:
                # Pause length: take the LARGER of age-based and wb-based
                # drain estimates. wb at 2Mbps with 4MB queued = 16s of
                # buffered video — old 2s cap couldn't clear deep queues
                # and drains chained. 5s cap with realistic estimate per
                # signal closes the loop.
                age_pause = (age_ms - budget) / 1000.0
                wb_pause  = (write_buf * 8) / max(self.bitrate, 1)
                pause_s = min(5.0, max(age_pause, wb_pause))
                self._drain_until = time.monotonic() + pause_s
                log.debug("drain pause: %.0fms (age=%.0fms wb=%dKB budget=%.0fms)",
                          pause_s * 1000, age_ms, write_buf // 1024, budget)

    def on_ping_rtt(self, rtt_ms):
        """Two-signal congestion detection via video-channel RTT.

        Signal 1 — gradient (primary): RTT rising means a buffer is FORMING right now.
        Fires early, before the queue is large, and requires no baseline or metric channel.
        Link-agnostic: RTT going up is RTT going up regardless of absolute value.

        Signal 2 — delta vs metric (secondary): RTT stable but elevated above the unloaded
        metric channel means a STATIC buffer exists. This catches the case where the gradient
        already fired and settled, or where we joined mid-congestion. A static buffer is an
        unstable equilibrium; slight backoff drains it quickly.

        BOTH signals are disabled in buffer mode (lag_budget_override > 0): the user has
        deliberately accepted queue formation up to their chosen buffer size, and gradient
        backoff would actively fight that intent. The lag-report path with the user's
        budget is the only backoff signal in buffer mode."""
        if self.lag_budget_override > 0:
            return
        with self._lock:
            # Smooth to suppress per-sample jitter before computing gradient
            self._ping_smooth = (self._ping_smooth * 0.6 + rtt_ms * 0.4
                                 if self._ping_smooth > 0 else rtt_ms)
            s = self._ping_smooth
            self._ping_history.append(s)
            if len(self._ping_history) > 4:
                self._ping_history.pop(0)

            # Signal 1: gradient — buffer FORMING
            gradient_fired = False
            if len(self._ping_history) >= 3:
                prev_mean = sum(self._ping_history[:-1]) / len(self._ping_history[:-1])
                gradient = s - prev_mean
                if gradient > 15:       # rising >15ms per 2s sample = queue building
                    self._backoff(gradient > 40)
                    gradient_fired = True
                    log.info("ping gradient=%.1fms rtt=%.1fms br=%dk", gradient, s, self.bitrate // 1000)

            # Signal 2: delta — buffer STATIC (only when gradient hasn't already fired)
            # Threshold scales with lag budget: at 50ms budget fire at 50ms delta;
            # at 200ms budget fire at 200ms delta (user explicitly allows that much queuing).
            if not gradient_fired and self._metric_rtt > 0:
                delta = s - self._metric_rtt
                budget = self.lag_budget_ms()
                if delta > budget:
                    self._backoff(delta > budget * 2)
                    log.info("ping delta=%.1fms rtt=%.1fms metric=%.1fms budget=%.0fms br=%dk",
                             delta, s, self._metric_rtt, budget, self.bitrate // 1000)

    def on_client_clear(self):
        """Client lag report confirmed path is clear — allow next ramp step promptly.

        Called when browser reports age_ms < budget: not a backoff signal but a positive
        'path is clear' confirmation. Used to unlock early ramp steps instead of waiting
        the full 2s heuristic interval."""
        with self._lock:
            self._last_clear_t = time.monotonic()

    def on_metric_rtt(self, rtt_ms):
        """RTT on the unloaded metric channel — pure link latency, no video queuing.
        Fast EWA (0.7/0.3) so link changes from WiFi↔5G roaming are reflected in ~4s."""
        with self._lock:
            if self._metric_rtt == 0.0:
                self._metric_rtt = rtt_ms
            else:
                self._metric_rtt = self._metric_rtt * 0.7 + rtt_ms * 0.3

    def on_fresh(self, frame_bytes: int = 0):
        """One ramp tick, called by the sender after every frame.

        `frame_bytes` is accepted for call-site compatibility but no longer
        gates anything: the ramp is now clocked by measured throughput
        (`_recent_bps`), which subsumes it. See the comment in the bitrate
        branch below for why the byte gate was both too weak and too strong.
        """
        with self._lock:
            now = time.monotonic()
            # Minimum tick: 100ms — lag reports are throttled to 10/s; no point checking faster.
            if now - self._last_fast < 0.1:
                return
            # Post-congestion settle: require 2s quiet after BOTH last backoff and drain end.
            # _last_slow is set at backoff START, not drain end — without the drain_until term
            # we'd ramp 0.65s after a 1.35s drain, immediately re-filling the cleared buffer.
            settle_until = max(self._last_slow + 2.0, self._drain_until + 2.0)
            if now < settle_until:
                log.debug("fresh blocked: settle in %.1fs (slow=%.1f drain=%.1f)",
                          settle_until - now, self._last_slow, self._drain_until)
                return
            # Step interval: short when client actively confirms "clear" via lag reports,
            # long when flying blind (no metric_rtt or no recent clear signal).
            #
            # clear_window = max(500ms, 2×RTT): how recently a clear report must have arrived.
            # On a 20ms SSH tunnel, reports arrive every 100ms → clear_window=500ms → we step
            # every 100ms (limited by the 0.1s tick above). Each +20% step is validated by the
            # client before the next — application-layer ACK-clocking.
            # Without metric_rtt or with stale clear signal: 2s fallback (original heuristic).
            # clear_window = max(500ms, 2×RTT).  When metric_rtt is not yet measured (first
            # ping hasn't completed) use the 500ms floor so lag reports from the initial
            # connect can still gate the ramp — otherwise the first 2s would use the 2s
            # fallback regardless of what the browser is reporting.
            if self._metric_rtt > 0:
                clear_window = max(0.5, 2.0 * self._metric_rtt / 1000.0)
            else:
                clear_window = 0.5
            have_clear = (now - self._last_clear_t) < clear_window
            if not have_clear and now - self._last_fast < 2.0:
                return
            self._last_fast = now
            fps_ceil = float(self.fps_cap) if self.fps_cap > 0 else self.max_fps
            _changed = False
            if self.fps < fps_ceil:
                self.fps = fps_ceil
                _changed = True
            elif self.bitrate < self._max_br:
                # --- measurement gate ------------------------------------------------
                # The encoder target may never exceed _PROBE_GROWTH x the rate we have
                # actually pushed through the link recently.  This replaces the old
                # `frame_bytes > 1500` gate, which was both too weak and too strong:
                #
                #   too weak  — it only guarded the ABOVE-ceiling branch.  Until the
                #               first congestion event _ceil_bitrate is 0, so
                #               target == _max_br, the below-target branch ran ungated,
                #               and the controller walked +20%/100ms all the way to
                #               _max_br (measured: 300k -> 35Mbps in 2.9s on a 6Mbps
                #               link) without a single byte of link testing.  The
                #               resulting overshoot is what produces the queue blowup,
                #               the backoff cascade, and the ratcheted-down ceiling.
                #   too strong— on a static screen no frame ever clears the byte
                #               threshold, so once a (possibly bogus) ceiling was
                #               recorded the controller was pinned at 0.9x it for the
                #               whole idle period and could only crawl +10%/tick out of
                #               it afterwards.  That is the reported "parked at a few
                #               hundred kbps, blurry until something moves" symptom.
                #
                # Measured throughput answers the real question ("did the link carry
                # this?") directly, and answers it in both directions: an idle screen
                # measures ~0 and cannot grow the target at all, while a busy screen on
                # a fast link licenses a doubling per tick.
                allowed = max(self._min_br, int(self._recent_bps(self._BPS_WINDOW)
                                                * self._PROBE_GROWTH))
                if allowed <= self.bitrate:
                    # Nothing recently sent justifies a higher target. Near-zero static
                    # P-frames land here — exactly what the old byte gate was for.
                    return
                allowed = min(allowed, self._max_br,
                              int(self.bitrate * self._MAX_TICK_GROWTH))

                # Below the last congestion point we are re-converging on known-good
                # capacity (slow start): take the whole measured allowance in one hop.
                # At or above it we are probing new territory (congestion avoidance):
                # keep the old cautious +10% / +5% steps, now also capped by measurement.
                target = int(self._ceil_bitrate * 0.90) if self._ceil_bitrate > 0 else self._max_br
                target = min(target, self._max_br)
                if self.bitrate < target:
                    self.bitrate = min(target, allowed)
                else:
                    factor = 1.10 if self.bitrate < 20_000_000 else 1.05
                    self.bitrate = min(self._max_br, allowed, int(self.bitrate * factor))
                self.jpeg_quality = min(95, self.jpeg_quality + 5)
                _changed = True
            if _changed:
                log.info("fresh: fps=%.1f br=%dk ceil=%dk max=%dk sent=%dk",
                         self.fps, self.bitrate // 1000, self._ceil_bitrate // 1000,
                         self._max_br // 1000, int(self._recent_bps(self._BPS_WINDOW)) // 1000)

    def on_screen_active(self):
        """Screen content changed after a static period — restore fps and jump toward last
        known stable bitrate. Uses 90% of the congestion ceiling (same as on_fresh recovery)
        to avoid immediately re-triggering congestion on every screen-active event."""
        with self._lock:
            fps_ceil = float(self.fps_cap) if self.fps_cap > 0 else self.max_fps
            self.fps = fps_ceil
            if self._ceil_bitrate > 0 and self._ceil_bitrate > self.bitrate:
                target = int(self._ceil_bitrate * 0.90)
                # Conservative step: at most 50% jump toward ceiling per active event.
                # The old 2× step caused a large first frame that exceeded the wb budget
                # and immediately triggered backoff, ratcheting the ceiling lower with
                # every burst of screen activity.
                step_ceil = max(int(self.bitrate * 1.5), self.bitrate + 300_000)
                self.bitrate = max(self._min_br, min(target, step_ceil))
                self.jpeg_quality = min(95, self.jpeg_quality + 20)
                # Only debounce on_fresh when we actually jump bitrate — setting
                # _last_fast unconditionally fires on every frame boundary during
                # active content (30fps captures × 40fps sender = frequent static→active
                # micro-transitions) and permanently blocks the 2s fallback ramp path
                # in on_fresh() when there are no client clear signals (e.g. background tab).
                self._last_fast = time.monotonic()
            log.debug("screen active: fps=%.1f br=%dk ceil=%dk", self.fps, self.bitrate//1000, self._ceil_bitrate//1000)

    def snapshot(self):
        with self._lock:
            return self.fps, self.bitrate, self.jpeg_quality
