"""
X11 keyboard loopback test using Xvfb.

Injects events through X11Input (the same path used by the browser-screencast
server) and reads them back from a real X11 window on the same display.
Covers: regular keys, all modifier keys (Shift/Ctrl/Alt/Meta), shifted
symbols, special keys, and the send_key_event(code, key) translation path
that the server uses for every keydown/keyup message from the browser.

Skip conditions (auto):
  - Xvfb not in PATH
  - python-xlib not installed

Run locally: cd /workspace/browser-screencast && .venv/bin/python -m pytest tests/test_x11_keyboard_loopback.py -v
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import pytest

# ---------------------------------------------------------------------------
# Guard Xlib import so the module can still be collected on systems without it
# ---------------------------------------------------------------------------
_XLIB_OK = False
if True:  # keep indent to allow conditional assignment without name-error
    try:
        from Xlib import display as _Xdisplay, X as _X, XK as _XKmod
        from Xlib.ext import xtest as _xtest  # noqa: F401
        _XLIB_OK = True
    except ImportError:
        pass

_XVFB = shutil.which("Xvfb")

pytestmark = pytest.mark.skipif(
    not (_XLIB_OK and _XVFB),
    reason="Xvfb or python-xlib not available",
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Xvfb lifecycle — module-scoped so one server serves all tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def xvfb_display():
    """Start Xvfb, return ":N" display string, kill on teardown."""
    for n in range(99, 200):
        lock = f"/tmp/.X{n}-lock"
        if os.path.exists(lock):
            continue
        proc = subprocess.Popen(
            [_XVFB, f":{n}", "-screen", "0", "1024x768x24",
             "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.4)
        if proc.poll() is None:
            old = os.environ.get("DISPLAY")
            os.environ["DISPLAY"] = f":{n}"
            yield f":{n}"
            if old is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = old
            proc.terminate()
            proc.wait(timeout=5)
            return
        proc.wait()
    pytest.skip("Could not start Xvfb on any display :99-:199")


# ---------------------------------------------------------------------------
# Event-capture window
# ---------------------------------------------------------------------------

class _EventWindow:
    """Creates a focused X11 window and records KeyPress/KeyRelease events."""

    def __init__(self, display_str: str):
        self._d = _Xdisplay.Display(display_str)
        screen = self._d.screen()
        self.win = screen.root.create_window(
            0, 0, 200, 150, 0,
            screen.root_depth,
            _X.InputOutput,
            _X.CopyFromParent,
            background_pixel=screen.white_pixel,
            event_mask=_X.KeyPressMask | _X.KeyReleaseMask,
            override_redirect=True,
        )
        self.win.set_wm_name("kbd-loopback")
        self.win.map()
        self.win.configure(stack_mode=_X.Above)
        self._d.sync()
        self.win.set_input_focus(_X.RevertToParent, _X.CurrentTime)
        self._d.sync()
        self.events: list = []  # list of (type, keycode)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True,
                                        name="kbd-loopback-capture")
        self._thread.start()

    def _poll(self):
        while not self._stop.is_set():
            try:
                while self._d.pending_events():
                    ev = self._d.next_event()
                    if ev.type in (_X.KeyPress, _X.KeyRelease):
                        self.events.append((ev.type, ev.detail))
            except Exception:
                pass
            time.sleep(0.004)

    def keycode(self, keysym: int) -> int:
        return int(self._d.keysym_to_keycode(int(keysym)))

    def wait(self, pred, timeout: float = 1.0) -> bool:
        """Wait until at least one event in self.events satisfies pred."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(pred(e) for e in self.events):
                return True
            time.sleep(0.01)
        return False

    def saw_press(self, keysym: int) -> bool:
        kc = self.keycode(keysym)
        return self.wait(lambda e: e == (_X.KeyPress, kc))

    def saw_release(self, keysym: int) -> bool:
        kc = self.keycode(keysym)
        return self.wait(lambda e: e == (_X.KeyRelease, kc))

    def clear(self):
        self.events.clear()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1)
        try:
            self.win.destroy()
            self._d.close()
        except Exception:
            pass


@pytest.fixture
def event_win(xvfb_display):
    w = _EventWindow(xvfb_display)
    yield w
    w.close()


@pytest.fixture
def x11inp(xvfb_display):
    from mvs.platform import X11Input
    inp = X11Input()
    assert inp.start(), "X11Input.start() failed"
    yield inp
    inp.send_key_reset()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _keysym(name: str) -> int:
    return _XKmod.string_to_keysym(name)


# ---------------------------------------------------------------------------
# Tests: basic keys
# ---------------------------------------------------------------------------

def test_letter_key_press_and_release(event_win, x11inp):
    x11inp.send_key_event(True, "KeyA", "a")
    x11inp.send_key_event(False, "KeyA", "a")
    assert event_win.saw_press(_keysym("a")), "KeyA press not received"
    assert event_win.saw_release(_keysym("a")), "KeyA release not received"


def test_digit_key(event_win, x11inp):
    x11inp.send_key_event(True, "Digit3", "3")
    x11inp.send_key_event(False, "Digit3", "3")
    assert event_win.saw_press(_keysym("3")), "Digit3 press not received"


def test_enter_key(event_win, x11inp):
    x11inp.send_key_event(True, "Enter", "Enter")
    x11inp.send_key_event(False, "Enter", "Enter")
    assert event_win.saw_press(_keysym("Return")), "Enter press not received"


def test_backspace_key(event_win, x11inp):
    x11inp.send_key_event(True, "Backspace", "Backspace")
    x11inp.send_key_event(False, "Backspace", "Backspace")
    assert event_win.saw_press(_keysym("BackSpace")), "Backspace press not received"


def test_escape_key(event_win, x11inp):
    x11inp.send_key_event(True, "Escape", "Escape")
    x11inp.send_key_event(False, "Escape", "Escape")
    assert event_win.saw_press(_keysym("Escape")), "Escape press not received"


def test_tab_key(event_win, x11inp):
    x11inp.send_key_event(True, "Tab", "Tab")
    x11inp.send_key_event(False, "Tab", "Tab")
    assert event_win.saw_press(_keysym("Tab")), "Tab press not received"


# ---------------------------------------------------------------------------
# Tests: arrow keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,keysym_name", [
    ("ArrowLeft",  "Left"),
    ("ArrowRight", "Right"),
    ("ArrowUp",    "Up"),
    ("ArrowDown",  "Down"),
])
def test_arrow_key(event_win, x11inp, code, keysym_name):
    event_win.clear()
    x11inp.send_key_event(True, code, code)
    x11inp.send_key_event(False, code, code)
    assert event_win.saw_press(_keysym(keysym_name)), f"{code} press not received"


# ---------------------------------------------------------------------------
# Tests: modifier keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,keysym_name,label", [
    ("ShiftLeft",   "Shift_L",   "Shift"),
    ("ControlLeft", "Control_L", "Ctrl"),
    ("AltLeft",     "Alt_L",     "Alt"),
    ("MetaLeft",    "Super_L",   "Super/Meta"),
])
def test_modifier_press_and_release(event_win, x11inp, code, keysym_name, label):
    event_win.clear()
    x11inp.send_key_event(True, code, code)
    x11inp.send_key_event(False, code, code)
    ks = _keysym(keysym_name)
    assert event_win.saw_press(ks), f"{label} press not received"
    assert event_win.saw_release(ks), f"{label} release not received"


# ---------------------------------------------------------------------------
# Tests: modifier + letter chord
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod_code,mod_ks_name,mod_label", [
    ("ShiftLeft",   "Shift_L",   "Shift"),
    ("ControlLeft", "Control_L", "Ctrl"),
    ("AltLeft",     "Alt_L",     "Alt"),
    ("MetaLeft",    "Super_L",   "Super"),
])
def test_modifier_chord_with_letter(event_win, x11inp, mod_code, mod_ks_name, mod_label):
    """Browser sends modifier-down then letter-down as two separate messages."""
    event_win.clear()
    x11inp.send_key_event(True, mod_code, mod_code)
    x11inp.send_key_event(True, "KeyS", "s")
    x11inp.send_key_event(False, "KeyS", "s")
    x11inp.send_key_event(False, mod_code, mod_code)

    mod_ks = _keysym(mod_ks_name)
    letter_ks = _keysym("s")

    assert event_win.saw_press(mod_ks), f"{mod_label} press not received in chord"
    assert event_win.saw_press(letter_ks), f"s press not received in {mod_label}+S chord"
    assert event_win.saw_release(letter_ks), f"s release not received in {mod_label}+S chord"
    assert event_win.saw_release(mod_ks), f"{mod_label} release not received in chord"


# ---------------------------------------------------------------------------
# Tests: shifted symbols (send_key_event with raw key character)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("char,shift_base,base_ks_name", [
    ("!",  True,  "1"),
    ("@",  True,  "2"),
    ("#",  True,  "3"),
    ("$",  True,  "4"),
    ("%",  True,  "5"),
    ("^",  True,  "6"),
    ("&",  True,  "7"),
    ("*",  True,  "8"),
    ("(",  True,  "9"),
    (")",  True,  "0"),
    ("_",  True,  "minus"),
    ("+",  True,  "equal"),
    ("{",  True,  "bracketleft"),
    ("}",  True,  "bracketright"),
])
def test_shifted_symbol_injects_shift_chord(event_win, x11inp, char, shift_base, base_ks_name):
    """Shifted symbols (!, @, … ) must arrive as Shift + base-key."""
    event_win.clear()
    # Browser sends: code="" key="!" for a shifted symbol typed directly
    x11inp.send_key_event(True, char, char)
    x11inp.send_key_event(False, char, char)
    shift_ks = _keysym("Shift_L")
    base_ks = _keysym(base_ks_name)
    assert event_win.saw_press(shift_ks), f"Shift not injected for '{char}'"
    assert event_win.saw_press(base_ks), f"base key '{base_ks_name}' not injected for '{char}'"


# ---------------------------------------------------------------------------
# Tests: type_text path
# ---------------------------------------------------------------------------

def test_type_text_basic(event_win, x11inp):
    event_win.clear()
    x11inp.type_text("hi")
    assert event_win.saw_press(_keysym("h")), "h not delivered by type_text"
    assert event_win.saw_press(_keysym("i")), "i not delivered by type_text"


def test_type_text_uppercase(event_win, x11inp):
    event_win.clear()
    x11inp.type_text("A")
    assert event_win.saw_press(_keysym("Shift_L")), "Shift not injected for uppercase A"
    assert event_win.saw_press(_keysym("a")), "a not injected for uppercase A"


def test_type_text_enter(event_win, x11inp):
    event_win.clear()
    x11inp.type_text("\n")
    assert event_win.saw_press(_keysym("Return")), "Return not delivered for \\n in type_text"


# ---------------------------------------------------------------------------
# Tests: throughput — flush() must not add per-keystroke round-trip delay
# ---------------------------------------------------------------------------

def test_flush_not_sync_throughput(event_win, x11inp):
    """50 keystrokes must complete injection in under 200ms.

    With sync() each keystroke waited for an X server round-trip.  On an
    Xvfb loopback that is ~0.5ms → 25ms for 50 keys.  On a remote X display
    it can be 2–20ms per keystroke → 100–1000ms per 50 keys.

    With flush() the cost is just the socket write, sub-millisecond, so
    50 keystrokes should take well under 50ms on any host.  We use 200ms as
    the threshold to give headroom on loaded CI machines.
    """
    event_win.clear()
    t0 = time.perf_counter()
    for _ in range(50):
        x11inp.send_key_event(True, "KeyA", "a")
        x11inp.send_key_event(False, "KeyA", "a")
    elapsed = time.perf_counter() - t0
    # Wait for events to land before asserting timing
    event_win.wait(lambda _: len(event_win.events) >= 90, timeout=2.0)
    assert elapsed < 0.200, (
        f"50 keystrokes took {elapsed*1000:.1f}ms — "
        "likely display.sync() re-introduced (should be flush())"
    )
    # Functional sanity: most presses arrived
    presses = sum(1 for e in event_win.events if e[0] == _X.KeyPress)
    assert presses >= 45, f"Only {presses}/50 keypresses arrived"
