#!/usr/bin/env python3
"""Policy guards for frame dropping and forced keyframes."""

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]


class HandlerPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "mvs" / "handler.py").read_text()

    def test_static_heartbeat_does_not_force_periodic_keyframes(self):
        self.assertNotIn("encoder.encode_keyframe, fb_s", self.source)
        self.assertIn("encoder.encode, fb_s", self.source)

    def test_post_encode_bandwidth_cap_drop_is_jpeg_only(self):
        marker = "# Enforce user bandwidth cap via rolling 1s window."
        start = self.source.index(marker)
        block = self.source[start:self.source.index("_n_diag += 1", start)]
        self.assertIn("if has_webcodecs:", block)
        self.assertIn("continue  # JPEG: safe to drop, no reference frames", block)

    def test_only_allowed_server_forced_keyframe_triggers_remain(self):
        lines = self.source.splitlines()
        triggers = []
        for idx, line in enumerate(lines, start=1):
            if "_need_keyframe = True" in line:
                window = "\n".join(lines[max(0, idx - 16):idx + 4])
                # Skip re-arm lines: payload=None retry paths that re-set the flag
                # rather than originating a new keyframe request.
                if "_was_kf_req" in window or "_kf_was_piped" in window:
                    continue
                triggers.append((idx, window))

        self.assertEqual(len(triggers), 3, triggers)
        # 1. New encoder reinit: VideoDecoder has no reference frame after reinit.
        self.assertIn("encoder.actual_codec != CODEC_JPEG", triggers[0][1])
        # 2. Explicit client keyframe request via WebSocket message.
        self.assertIn('elif t == "keyframe"', triggers[1][1])
        # 3. Codec change detected in frame_sender.
        self.assertIn("current_codec != last_encoder_codec", triggers[2][1])


if __name__ == "__main__":
    unittest.main()
