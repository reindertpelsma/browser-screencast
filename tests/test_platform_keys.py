#!/usr/bin/env python3
"""Regression checks for portable keyboard translation."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mvs.platform import X11Input, XK


class _RecordingX11Input(X11Input):
    def __init__(self):
        super().__init__()
        self.events = []

    def send_key(self, down: bool, keysym: int):
        self.events.append((bool(down), int(keysym)))
        return True


class X11KeyTranslationTests(unittest.TestCase):
    def test_physical_digit_key_does_not_inject_shift(self):
        inp = _RecordingX11Input()

        self.assertTrue(inp.send_key_event(True, "Digit1", "!"))

        self.assertEqual(inp.events, [(True, ord("1"))])

    def test_raw_shifted_symbol_uses_shift_chord(self):
        inp = _RecordingX11Input()

        self.assertTrue(inp.send_key_event(True, "!", "!"))
        self.assertTrue(inp.send_key_event(False, "!", "!"))

        self.assertEqual(inp.events, [
            (True, XK["ShiftLeft"]),
            (True, ord("1")),
            (False, ord("1")),
            (False, XK["ShiftLeft"]),
        ])

    def test_type_text_preserves_uppercase_and_symbols(self):
        inp = _RecordingX11Input()

        inp.type_text("A!")

        self.assertEqual(inp.events, [
            (True, XK["ShiftLeft"]),
            (True, ord("a")),
            (False, ord("a")),
            (False, XK["ShiftLeft"]),
            (True, XK["ShiftLeft"]),
            (True, ord("1")),
            (False, ord("1")),
            (False, XK["ShiftLeft"]),
        ])


if __name__ == "__main__":
    unittest.main()
