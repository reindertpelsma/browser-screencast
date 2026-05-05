#!/usr/bin/env python3
"""Regression checks for server-managed Xvfb lifecycle."""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mvs.headless import HeadlessSession


class _FakeProc:
    def __init__(self, poll_result=None):
        self.returncode = poll_result
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True

    def kill(self):
        self.killed = True


def _cfg(display=":123", wm="auto"):
    return types.SimpleNamespace(
        headless_display=display,
        headless_size="640x360x24",
        headless_wm=wm,
    )


class HeadlessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._old_display = os.environ.get("DISPLAY")

    def tearDown(self):
        if self._old_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = self._old_display

    def test_start_cleans_partial_xvfb_when_display_never_becomes_ready(self):
        os.environ.pop("DISPLAY", None)
        xvfb = _FakeProc()
        session = HeadlessSession(_cfg())

        def fake_which(name):
            return f"/usr/bin/{name}" if name in {"Xvfb", "xdpyinfo"} else None

        with patch("mvs.headless.shutil.which", side_effect=fake_which), \
                patch("mvs.headless.subprocess.Popen", return_value=xvfb), \
                patch.object(HeadlessSession, "_display_ready", return_value=False), \
                patch.object(HeadlessSession, "_wait_display",
                             side_effect=RuntimeError("not ready")):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                session.start()

        self.assertTrue(xvfb.terminated)
        self.assertTrue(xvfb.waited)
        self.assertEqual(session._procs, [])
        self.assertNotEqual(os.environ.get("DISPLAY"), ":123")

    def test_immediately_exiting_window_manager_is_not_retained(self):
        os.environ.pop("DISPLAY", None)
        wm = _FakeProc(poll_result=7)
        session = HeadlessSession(_cfg(display=":124"))

        def fake_which(name):
            if name in {"Xvfb", "xdpyinfo", "openbox"}:
                return f"/usr/bin/{name}"
            return None

        with patch("mvs.headless.shutil.which", side_effect=fake_which), \
                patch("mvs.headless.subprocess.Popen", return_value=wm), \
                patch("mvs.headless.time.sleep"), \
                patch.object(HeadlessSession, "_display_ready", return_value=True):
            self.assertTrue(session.start())

        self.assertEqual(session._procs, [])
        self.assertEqual(os.environ.get("DISPLAY"), ":124")
        session.stop()
        self.assertIsNone(os.environ.get("DISPLAY"))


if __name__ == "__main__":
    unittest.main()
