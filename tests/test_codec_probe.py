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
