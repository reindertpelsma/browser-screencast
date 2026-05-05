#!/usr/bin/env python3
"""Regression checks for the frame wire header used by smoke tests."""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mvs.codec import CODEC_H264, _hdr


class WireHeaderTests(unittest.TestCase):
    def test_keyframe_flag_is_separate_from_codec_byte(self):
        msg = _hdr(7, 123456, CODEC_H264, False, 42)
        seq, ts_ms, codec, flags, payload_len = struct.unpack(">IQBBI", msg)

        self.assertEqual(seq, 7)
        self.assertEqual(ts_ms, 123456)
        self.assertEqual(codec, CODEC_H264)
        self.assertEqual(flags & 1, 0)
        self.assertEqual(payload_len, 42)


if __name__ == "__main__":
    unittest.main()
