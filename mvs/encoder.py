import fractions
import logging

import numpy as np

from mvs.codec import (CODEC_JPEG, CODEC_H264, CODEC_H265, CODEC_AV1, CODEC_VP9,
                        _AV_OK, _av, _encode_jpeg)

log = logging.getLogger("browser_screencast")


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
                ("h264_nvenc", {"preset": "p4", "tune": "ull", "rc": "vbr"}),
                ("h264_qsv", {"preset": "veryfast"}),
                ("h264_amf", {"usage": "ultralowlatency", "quality": "speed"}),
                ("libx264", {"preset": "fast", "tune": "zerolatency",
                             "x264-params": "bframes=0:rc-lookahead=0:aq-mode=1"}),
            ],
            CODEC_H265: [
                ("hevc_nvenc", {"preset": "p4", "tune": "ull", "rc": "vbr"}),
                ("hevc_qsv", {"preset": "veryfast"}),
                ("hevc_amf", {"usage": "ultralowlatency", "quality": "speed"}),
                ("libx265", {"preset": "fast", "tune": "zerolatency",
                             "x265-params": "bframes=0:rc-lookahead=0:aq-mode=1"}),
            ],
            CODEC_AV1: [
                ("av1_nvenc", {"preset": "p4", "tune": "ull", "rc": "vbr"}),
                ("av1_qsv", {"preset": "veryfast"}),
                ("av1_amf", {"usage": "ultralowlatency", "quality": "speed"}),
                ("libsvtav1", {"preset": "10",
                               "svtav1-params": "film-grain=0:irefresh-type=2"}),
                ("libaom-av1", {"cpu-used": "10", "usage": "realtime"}),
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
                # Large GOP: one I-frame per 5 seconds at max fps. Static screens
                # produce near-zero P-frames; a short GOP would flood with large I-frames.
                cc.gop_size = 99999
                cc.options = opts
                cc.open()
                # Warm up hardware encoder — first frame is buffered, discard it.
                # key_frame=True ensures this discarded frame is a true IDR so
                # subsequent P-frames have a known reference base.
                dummy = _av.VideoFrame(cc.width, cc.height, "yuv420p")
                dummy.pts = 0
                dummy.pict_type = 1
                dummy.key_frame = True
                list(cc.encode(dummy))
                self._last_pts = 0
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
        if self._cc is None:
            return _encode_jpeg(self._to_rgb(rgb), jpeg_quality), True, CODEC_JPEG
        try:
            fmt = "bgra" if (rgb.ndim == 3 and rgb.shape[2] == 4) else "rgb24"
            frame = _av.VideoFrame.from_ndarray(rgb, format=fmt)
            # Downscale to encoder dimensions if source is larger (libswscale Lanczos).
            if frame.width != self._cc.width or frame.height != self._cc.height:
                frame = frame.reformat(width=self._cc.width, height=self._cc.height, format=fmt,
                                       interpolation="LANCZOS")
            frame = frame.reformat(format="yuv420p")
            pts = max(self._last_pts + 1, capture_ms)
            frame.pts = pts
            self._last_pts = pts
            pkts = list(self._cc.encode(frame))
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
        """
        if self._cc is None:
            return _encode_jpeg(self._to_rgb(rgb), quality), True, CODEC_JPEG
        pkts = []
        try:
            fmt = "bgra" if (rgb.ndim == 3 and rgb.shape[2] == 4) else "rgb24"
            frame = _av.VideoFrame.from_ndarray(rgb, format=fmt)
            if frame.width != self._cc.width or frame.height != self._cc.height:
                frame = frame.reformat(width=self._cc.width, height=self._cc.height, format=fmt,
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
            frame.key_frame = True
            pkts = list(self._cc.encode(frame))
            self._last_pts = pts
        except Exception as e:
            log.debug("encode_keyframe err: %s", e)
        if not pkts:
            return None, False, self.actual_codec
        return bytes(pkts[0]), True, self.actual_codec

    def close(self):
        if self._cc:
            try: self._cc.close()
            except Exception: pass
            self._cc = None
