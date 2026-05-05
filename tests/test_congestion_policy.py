#!/usr/bin/env python3
"""Policy guards for the responsiveness-first congestion controller."""

from types import SimpleNamespace
import unittest

import mvs.congestion as congestion
from mvs.congestion import AdaptiveController


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def step(self, seconds):
        self.now += seconds


class CongestionPolicyTests(unittest.TestCase):
    def test_initial_bitrate_is_floored_at_300kbps(self):
        ctrl = AdaptiveController(SimpleNamespace(max_fps=60, initial_bitrate=1))
        self.assertEqual(ctrl.bitrate, 300_000)

    def test_user_bandwidth_cap_cannot_push_encoder_below_300kbps(self):
        ctrl = AdaptiveController(SimpleNamespace(max_fps=60, initial_bitrate=1_200_000))
        ctrl.on_quality(cap_h=0, fps_cap=0, max_kbps=1, lag_ms=0)

        self.assertEqual(ctrl.bitrate, 300_000)
        self.assertEqual(ctrl._max_br, 300_000)
        self.assertEqual(ctrl.user_bw_cap, 1_000)

    def test_backoff_never_encodes_below_300kbps_or_below_20fps_at_50ms_budget(self):
        fake = _FakeClock()
        old_monotonic = congestion.time.monotonic
        congestion.time.monotonic = fake.monotonic
        try:
            ctrl = AdaptiveController(SimpleNamespace(max_fps=60, initial_bitrate=1_200_000))
            for _ in range(20):
                ctrl.on_lag(10_000, 0)
                fake.step(0.31)

            fps, bitrate, _quality = ctrl.snapshot()
            self.assertEqual(bitrate, 300_000)
            self.assertGreaterEqual(fps, 20.0)
        finally:
            congestion.time.monotonic = old_monotonic


if __name__ == "__main__":
    unittest.main()
