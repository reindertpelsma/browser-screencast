#!/usr/bin/env python3
"""Regression checks for portable clipboard helpers."""

import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mvs.platform import Clipboard


class ClipboardTests(unittest.TestCase):
    def test_windows_get_clipboard_uses_raw_and_strips_powershell_record_separator(self):
        cb = Clipboard()
        cb._powershell_clip = Mock(return_value="hello\r\n")

        with patch.dict(sys.modules, {"pyperclip": None}), \
                patch("mvs.platform.platform.system", return_value="Windows"):
            self.assertEqual(cb.get(), "hello")

        cb._powershell_clip.assert_called_once_with("Get-Clipboard -Raw")


if __name__ == "__main__":
    unittest.main()
