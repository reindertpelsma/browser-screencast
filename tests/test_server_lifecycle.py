#!/usr/bin/env python3
"""Regression checks for server startup and cleanup."""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


class _FakeHeadless:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True


def _cfg():
    return types.SimpleNamespace(
        print_caps=False,
        headless=True,
        capture="auto",
        input="auto",
        password="",
        listen="127.0.0.1",
        port=6081,
        codec="h264",
        max_fps=60,
        headless_display=":123",
        headless_size="640x360x24",
        headless_wm="none",
    )


class ServerLifecycleTests(unittest.TestCase):
    def test_headless_env_accepts_common_truthy_values(self):
        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            with self.subTest(value=value), \
                    patch.dict(os.environ, {"HEADLESS": value}, clear=True), \
                    patch.object(sys, "argv", ["server.py"]):
                self.assertTrue(server.parse_args().headless)

    def test_headless_stops_if_bridge_construction_fails(self):
        fake_headless = _FakeHeadless()
        cfg = _cfg()

        with patch.object(server, "parse_args", return_value=cfg), \
                patch("mvs.codec.probe_server_codecs", return_value={"jpeg": {"sw": True}}), \
                patch("mvs.headless.HeadlessSession", return_value=fake_headless), \
                patch.object(server, "_build_bridge", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                server.main()

        self.assertTrue(fake_headless.started)
        self.assertTrue(fake_headless.stopped)


if __name__ == "__main__":
    unittest.main()
