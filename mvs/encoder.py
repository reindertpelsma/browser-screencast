import fractions
import logging

import numpy as np

from mvs.codec import (CODEC_JPEG, CODEC_H264, CODEC_H265, CODEC_AV1, CODEC_VP9,
                        _AV_OK, _av, _encode_jpeg)

log = logging.getLogger("browser_screencast")

# Software encoder names — no warmup needed (no hardware pipeline to prime).
_SW_ENCODERS = frozenset({
    "libx264", "libx265", "libsvtav1", "libaom-av1", "libvpx-vp9",
})


# ---------------------------------------------------------------------------
# EncoderPipeline — per-client H.264/H.265 with JPEG fallback
# ---------------------------------------------------------------------------
class EncoderPipeline:
    def __init__(self, target_codec, width, height, bitrate):
        self.target_codec = target_codec
        self.actual_codec = CODEC_JPEG
        self._cc = None
        self._last_pts = -1
        self._setup(width, height, bitrate)

    def _setup(self, width, height, bitrate):
        if not _AV_OK or self.target_codec == CODEC_JPEG:
            return
        # Keep VBR-style realtime settings. Strict CBR modes on hardware
        # encoders have backend-specific failure modes; bandwidth enforcement
        # lives in congestion.py and the websocket send loop.
        candidates = {
            CODEC_H264: [
                # bf=0: no B-frames. level=4.2 matches avc1.64002a codec string.
                ("h264_nvenc", {"preset": "p4", "tune": "ull", "rc": "vbr",
                                "bf": "0", "level": "4.2",
                                # forced-idr: NVENC ignores pict_type=I as an IDR
                                # request and honours gop_size instead, so without
                                # this encode_keyframe() gets a P-frame back,
                                # discards it as "IDR still in the pipeline" and
                                # retries forever -- zero frames ever sent.
                                # NOTE the HYPHEN: "forced_idr" is not the AVOption
                                # name and is silently ignored (measured: identical
                                # 1/12 keyframes with and without it).
                                "forced-idr": "1"}),
                ("h264_qsv", {"preset": "veryfast", "look_ahead": "0",
                              "low_power": "0"}),
                ("h264_amf", {"usage": "ultralowlatency", "quality": "speed",
                              "level": "4.2"}),
                # threads=1 + sliced-threads=0: tune=zerolatency sets
                # sliced-threads=1 (N slices per frame matching thread count),
                # which Chrome's WebCodecs H264 hardware decoder misdecodes.
                # We omit tune=zerolatency entirely because it also sets
                # no-mbtree=1, disabling macroblock-tree rate control and
                # causing visible P-frame quality drift.  Instead we set only
                # the needed low-latency params explicitly.
                # force-cfr=1 + sync-lookahead=0 → immediate per-frame output.
                ("libx264", {"preset": "fast", "threads": "1",
                             "x264-params": "bframes=0:rc-lookahead=0:aq-mode=1:sliced-threads=0"}),
            ],
            CODEC_H265: [
                # bf=0: no B-frames (low-latency screen capture). level=4.1 matches
                # the codec string the frontend declares; without an explicit level
                # nvenc may auto-pick a higher one and trigger the same DPB-undersize
                # bug as libx265 did.
                ("hevc_nvenc", {"preset": "p4", "tune": "ull", "rc": "vbr",
                                "bf": "0", "level": "4.1",
                                # See the h264_nvenc note: hyphenated, and required
                                # or encode_keyframe() never returns a packet.
                                # Measured on an RTX 3060: 1/12 keyframes without,
                                # 10/10 with.
                                "forced-idr": "1"}),
                ("hevc_qsv", {"preset": "veryfast", "look_ahead": "0",
                              "low_power": "0"}),
                ("hevc_amf", {"usage": "ultralowlatency", "quality": "speed",
                              "level": "4.1"}),
                # min-keyint=1: allows consecutive IDR frames when encode_keyframe()
                # is retried during x265's 2-frame pipeline startup.
                # no-sao + no-strong-intra-smoothing: SAO and intra smoothing blur
                # sharp text/UI edges in screen capture; harmful for screen content.
                # scenecut=0: prevents spontaneous IDR on scene cuts (browser sees
                # unlabelled IDR as a P-frame, corrupting DPB until next forced IDR).
                # tune=zerolatency omitted: it disables cu-tree rate control, causing
                # uneven post-motion bit distribution and visible softness after pans.
                # ultrafast preset: x265 "fast" is 5-10× heavier than x264 "fast";
                # on CPU-only systems it produces ~1fps at 1280×720. ultrafast gives
                # adequate quality for screen capture (mostly static, sharp UI edges)
                # while being fast enough for real-time use.
                # wpp=0:pmode=0:pme=0: disable all intra-frame parallelism. x265 spawns
                # per-frame worker threads by default; on QEMU / low-vCPU hosts they
                # thrash against each other and the Python thread pool, cutting throughput
                # further. Single-threaded is faster end-to-end for short frames.
                ("libx265", {"preset": "ultrafast",
                             "x265-params": "bframes=0:rc-lookahead=0:aq-mode=1:min-keyint=1:scenecut=0:no-sao=1:no-strong-intra-smoothing=1:level-idc=4.1:weightp=0:wpp=0:pmode=0:pme=0"}),
            ],
            CODEC_AV1: [
                ("av1_nvenc", {"preset": "p4", "tune": "ull", "rc": "vbr",
                               # Same IDR requirement as the other NVENC entries.
                               # Untested here -- Ampere has no AV1 encode block,
                               # so this path could not be exercised on the test GPU.
                               "forced-idr": "1"}),
                ("av1_qsv", {"preset": "veryfast"}),
                ("av1_amf", {"usage": "ultralowlatency", "quality": "speed"}),
                # enable-tf=0: temporal filtering produces alt-ref-style references
                # that Chrome's WebCodecs AV1 decoder stalls on (out-of-order frames).
                # low-delay=1: force low-delay (P-frame only) prediction structure so
                # the encoder outputs each frame immediately without buffering a
                # lookahead window. Replaces pred-struct=1 which was removed in
                # SVT-AV1 v2+. Without low-delay=1, v3.x uses random-access mode and
                # overrides lookahead=0 with a forced minimum of 20 frames.
                # irefresh-type=2: IDR refresh so forced keyframes flush immediately.
                ("libsvtav1", {"preset": "10",
                               "svtav1-params": "film-grain=0:irefresh-type=2:enable-tf=0:lookahead=0:low-delay=1"}),
                ("libaom-av1", {"cpu-used": "9", "usage": "realtime"}),
            ],
            CODEC_VP9: [
                ("libvpx-vp9", {"deadline": "realtime", "cpu-used": "8",
                                "row-mt": "1", "lag-in-frames": "0"}),
            ],
        }
        for name, opts in candidates.get(self.target_codec, []):
            try:
                cc = _av.CodecContext.create(name, "w")
                cc.width = width & ~1
                cc.height = height & ~1
                cc.pix_fmt = "yuv420p"
                cc.bit_rate = bitrate
                cc.time_base = fractions.Fraction(1, 1000)
                # Declare 60fps so encoders (SVT-AV1, x265) don't derive 1000fps
                # from time_base=1/1000, which causes level/profile violations.
                cc.framerate = fractions.Fraction(60, 1)
                # Large GOP: one I-frame per 5 seconds at max fps. Static screens
                # produce near-zero P-frames; a short GOP would flood with large I-frames.
                cc.gop_size = 99999
                cc.options = opts
                cc.open()
                # Hardware encoders buffer the first frame; encode a black IDR warmup
                # to prime the pipeline so the first real frame is not delayed.
                # libsvtav1 forces a 20-frame lookahead regardless of options (preset
                # 10 uses a 5-level hierarchy that needs 20 frames to plan). Pre-fill
                # the lookahead with dummy black frames so the first real capture frame
                # produces output immediately instead of waiting for 20 captures.
                # Other pure-output software encoders (libx264/libx265/libvpx-vp9)
                # output on the first frame and don't need priming.
                # NVENC with bf=0 + tune=ull has no reorder delay, and its warmup
                # returns 0 packets -- the black dummy stays IN the pipeline and is
                # emitted as the first real packet, so every decoded frame is offset
                # by one against its source (measured: PSNR 7.8 dB on pkt 0).
                # Skip the warmup for NVENC; it is priming that costs correctness.
                if name.endswith("_nvenc"):
                    self._last_pts = 0
                elif name not in _SW_ENCODERS:
                    dummy = _av.VideoFrame(cc.width, cc.height, "yuv420p")
                    dummy.pts = 0
                    dummy.pict_type = 1
                    # PyAV exposes VideoFrame.key_frame read-only (confirmed on
                    # 8.1.0 and on >=11 -- this is not a new-version regression).
                    # Assigning it raises AttributeError, which the except below
                    # swallows as "codec failed", so EVERY hardware candidate
                    # (nvenc/qsv/amf) was silently rejected and the chain fell
                    # through to libx264/libx265 -- which skip this branch via
                    # _SW_ENCODERS and so probe fine. Measured on an RTX 3060:
                    # without this, hevc_nvenc and h264_nvenc are both rejected;
                    # with it, both are selected. pict_type=I already marks the
                    # warmup frame as an IDR, so nothing is lost by dropping it.
                    try:
                        dummy.key_frame = True
                    except AttributeError:
                        pass
                    list(cc.encode(dummy))
                    self._last_pts = 0
                elif name == "libsvtav1":
                    dummy = _av.VideoFrame(cc.width, cc.height, "yuv420p")
                    for i in range(25):  # fill 20-frame lookahead + 5 spare
                        dummy.pts = i
                        list(cc.encode(dummy))
                    self._last_pts = 24
                self._cc = cc
                self.actual_codec = self.target_codec
                log.info("Encoder: %s %dx%d @%dkbps", name, cc.width, cc.height, bitrate//1000)
                return
            except Exception as e:
                log.debug("Codec %s failed: %s", name, e)
        log.warning("No video codec available — JPEG fallback")

    def set_bitrate(self, bitrate):
        if self._cc is not None:
            try: self._cc.bit_rate = bitrate
            except Exception: pass

    @staticmethod
    def _to_rgb(frame):
        """Convert BGRA or RGB frame to RGB (in-place if already RGB)."""
        if frame.ndim == 3 and frame.shape[2] == 4:
            return np.ascontiguousarray(frame[:, :, 2::-1])
        return frame

    def encode(self, rgb, capture_ms, jpeg_quality=65):
        """Returns (payload, is_keyframe, codec_byte) or (None, False, _) on skip."""
        # Snapshot _cc into a local so a concurrent close() (from _upgrade_encoder racing
        # with run_in_executor) cannot turn it None between the null check and encode().
        cc = self._cc
        if cc is None:
            return _encode_jpeg(self._to_rgb(rgb), jpeg_quality), True, CODEC_JPEG
        try:
            fmt = "bgra" if (rgb.ndim == 3 and rgb.shape[2] == 4) else "rgb24"
            frame = _av.VideoFrame.from_ndarray(rgb, format=fmt)
            # Downscale to encoder dimensions if source is larger (libswscale Lanczos).
            if frame.width != cc.width or frame.height != cc.height:
                frame = frame.reformat(width=cc.width, height=cc.height, format=fmt,
                                       interpolation="LANCZOS")
            frame = frame.reformat(format="yuv420p")
            pts = max(self._last_pts + 1, capture_ms)
            frame.pts = pts
            self._last_pts = pts
            pkts = list(cc.encode(frame))
            if not pkts:
                return None, False, self.actual_codec
            pkt = pkts[0]
            is_kf = bool(getattr(pkt, "is_keyframe", True))
            return bytes(pkt), is_kf, self.actual_codec
        except Exception as e:
            log.warning("Encode error: %s — JPEG fallback", e)
            self._cc = None
            self.actual_codec = CODEC_JPEG
            return _encode_jpeg(self._to_rgb(rgb), jpeg_quality), True, CODEC_JPEG

    def encode_keyframe(self, rgb, capture_ms, quality):
        """Force an IDR frame — a true decoder-reset point, not just an I-slice.

        libx264 distinguishes X264_TYPE_I (I-slice, cannot reset decoder DPB)
        from X264_TYPE_IDR (IDR, resets reference picture buffer).  WebCodecs
        VideoDecoder requires a real IDR for chunks marked type:'key'.  Setting
        frame.key_frame=True is the PyAV/libavcodec path that maps to IDR;
        frame.pict_type=AV_PICTURE_TYPE_I only produces an I-slice.

        Pipelined encoders (libx265 2-frame threading) may output a queued P-frame
        while the actual IDR is still in the pipeline.  Returning None here signals
        the caller to retry — the handler re-sets _need_keyframe and tries again.
        """
        cc = self._cc  # snapshot — same close() race guard as encode()
        if cc is None:
            return _encode_jpeg(self._to_rgb(rgb), quality), True, CODEC_JPEG
        pkts = []
        try:
            fmt = "bgra" if (rgb.ndim == 3 and rgb.shape[2] == 4) else "rgb24"
            frame = _av.VideoFrame.from_ndarray(rgb, format=fmt)
            if frame.width != cc.width or frame.height != cc.height:
                frame = frame.reformat(width=cc.width, height=cc.height, format=fmt,
                                       interpolation="LANCZOS")
            frame = frame.reformat(format="yuv420p")
            pts = max(self._last_pts + 1, capture_ms)
            frame.pts = pts
            # key_frame=True → X264_TYPE_IDR (actual IDR, resets decoder DPB)
            # libx264 via libavcodec requires BOTH flags to emit X264_TYPE_IDR:
            #   pict_type = AV_PICTURE_TYPE_I  → enters the IDR/I-slice branch
            #   key_frame = True               → selects IDR over I-slice within that branch
            # key_frame alone → X264_TYPE_AUTO (P-frame, no forced keyframe).
            frame.pict_type = 1
            # PyAV exposes key_frame read-only (8.1.0 and >=11 alike), so this
            # raises AttributeError -- which the except below swallowed, leaving
            # pkts empty. encode_keyframe() then returned None, the handler
            # re-armed _need_keyframe, and every subsequent frame took the same
            # path: a permanent retry loop in which NOTHING is ever sent. That
            # is the "capture runs at 60fps but the client sees a static
            # screen" symptom -- DIAG showed cap=60.0fps, sent=0.0fps,
            # nosend=300/5s. IDR is instead forced by pict_type=I plus the
            # encoder's forced-idr option (see the candidate table above).
            try:
                frame.key_frame = True
            except AttributeError:
                pass
            pkts = list(cc.encode(frame))
            self._last_pts = pts
        except Exception as e:
            log.debug("encode_keyframe err: %s", e)
        if not pkts:
            return None, False, self.actual_codec
        pkt = pkts[0]
        # Pipelined encoders output the previously buffered frame when a new one is
        # submitted.  If the output is not a keyframe, the IDR request is still in
        # the pipeline — discard the stale P-frame and signal retry.
        if not bool(getattr(pkt, "is_keyframe", True)):
            return None, False, self.actual_codec
        return bytes(pkt), True, self.actual_codec

    def close(self):
        if self._cc:
            try: self._cc.close()
            except Exception: pass
            self._cc = None
