#!/usr/bin/env python3
"""Regression guards for the idle -> active bitrate ramp.

Two properties have to hold at once, and historically the controller could only
manage one of them:

  ramp-up   an idle screen that starts changing again must reach a usable
            bitrate in well under a second on a link that can carry it;
  honesty   the target must never climb on near-zero static frames, which prove
            nothing about the link and used to let the controller walk to tens
            of Mbps before the first real frame congested it.

The ramp is clocked by measured throughput (AdaptiveController._recent_bps), so
these tests drive the controller the way the sender does: report_sent() after
every frame, a client "clear" confirmation, then on_fresh().
"""

import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mvs.congestion as congestion
from mvs.congestion import AdaptiveController

ROOT = Path(__file__).resolve().parents[1]


class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def step(self, seconds):
        self.now += seconds


class _Clocked(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeClock()
        self._real = congestion.time.monotonic
        congestion.time.monotonic = self.fake.monotonic
        self.addCleanup(self._restore)

    def _restore(self):
        congestion.time.monotonic = self._real

    def _ctrl(self, **kw):
        cfg = SimpleNamespace(max_fps=60, initial_bitrate=kw.pop("initial_bitrate", 1_000_000))
        return AdaptiveController(cfg)

    def _run(self, ctrl, seconds, fps=20.0, frame_bytes=None, link_bps=None):
        """Drive `seconds` of sending through the controller.

        frame_bytes  fixed payload size, or None to model an encoder that fills
                     its current target (bitrate / fps / 8), optionally clipped
                     by `link_bps` to model a link that cannot carry the target.
        Returns the elapsed simulated seconds.
        """
        n = max(1, int(seconds * fps))
        dt = seconds / n
        for _ in range(n):
            self.fake.step(dt)
            if frame_bytes is not None:
                nb = frame_bytes
            else:
                eff = ctrl.bitrate if link_bps is None else min(ctrl.bitrate, link_bps)
                nb = max(1, int(eff / fps / 8))
            ctrl.report_sent(nb)
            ctrl.on_client_clear()          # browser reports age < budget
            ctrl.on_fresh(nb)
        return seconds


class NearZeroFrameTests(_Clocked):
    """The property the old `frame_bytes > 1500` gate existed to protect."""

    def test_static_frames_do_not_raise_the_target(self):
        ctrl = self._ctrl(initial_bitrate=1_000_000)
        # 60fps of 300-byte P-frames = ~144kbps. An idle VNC desktop looks like this.
        self._run(ctrl, seconds=10.0, fps=60.0, frame_bytes=300)
        self.assertEqual(ctrl.bitrate, 1_000_000,
                         "near-zero static frames must not license any capacity")

    def test_static_frames_do_not_raise_the_target_before_any_congestion(self):
        """The regression the old gate MISSED.

        With _ceil_bitrate == 0 the ramp target was _max_br, so the gate's
        above-ceiling branch never ran and the ungated +20%/tick branch walked
        the target all the way to the cap on frames carrying nothing.
        """
        ctrl = self._ctrl(initial_bitrate=1_000_000)
        self.assertEqual(ctrl._ceil_bitrate, 0)
        self._run(ctrl, seconds=10.0, fps=60.0, frame_bytes=300)
        self.assertEqual(ctrl._ceil_bitrate, 0)
        self.assertLess(ctrl.bitrate, 1_100_000,
                        "controller probed above a never-tested link on idle frames")

    def test_heartbeat_only_traffic_does_not_raise_the_target(self):
        """A static screen throttled to one heartbeat every 2s: too few samples
        to measure anything, so the target must simply hold."""
        ctrl = self._ctrl(initial_bitrate=2_000_000)
        for _ in range(10):
            self.fake.step(2.0)
            ctrl.report_sent(4_000)
            ctrl.on_client_clear()
            ctrl.on_fresh(4_000)
        self.assertEqual(ctrl.bitrate, 2_000_000)

    def test_target_never_exceeds_twice_measured_throughput(self):
        """The invariant the whole design rests on.

        Checked against the peak measurement seen so far, not the instantaneous
        one: on_fresh only ever raises the target, so a rate that later drops
        does not pull it back down — that is backoff's job.
        """
        ctrl = self._ctrl(initial_bitrate=300_000)
        peak = 0.0
        for _ in range(400):
            self.fake.step(0.05)
            ctrl.report_sent(2_500)          # 20fps x 2.5KB = 400kbps, steady
            ctrl.on_client_clear()
            ctrl.on_fresh(2_500)
            peak = max(peak, ctrl._recent_bps(ctrl._BPS_WINDOW))
            self.assertLessEqual(
                ctrl.bitrate, max(300_000, int(peak * ctrl._PROBE_GROWTH)) + 1,
                "target exceeded twice the measured send rate")
        self.assertLess(ctrl.bitrate, 1_000_000,
                        "a steady 400kbps stream must not license megabits")


class IdleToActiveRampTests(_Clocked):
    def test_ramp_from_floor_reaches_2mbps_in_well_under_a_second(self):
        ctrl = self._ctrl(initial_bitrate=300_000)
        elapsed = 0.0
        dt = 1.0 / 20.0
        while elapsed < 3.0 and ctrl.bitrate < 2_000_000:
            self.fake.step(dt)
            nb = max(1, int(ctrl.bitrate / 20.0 / 8))
            ctrl.report_sent(nb)
            ctrl.on_client_clear()
            ctrl.on_fresh(nb)
            elapsed += dt
        self.assertGreaterEqual(ctrl.bitrate, 2_000_000,
                                f"never reached 2Mbps (got {ctrl.bitrate})")
        self.assertLess(elapsed, 1.0,
                        f"took {elapsed:.2f}s to reach 2Mbps from the 300kbps floor")

    def test_idle_period_does_not_strand_the_ramp(self):
        """Active, then a long idle stretch of heartbeats, then active again.

        The second active burst must climb again immediately. Before the fix a
        stale congestion ceiling plus the byte gate left the controller pinned
        at 0.9x that ceiling for the rest of the session.
        """
        ctrl = self._ctrl(initial_bitrate=300_000)
        self._run(ctrl, seconds=2.0, fps=20.0)
        busy = ctrl.bitrate
        self.assertGreater(busy, 2_000_000)

        # Pretend a congestion event stamped a low ceiling, the way a spurious
        # backoff used to.
        ctrl._ceil_bitrate = 600_000
        ctrl.bitrate = 540_000
        ctrl._last_slow = self.fake.now
        self.fake.step(3.0)                 # past the 2s settle window

        # 20s of static heartbeats — must change nothing.
        for _ in range(10):
            self.fake.step(2.0)
            ctrl.report_sent(4_000)
            ctrl.on_client_clear()
            ctrl.on_fresh(4_000)
        self.assertEqual(ctrl.bitrate, 540_000, "idle period moved the target")

        # Activity resumes.
        elapsed = 0.0
        dt = 1.0 / 20.0
        while elapsed < 3.0 and ctrl.bitrate < 2_000_000:
            self.fake.step(dt)
            nb = max(1, int(ctrl.bitrate / 20.0 / 8))
            ctrl.report_sent(nb)
            ctrl.on_client_clear()
            ctrl.on_fresh(nb)
            elapsed += dt
        self.assertGreaterEqual(ctrl.bitrate, 2_000_000,
                                "stale ceiling stranded the ramp after an idle period")
        # A genuine congestion ceiling still puts the ramp on the cautious
        # +10%/tick congestion-avoidance path, so this is slower than the
        # no-ceiling case above (which is under 1s). What matters here is that
        # it recovers at all — before the fix it never left 0.9x the ceiling.
        self.assertLess(elapsed, 2.5, f"took {elapsed:.2f}s to recover after idle")

    def test_ramp_stops_near_a_link_that_cannot_carry_more(self):
        """A 2Mbps link: the encoder never delivers more than that, so the
        target must settle within the growth factor of it rather than running
        away to the 50Mbps cap."""
        ctrl = self._ctrl(initial_bitrate=300_000)
        self._run(ctrl, seconds=20.0, fps=20.0, link_bps=2_000_000)
        # 2x the link rate, plus a little slack for the measurement transient
        # while the window is still filling.
        self.assertLessEqual(ctrl.bitrate, int(2_000_000 * ctrl._PROBE_GROWTH * 1.2),
                             f"target ran away to {ctrl.bitrate} on a 2Mbps link")


class ProactiveBackoffWatchdogTests(unittest.TestCase):
    """The watchdog that treats missing lag reports as congestion must not fire
    just because the screen went static.

    The browser only emits a lag report when a frame arrives, and a static
    screen is deliberately throttled to one heartbeat every 2s. Arming the
    watchdog on a frame COUNT over the 5s diagnostic window kept it live for
    seconds after the screen stopped changing, which fired repeated backoffs and
    ratcheted _ceil_bitrate down — measured at 2189k -> 519k in 2.1s of idle.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "mvs" / "handler.py").read_text()

    def test_watchdog_is_armed_by_unacked_sends_not_by_a_window_frame_count(self):
        idx = self.source.index("proactive backoff: no lag report")
        cond = self.source[self.source.rindex("if (", 0, idx):idx]
        self.assertIn("_sends_since_lag >= 3", cond)
        self.assertNotIn("_n_diag", cond)

    def test_lag_report_clears_the_unacked_counter(self):
        idx = self.source.index("_last_lag_received = time.monotonic()")
        self.assertIn("_sends_since_lag = 0", self.source[idx:idx + 200])

    def test_every_send_path_increments_the_unacked_counter(self):
        # Both the normal send and the static heartbeat must count, otherwise
        # the watchdog either never fires or fires on an idle screen again.
        self.assertEqual(self.source.count("_sends_since_lag += 1"), 2)


if __name__ == "__main__":
    unittest.main()
