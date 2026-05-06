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


if __name__ == "__main__":
    unittest.main()
