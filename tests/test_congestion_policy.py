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


    def test_backoff_at_floor_does_not_block_on_fresh(self):
        """_backoff() when both bitrate AND fps are at their floors must not update
        _last_slow. Without this guard, on_fresh()'s 2s settle window is continuously
        refreshed by no-op backoffs, permanently preventing recovery ramp-up.

        Scenario: controller is at floor (300kbps, ~20fps at 50ms budget), still
        receiving lag reports (e.g. from SSH tunnel latency). After 3 seconds of
        no-op backoffs, on_fresh() must be able to ramp."""
        fake = _FakeClock()
        old_monotonic = congestion.time.monotonic
        congestion.time.monotonic = fake.monotonic
        try:
            ctrl = AdaptiveController(SimpleNamespace(max_fps=60, initial_bitrate=1_200_000))
            # Drive to floor
            for _ in range(20):
                ctrl.on_lag(10_000, 0)
                fake.step(0.31)

            fps_floor, br_floor, _ = ctrl.snapshot()
            self.assertEqual(br_floor, 300_000)

            # Simulate 3 seconds of lag reports while at floor — all should be no-ops
            _last_slow_before = ctrl._last_slow
            for _ in range(10):
                ctrl.on_lag(150, 0)   # 150ms lag > 50ms budget → triggers _backoff
                fake.step(0.31)

            # _last_slow must NOT have advanced (no-op backoffs at floor)
            self.assertEqual(ctrl._last_slow, _last_slow_before,
                             "_last_slow was updated by a no-op floor backoff")

            # Clear any drain pause left over from the initial 10s-lag drive phase —
            # this test is specifically about _last_slow not being poisoned, not drain behavior.
            ctrl._drain_until = 0.0

            # After 2s past the last real backoff, on_fresh() must make progress.
            # First call restores fps from the floor to fps_ceil; second call ramps bitrate.
            fake.step(2.1)
            ctrl._last_clear_t = fake.now - 0.1   # simulate recent browser "clear" signal
            ctrl.on_fresh()                         # first: fps restored to fps_ceil
            fake.step(0.2)
            ctrl._last_clear_t = fake.now - 0.1
            ctrl.on_fresh()                         # second: bitrate ramp

            _, br_after, _ = ctrl.snapshot()
            self.assertGreater(br_after, 300_000,
                               "on_fresh() could not ramp: settle timer was poisoned by floor backoff")
        finally:
            congestion.time.monotonic = old_monotonic


if __name__ == "__main__":
    unittest.main()
