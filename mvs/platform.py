import ctypes
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger("browser_screencast")


# X11 / RFB keysyms for the browser keys this project sends most often.
XK = {
    "Backspace": 0xFF08,
    "BackSpace": 0xFF08,
    "Tab": 0xFF09,
    "Enter": 0xFF0D,
    "Return": 0xFF0D,
    "Escape": 0xFF1B,
    "Delete": 0xFFFF,
    "Insert": 0xFF63,
    "Home": 0xFF50,
    "End": 0xFF57,
    "PageUp": 0xFF55,
    "PageDown": 0xFF56,
    "ArrowLeft": 0xFF51,
    "ArrowUp": 0xFF52,
    "ArrowRight": 0xFF53,
    "ArrowDown": 0xFF54,
    "ShiftLeft": 0xFFE1,
    "ShiftRight": 0xFFE2,
    "ControlLeft": 0xFFE3,
    "ControlRight": 0xFFE4,
    "AltLeft": 0xFFE9,
    "AltRight": 0xFFEA,
    "MetaLeft": 0xFFEB,   # Super_L, not Meta_L: browser Cmd/Win -> remote Super
    "MetaRight": 0xFFEC,  # Super_R
    "CapsLock": 0xFFE5,
    "Space": 0x0020,
    " ": 0x0020,
    "Minus": ord("-"),
    "Equal": ord("="),
    "BracketLeft": ord("["),
    "BracketRight": ord("]"),
    "Backslash": ord("\\"),
    "Semicolon": ord(";"),
    "Quote": ord("'"),
    "Comma": ord(","),
    "Period": ord("."),
    "Slash": ord("/"),
    "Backquote": ord("`"),
}
for _i in range(1, 13):
    XK[f"F{_i}"] = 0xFFBD + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    XK[f"Key{_c.upper()}"] = ord(_c)
    XK[_c] = ord(_c)
    XK[_c.upper()] = ord(_c)
for _d in "0123456789":
    XK[f"Digit{_d}"] = ord(_d)
    XK[_d] = ord(_d)
del _c, _d, _i

_SHIFTED_ASCII = {
    "~": "`",
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
}


def _x11_ascii_chord(ch: str):
    if not ch or len(ch) != 1 or ord(ch) >= 128:
        return None
    if "A" <= ch <= "Z":
        return XK["ShiftLeft"], ord(ch.lower())
    base = _SHIFTED_ASCII.get(ch)
    if base:
        return XK["ShiftLeft"], ord(base)
    if 32 <= ord(ch) < 127:
        return None, ord(ch)
    return None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _first_monitor(monitors, idx: int):
    # mss uses index 0 for the virtual full desktop and 1..N for real monitors.
    if idx >= 0 and idx < len(monitors):
        return monitors[idx]
    if len(monitors) > 1:
        return monitors[1]
    return monitors[0]


class MSSCapture:
    """Cross-platform full-screen capture using python-mss.

    On Linux/X11 this is the v0.1 direct capture backend. On Windows it is the
    functional fallback until Windows.Graphics.Capture lands.
    """

    name = "mss"

    def __init__(self, fps: int = 60, monitor: int = 1):
        self._fps = max(1, min(120, int(fps or 60)))
        self._monitor_idx = int(monitor or 1)
        self._lock = threading.Lock()
        self._fb = None
        self._cached_fb = None
        self._cached_seq = -1
        self._fb_seq = 0
        self._fb_ms = 0
        self._W = 0
        self._H = 0
        self._running = False
        self._thread = None
        self._last_hash = None

    @property
    def dimensions(self):
        with self._lock:
            return self._W, self._H

    @property
    def _fbu_count(self):
        return self._fb_seq

    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        try:
            import mss  # noqa: F401
        except Exception as e:
            log.warning("mss capture unavailable: %s", e)
            return False
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="capture-mss")
        self._thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.dimensions != (0, 0):
                return True
            if not self._running:
                break
            time.sleep(0.05)
        log.warning("mss capture did not produce a frame")
        self.stop()
        return False

    def stop(self):
        self._running = False

    def _run(self):
        try:
            import mss
        except Exception:
            self._running = False
            return
        interval = 1.0 / self._fps
        try:
            with mss.mss() as sct:
                mon = _first_monitor(sct.monitors, self._monitor_idx)
                log.info("capture=%s monitor=%s %dx%d", self.name, self._monitor_idx,
                         mon.get("width", 0), mon.get("height", 0))
                while self._running:
                    t0 = time.monotonic()
                    try:
                        shot = sct.grab(mon)
                        frame = np.asarray(shot)
                        # mss returns BGRA. Keep BGRA: EncoderPipeline accepts it and
                        # avoids a copy before handing frames to PyAV.
                        if not frame.flags["C_CONTIGUOUS"]:
                            frame = np.ascontiguousarray(frame)
                        h = int(shot.height)
                        w = int(shot.width)
                        # Hash a small stride sample so static-screen detection avoids
                        # sending 60 identical frames but still detects quick changes.
                        sample = frame[::32, ::32, :3]
                        hsh = hash(sample.tobytes())
                        with self._lock:
                            self._fb = frame.copy()
                            self._W = w
                            self._H = h
                            self._fb_ms = _now_ms()
                            if hsh != self._last_hash:
                                self._fb_seq += 1
                                self._cached_seq = -1
                                self._last_hash = hsh
                    except Exception as e:
                        log.warning("capture frame failed: %s", e)
                        time.sleep(0.5)
                    elapsed = time.monotonic() - t0
                    if elapsed < interval:
                        time.sleep(interval - elapsed)
        finally:
            self._running = False

    def get_current_frame(self):
        with self._lock:
            if self._fb is None:
                return None, 0
            if self._fb_seq != self._cached_seq:
                self._cached_fb = self._fb.copy()
                self._cached_seq = self._fb_seq
            return self._cached_fb, self._fb_ms or _now_ms()


class NullInput:
    name = "none"

    def start(self) -> bool:
        return True

    def send_pointer(self, buttons: int, x: int, y: int):
        return False

    def send_scroll(self, dx: int, dy: int, x: int, y: int):
        return False

    def send_key_event(self, down: bool, code: str, key: str):
        return False

    def send_key(self, down: bool, keysym: int):
        return False

    def send_key_reset(self):
        return None

    def type_text(self, text: str):
        return False


class X11Input(NullInput):
    name = "x11"

    def __init__(self):
        self._display = None
        self._prev_buttons = 0

    def start(self) -> bool:
        if not os.environ.get("DISPLAY"):
            log.warning("X11 input requested but DISPLAY is unset")
            return False
        try:
            from Xlib import display  # noqa: F401
            from Xlib.ext import xtest  # noqa: F401
        except Exception as e:
            log.warning("X11 input unavailable: %s", e)
            return False
        try:
            from Xlib import display
            self._display = display.Display()
            log.info("input=X11 XTest")
            return True
        except Exception as e:
            log.warning("X11 input open failed: %s", e)
            return False

    def _fake(self, event_type, detail=0, x=0, y=0):
        if self._display is None:
            return False
        try:
            from Xlib.ext import xtest
            xtest.fake_input(self._display, event_type, detail=detail, x=x, y=y)
            self._display.sync()
            return True
        except Exception as e:
            log.debug("XTest input failed: %s", e)
            return False

    def send_pointer(self, buttons: int, x: int, y: int):
        try:
            from Xlib import X
            self._fake(X.MotionNotify, x=max(0, int(x)), y=max(0, int(y)))
            changed = buttons ^ self._prev_buttons
            for mask, btn in ((1, 1), (2, 2), (4, 3)):
                if changed & mask:
                    self._fake(X.ButtonPress if buttons & mask else X.ButtonRelease, detail=btn)
            self._prev_buttons = buttons & 0x7
            return True
        except Exception:
            return False

    def send_scroll(self, dx: int, dy: int, x: int, y: int):
        try:
            from Xlib import X
            self._fake(X.MotionNotify, x=max(0, int(x)), y=max(0, int(y)))
            events = []
            if dy:
                events.append((5 if dy > 0 else 4, abs(int(dy))))
            if dx:
                events.append((7 if dx > 0 else 6, abs(int(dx))))
            for btn, count in events:
                for _ in range(min(count, 40)):
                    self._fake(X.ButtonPress, detail=btn)
                    self._fake(X.ButtonRelease, detail=btn)
            return True
        except Exception:
            return False

    def _keysym_to_keycode(self, keysym: int) -> int:
        try:
            return int(self._display.keysym_to_keycode(int(keysym)))
        except Exception:
            return 0

    def send_key_event(self, down: bool, code: str, key: str):
        keysym = XK.get(code)
        if keysym is not None:
            return self.send_key(down, keysym)
        if key and len(key) == 1 and ord(key) < 128:
            return self._send_ascii_key(down, key)
        keysym = XK.get(key)
        return self.send_key(down, keysym) if keysym else False

    def _send_ascii_key(self, down: bool, ch: str):
        chord = _x11_ascii_chord(ch)
        if not chord:
            return False
        shift, keysym = chord
        if not shift:
            return self.send_key(down, keysym)
        if down:
            self.send_key(True, shift)
            return self.send_key(True, keysym)
        ok = self.send_key(False, keysym)
        self.send_key(False, shift)
        return ok

    def send_key(self, down: bool, keysym: int):
        try:
            from Xlib import X
            keycode = self._keysym_to_keycode(keysym)
            if not keycode:
                return False
            return self._fake(X.KeyPress if down else X.KeyRelease, detail=keycode)
        except Exception:
            return False

    def send_key_reset(self):
        for keysym in (0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE9, 0xFFEA, 0xFFEB, 0xFFEC):
            self.send_key(False, keysym)
        self.send_pointer(0, 0, 0)

    def type_text(self, text: str):
        for ch in text:
            if ch == "\n":
                self.send_key(True, XK["Enter"])
                self.send_key(False, XK["Enter"])
            elif ch == "\t":
                self.send_key(True, XK["Tab"])
                self.send_key(False, XK["Tab"])
            elif ord(ch) < 128:
                self._send_ascii_key(True, ch)
                self._send_ascii_key(False, ch)
            time.sleep(0.003)
        return True


class WindowsInput(NullInput):
    name = "windows"

    KEYEVENTF_KEYUP = 0x0002
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_HWHEEL = 0x01000

    VK = {
        "Backspace": 0x08, "BackSpace": 0x08, "Tab": 0x09, "Enter": 0x0D,
        "Return": 0x0D, "Escape": 0x1B, "Space": 0x20, " ": 0x20,
        "PageUp": 0x21, "PageDown": 0x22, "End": 0x23, "Home": 0x24,
        "ArrowLeft": 0x25, "ArrowUp": 0x26, "ArrowRight": 0x27, "ArrowDown": 0x28,
        "Insert": 0x2D, "Delete": 0x2E,
        "ShiftLeft": 0xA0, "ShiftRight": 0xA1, "ControlLeft": 0xA2, "ControlRight": 0xA3,
        "AltLeft": 0xA4, "AltRight": 0xA5, "MetaLeft": 0x5B, "MetaRight": 0x5C,
        "CapsLock": 0x14,
        "Minus": 0xBD, "Equal": 0xBB, "BracketLeft": 0xDB, "BracketRight": 0xDD,
        "Backslash": 0xDC, "Semicolon": 0xBA, "Quote": 0xDE, "Comma": 0xBC,
        "Period": 0xBE, "Slash": 0xBF, "Backquote": 0xC0,
    }
    for _i in range(1, 13):
        VK[f"F{_i}"] = 0x6F + _i
    for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        VK[f"Key{_c}"] = ord(_c)
        VK[_c.lower()] = ord(_c)
        VK[_c] = ord(_c)
    for _d in "0123456789":
        VK[f"Digit{_d}"] = ord(_d)
        VK[_d] = ord(_d)
    del _c, _d, _i

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUTUNION(ctypes.Union):
        pass

    INPUTUNION._fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        pass

    INPUT._fields_ = [("type", ctypes.c_ulong), ("union", INPUTUNION)]

    def __init__(self):
        self._prev_buttons = 0
        self._user32 = None

    def start(self) -> bool:
        if platform.system() != "Windows":
            return False
        self._user32 = ctypes.windll.user32
        log.info("input=Windows SendInput")
        return True

    def _send_input(self, inp):
        if self._user32 is None:
            return False
        try:
            return bool(self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)))
        except Exception as e:
            log.debug("SendInput failed: %s", e)
            return False

    def _key(self, vk: int, down: bool):
        inp = self.INPUT()
        inp.type = 1
        inp.union.ki = self.KEYBDINPUT(vk, 0, 0 if down else self.KEYEVENTF_KEYUP, 0, None)
        return self._send_input(inp)

    def send_key_event(self, down: bool, code: str, key: str):
        vk = self.VK.get(code) or self.VK.get(key)
        if vk is None and key and len(key) == 1:
            vk = self.VK.get(key.upper())
        return self._key(vk, down) if vk else False

    def send_key(self, down: bool, keysym: int):
        # Best-effort fallback for RFB/X11 keysyms.
        rev = {v: k for k, v in XK.items()}
        return self.send_key_event(down, rev.get(keysym, ""), chr(keysym) if 32 <= keysym < 127 else "")

    def _mouse(self, flags: int, data: int = 0, x: int = 0, y: int = 0):
        inp = self.INPUT()
        inp.type = 0
        inp.union.mi = self.MOUSEINPUT(x, y, data, flags, 0, None)
        return self._send_input(inp)

    def send_pointer(self, buttons: int, x: int, y: int):
        if self._user32 is None:
            return False
        sx = max(1, int(self._user32.GetSystemMetrics(0)) - 1)
        sy = max(1, int(self._user32.GetSystemMetrics(1)) - 1)
        ax = int(max(0, min(sx, x)) * 65535 / sx)
        ay = int(max(0, min(sy, y)) * 65535 / sy)
        self._mouse(self.MOUSEEVENTF_MOVE | self.MOUSEEVENTF_ABSOLUTE, x=ax, y=ay)
        changed = buttons ^ self._prev_buttons
        for mask, dn, up in (
            (1, self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP),
            (2, self.MOUSEEVENTF_MIDDLEDOWN, self.MOUSEEVENTF_MIDDLEUP),
            (4, self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP),
        ):
            if changed & mask:
                self._mouse(dn if buttons & mask else up)
        self._prev_buttons = buttons & 0x7
        return True

    def send_scroll(self, dx: int, dy: int, x: int, y: int):
        self.send_pointer(self._prev_buttons, x, y)
        if dy:
            self._mouse(self.MOUSEEVENTF_WHEEL, data=int(-dy) * 120)
        if dx:
            self._mouse(self.MOUSEEVENTF_HWHEEL, data=int(dx) * 120)
        return True

    def send_key_reset(self):
        for code in ("ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight",
                     "AltLeft", "AltRight", "MetaLeft", "MetaRight"):
            self.send_key_event(False, code, "")
        self.send_pointer(0, 0, 0)

    def type_text(self, text: str):
        # Clipboard paste is the reliable Unicode path on Windows; this fallback
        # only handles simple ASCII.
        for ch in text:
            if ch == "\n":
                self.send_key_event(True, "Enter", "")
                self.send_key_event(False, "Enter", "")
            elif ord(ch) < 128:
                self.send_key_event(True, "", ch)
                self.send_key_event(False, "", ch)
            time.sleep(0.003)
        return True


class Clipboard:
    def __init__(self):
        self.text = ""
        self.seq = 0
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._poll, daemon=True, name="clipboard")
        self._thread.start()

    def stop(self):
        self._running = False

    def get(self) -> str:
        try:
            import pyperclip
            return pyperclip.paste() or ""
        except Exception:
            pass
        if platform.system() == "Windows":
            return self._powershell_clip("Get-Clipboard -Raw").rstrip("\r\n")
        for cmd in (["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"],
                    ["xsel", "-b", "-o"]):
            if shutil.which(cmd[0]):
                try:
                    return subprocess.run(cmd, capture_output=True, text=True,
                                          timeout=1.5).stdout
                except Exception:
                    pass
        return ""

    def set(self, text: str):
        ok = False
        try:
            import pyperclip
            pyperclip.copy(text)
            ok = True
        except Exception:
            pass
        if not ok and platform.system() == "Windows":
            ok = bool(self._powershell_clip("Set-Clipboard", text))
        if not ok:
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                        ["xsel", "-b", "-i"]):
                if shutil.which(cmd[0]):
                    try:
                        subprocess.run(cmd, input=text, text=True, timeout=1.5)
                        ok = True
                        break
                    except Exception:
                        pass
        if ok:
            self.text = text
            self.seq += 1
        return ok

    def _powershell_clip(self, command: str, text: Optional[str] = None):
        exe = shutil.which("powershell.exe") or shutil.which("powershell")
        if not exe:
            return ""
        try:
            if text is None:
                return subprocess.run([exe, "-NoProfile", "-Command", command],
                                      capture_output=True, text=True, timeout=2).stdout
            subprocess.run([exe, "-NoProfile", "-Command", command],
                           input=text, text=True, timeout=2)
            return "ok"
        except Exception:
            return ""

    def _poll(self):
        while self._running:
            try:
                text = self.get()
                if text != self.text:
                    self.text = text
                    self.seq += 1
            except Exception:
                pass
            time.sleep(1.0)


@dataclass
class BackendSelection:
    capture: object
    input: object


class PlatformBridge:
    """Adapter consumed by mvs.handler.

    It presents the same methods/properties as the macscreencast bridge while
    routing capture and input through platform-specific backends.
    """

    def __init__(self, cfg, selection: BackendSelection):
        self._cfg = cfg
        self._capture = selection.capture
        self._input = selection.input or NullInput()
        self._clipboard = Clipboard()

    def start(self) -> bool:
        if not self._capture.start():
            return False
        if not self._input.start():
            log.warning("input backend unavailable; view-only mode")
            self._input = NullInput()
        self._clipboard.start()
        return True

    def stop(self):
        try:
            self._capture.stop()
        except Exception:
            pass
        self._clipboard.stop()

    def is_running(self):
        return self._capture.is_running()

    @property
    def dimensions(self):
        return self._capture.dimensions

    @property
    def _fb_seq(self):
        return self._capture._fb_seq

    @property
    def _fb_ms(self):
        return self._capture._fb_ms

    @property
    def _fbu_count(self):
        return getattr(self._capture, "_fbu_count", self._capture._fb_seq)

    @property
    def server_clipboard_seq(self):
        return self._clipboard.seq

    @property
    def server_clipboard(self):
        return self._clipboard.text

    def get_current_frame(self):
        return self._capture.get_current_frame()

    def send_pointer(self, buttons, x, y):
        return self._input.send_pointer(int(buttons), int(x), int(y))

    def send_scroll(self, dx, dy, x, y):
        return self._input.send_scroll(int(dx), int(dy), int(x), int(y))

    def send_key_event(self, down: bool, code: str, key: str):
        return self._input.send_key_event(bool(down), code or "", key or "")

    def send_key(self, down, keysym):
        return self._input.send_key(bool(down), int(keysym))

    def send_clipboard(self, text):
        return self._clipboard.set(text or "")

    def send_key_reset(self):
        return self._input.send_key_reset()

    def paste_text(self, text: str, paste: bool = True, type_chars: bool = False):
        text = text or ""
        if type_chars and text:
            return self._input.type_text(text)
        if text:
            self.send_clipboard(text)
        if paste:
            self._input.send_key_event(True, "ControlLeft", "Control")
            self._input.send_key_event(True, "KeyV", "v")
            self._input.send_key_event(False, "KeyV", "v")
            self._input.send_key_event(False, "ControlLeft", "Control")
        return True


def detect_capture_mode(requested: str) -> str:
    requested = requested or "auto"
    if requested != "auto":
        return requested
    sysname = platform.system()
    if sysname == "Windows":
        return "windows"
    if os.environ.get("DISPLAY"):
        return "x11"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "none"


def select_platform_backends(cfg) -> BackendSelection:
    cap_mode = detect_capture_mode(getattr(cfg, "capture", "auto"))
    input_mode = getattr(cfg, "input", "auto") or "auto"
    fps = int(getattr(cfg, "max_fps", 60) or 60)
    monitor = int(getattr(cfg, "monitor", 1) or 1)

    if cap_mode in ("x11", "windows", "mss"):
        capture = MSSCapture(fps=fps, monitor=monitor)
    elif cap_mode == "wayland":
        raise RuntimeError("Wayland capture is planned but not implemented in this v0.1 scaffold")
    elif cap_mode == "none":
        raise RuntimeError("No display detected. Use X11/Windows, --capture vnc, or start Xvfb first.")
    else:
        raise RuntimeError(f"Unsupported capture backend: {cap_mode}")

    if input_mode == "none":
        input_backend = NullInput()
    elif input_mode == "x11" or (input_mode == "auto" and platform.system() != "Windows"):
        input_backend = X11Input()
    elif input_mode == "windows" or (input_mode == "auto" and platform.system() == "Windows"):
        input_backend = WindowsInput()
    else:
        input_backend = NullInput()

    return BackendSelection(capture=capture, input=input_backend)


def create_platform_bridge(cfg) -> PlatformBridge:
    return PlatformBridge(cfg, select_platform_backends(cfg))
