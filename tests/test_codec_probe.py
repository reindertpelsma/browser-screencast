#!/usr/bin/env python3
"""Regression checks for codec capability probing."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mvs import codec


class _FakeOpenableContext:
    """Minimal stand-in for av.CodecContext that survives attribute sets + open()."""

    def open(self):
        pass

    def __setattr__(self, key, value):
        pass  # accept width/height/pix_fmt/etc without error


class _FakeCodecContext:
    @staticmethod
    def create(name, mode):
        if mode == "w" and name in {"libx264", "h264"}:
            return _FakeOpenableContext()
        raise RuntimeError(name)


class _FakeAv:
    CodecContext = _FakeCodecContext


class CodecProbeTests(unittest.TestCase):
    def setUp(self):
        self._old_av = codec._av
        self._old_av_ok = codec._AV_OK
        self._old_ffmpeg_encoders_text = codec.ffmpeg_encoders_text
        self._old_has_hw_encoder_device = codec._has_hw_encoder_device
        codec._pyav_codec_known.cache_clear()

    def tearDown(self):
        codec._av = self._old_av
        codec._AV_OK = self._old_av_ok
        codec.ffmpeg_encoders_text = self._old_ffmpeg_encoders_text
        codec._has_hw_encoder_device = self._old_has_hw_encoder_device
        codec._pyav_codec_known.cache_clear()

    def test_pyav_codec_known_returns_real_bool(self):
        codec._av = _FakeAv()
        codec._AV_OK = True

        self.assertIs(codec._pyav_codec_known("libx264"), True)
        self.assertIs(codec._pyav_codec_known("libvpx-vp9"), False)

    def test_probe_uses_pyav_when_ffmpeg_is_unavailable(self):
        codec._av = _FakeAv()
        codec._AV_OK = True
        codec.ffmpeg_encoders_text = lambda: ""

        caps = codec.probe_server_codecs()

        self.assertTrue(caps["h264"]["sw"])
        self.assertFalse(caps["h264"]["hw"])
        self.assertTrue(caps["jpeg"]["sw"])


    def test_system_ffmpeg_listing_alone_cannot_claim_hardware(self):
        """`ffmpeg -encoders` describes the SYSTEM ffmpeg CLI, not the FFmpeg PyAV
        encodes with, and a device node only proves a device exists. Neither may
        stand in for a real open.

        This is the live-deployment failure: the host listed hevc_nvenc and had an
        NVIDIA device node, so h265 hw read True; NVENC could not actually
        initialise (cuCtxCreate -> CUDA_ERROR_OUT_OF_MEMORY, BAR1 exhausted), the
        encoder cascade fell through to libx265, and the server ran software HEVC
        — which auto mode is explicitly written never to choose."""
        codec._av = _FakeAv()             # opens libx264/h264 only; no nvenc
        codec._AV_OK = True
        codec.ffmpeg_encoders_text = lambda: (
            "V....D hevc_nvenc  NVIDIA NVENC hevc encoder\n"
            "V....D h264_nvenc  NVIDIA NVENC H.264 encoder\n")
        codec._has_hw_encoder_device = lambda names: True

        caps = codec.probe_server_codecs()

        self.assertFalse(caps["h265"]["hw"], "claimed hw H.265 without opening it")
        self.assertFalse(caps["h264"]["hw"], "claimed hw H.264 without opening it")

    def test_hardware_is_reported_when_it_actually_opens(self):
        class _NvencAv:
            class CodecContext:
                @staticmethod
                def create(name, mode):
                    if name in {"hevc_nvenc", "libx264", "h264"}:
                        return _FakeOpenableContext()
                    raise RuntimeError(name)

        codec._av = _NvencAv()
        codec._AV_OK = True
        codec.ffmpeg_encoders_text = lambda: ""
        codec._has_hw_encoder_device = lambda names: True

        caps = codec.probe_server_codecs()

        self.assertTrue(caps["h265"]["hw"])
        self.assertFalse(caps["av1"]["hw"])

    def test_no_hardware_device_means_no_hardware_capability(self):
        codec._av = _FakeAv()
        codec._AV_OK = True
        codec.ffmpeg_encoders_text = lambda: "V....D hevc_nvenc\n"
        codec._has_hw_encoder_device = lambda names: False

        self.assertFalse(codec.probe_server_codecs()["h265"]["hw"])


class HardwareFallbackVisibilityTests(unittest.TestCase):
    """A hardware->software downgrade is a large per-frame cost increase; it must
    not be discoverable only by reading the source.

    Measured on the live deployment's host (RTX 3060, 11 cores, 1920x1080):
    libx264 costs 26ms/frame at a 1Mbps target and 96ms at 16Mbps — a 10fps
    ceiling. Before this, every candidate failure went to log.debug, so the log
    read `codec negotiation: h265 server=hw` then `Encoder: libx265` with nothing
    linking the two."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.source = (Path(__file__).resolve().parents[1] / "mvs" / "encoder.py").read_text()

    def test_hardware_open_failures_are_warned_not_debugged(self):
        self.assertIn("log.warning(\"Hardware encoder %s unavailable: %s\"", self.source)

    def test_selected_encoder_records_hardware_or_software(self):
        self.assertIn('"hardware" if _is_hw_encoder(name) else "software"', self.source)

    def test_failure_reason_comes_from_captured_libav_logs(self):
        # PyAV's own exception is only `avcodec_open2("hevc_nvenc", {})`; the
        # reason FFmpeg printed has to be captured or it is lost.
        self.assertIn("capture_libav_logs", self.source)
        self.assertIn("libav_logs", self.source)


class SelectCodecPolicyTests(unittest.TestCase):
    """Policy invariants for auto-mode and explicit-mode codec selection."""

    # Convenience caps builders
    @staticmethod
    def _srv(**kw):
        """Build server caps dict. e.g. h264_sw=True, h265_hw=True."""
        caps = {c: {"hw": False, "sw": False} for c in ("h264","h265","av1","vp9")}
        caps["jpeg"] = {"sw": True, "hw": False}
        for k, v in kw.items():
            codec_name, kind = k.rsplit("_", 1)
            caps[codec_name][kind] = v
        return caps

    @staticmethod
    def _cli(**kw):
        caps = {c: {"hw": False, "sw": False} for c in ("h264","h265","av1","vp9")}
        caps["jpeg"] = {"sw": True, "hw": False}
        for k, v in kw.items():
            codec_name, kind = k.rsplit("_", 1)
            caps[codec_name][kind] = v
        return caps

    def test_auto_prefers_h265_hw_over_h264_hw(self):
        srv = self._srv(h265_hw=True, h264_hw=True)
        cli = self._cli(h265_sw=True, h264_hw=True)
        result, s, c = codec.select_codec(srv, cli)
        self.assertEqual(result, codec.CODEC_H265)
        self.assertEqual(s, "hw")

    def test_auto_sw_h265_does_not_beat_h264_hw(self):
        """SW H.265 encode must never win over H.264 hw in auto mode."""
        srv = self._srv(h265_sw=True, h264_hw=True)
        cli = self._cli(h265_sw=True, h264_hw=True)
        result, s, _ = codec.select_codec(srv, cli)
        self.assertNotEqual((result, s), (codec.CODEC_H265, "sw"),
                            "sw H.265 encode must not beat h264 hw in auto mode")
        self.assertEqual(result, codec.CODEC_H264)
        self.assertEqual(s, "hw")

    def test_auto_av1_hw_requires_client_hw_decode(self):
        """In auto mode, AV1 hw encode is only selected when client has hw AV1 decode."""
        srv = self._srv(av1_hw=True, h265_hw=True)
        # Client can only sw-decode AV1
        cli = self._cli(av1_sw=True, h265_sw=True)
        result, _, _ = codec.select_codec(srv, cli)
        self.assertNotEqual(result, codec.CODEC_AV1,
                            "AV1 must not be selected when client lacks hw AV1 decode")
        self.assertEqual(result, codec.CODEC_H265)

    def test_auto_h265_beats_av1_when_both_hw_available(self):
        """H.265 hw is preferred over AV1 hw in auto mode (safer default for constrained paths)."""
        srv = self._srv(av1_hw=True, h265_hw=True)
        cli = self._cli(av1_hw=True, h265_sw=True)
        result, s, c = codec.select_codec(srv, cli)
        self.assertEqual(result, codec.CODEC_H265)
        self.assertEqual(s, "hw")

    def test_auto_falls_back_to_h264_sw_when_no_hw(self):
        """Software-only server defaults to H.264 sw; never sw H.265/AV1/VP9."""
        srv = self._srv(h264_sw=True, h265_sw=True, av1_sw=True)
        cli = self._cli(h264_sw=True, h265_sw=True, av1_sw=True, vp9_sw=True)
        result, s, _ = codec.select_codec(srv, cli)
        self.assertEqual(result, codec.CODEC_H264)
        self.assertEqual(s, "sw")

    def test_explicit_allows_sw_h265(self):
        srv = self._srv(h265_sw=True, h264_sw=True)
        cli = self._cli(h265_sw=True, h264_sw=True)
        result, s, _ = codec.select_codec(srv, cli, explicit=True)
        self.assertEqual(result, codec.CODEC_H265)
        self.assertEqual(s, "sw")

    def test_explicit_allows_vp9_sw(self):
        srv = self._srv(vp9_sw=True, h264_sw=True)
        cli = self._cli(vp9_sw=True, h264_sw=True)
        result, _, _ = codec.select_codec(srv, cli, explicit=True)
        self.assertEqual(result, codec.CODEC_VP9)

    def test_explicit_av1_sw_encode_excluded(self):
        """libsvtav1 sw encode is excluded even in explicit mode (Chrome rejects bitstream)."""
        srv = self._srv(av1_sw=True, h264_sw=True)
        cli = self._cli(av1_sw=True, h264_sw=True)
        result, _, _ = codec.select_codec(srv, cli, explicit=True)
        self.assertNotEqual(result, codec.CODEC_AV1,
                            "AV1 sw encode must never be selected (Chrome bitstream rejection)")


if __name__ == "__main__":
    unittest.main()
