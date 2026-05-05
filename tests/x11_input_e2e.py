#!/usr/bin/env python3
"""End-to-end X11 input test through the browser-screencast websocket."""

import asyncio
import json
import os
import sys
import threading
import time

from Xlib import X, XK, display

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 6081
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
WS_URL = f"ws://{HOST}:{PORT}/" + (f"?token={TOKEN}" if TOKEN else "")


class EventWindow:
    def __init__(self):
        self.d = display.Display(os.environ.get("DISPLAY"))
        self.screen = self.d.screen()
        self.win = self.screen.root.create_window(
            0, 0, 420, 320, 0,
            self.screen.root_depth,
            X.InputOutput,
            X.CopyFromParent,
            background_pixel=self.screen.white_pixel,
            event_mask=(X.KeyPressMask | X.KeyReleaseMask |
                        X.ButtonPressMask | X.ButtonReleaseMask |
                        X.PointerMotionMask | X.FocusChangeMask),
            override_redirect=True,
        )
        self.win.set_wm_name("browser-screencast-input-e2e")
        self.win.map()
        self.win.configure(stack_mode=X.Above)
        self.d.sync()
        self.win.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.d.sync()
        self.events = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.win.destroy()
        self.d.sync()

    def _run(self):
        while not self._stop.is_set():
            while self.d.pending_events():
                ev = self.d.next_event()
                if ev.type in (X.KeyPress, X.KeyRelease):
                    self.events.append(("key", ev.type, ev.detail))
                elif ev.type in (X.ButtonPress, X.ButtonRelease):
                    self.events.append(("button", ev.type, ev.detail))
                elif ev.type == X.MotionNotify:
                    self.events.append(("motion", ev.event_x, ev.event_y))
            time.sleep(0.005)

    def keycode(self, keysym):
        return int(self.d.keysym_to_keycode(int(keysym)))

    def seen(self, pred):
        return any(pred(e) for e in self.events)


async def _discard(ws):
    try:
        async for _ in ws:
            pass
    except Exception:
        pass


async def main():
    try:
        import websockets
    except ImportError:
        print("SKIP — websockets not installed")
        return

    ew = EventWindow()
    ew.start()
    failures = []
    try:
        async with websockets.connect(WS_URL, open_timeout=10, max_size=16 * 1024 * 1024) as ws:
            reader = asyncio.create_task(_discard(ws))
            await ws.send(json.dumps({
                "t": "caps",
                "webcodecs": True,
                "codecs": ["h264"],
                "codecCaps": {"h264": {"hw": False, "sw": True}},
                "w": 420,
                "h": 320,
            }))
            await asyncio.sleep(0.2)

            await ws.send(json.dumps({"t": "mm", "x": 80, "y": 90}))
            await ws.send(json.dumps({"t": "md", "b": 0, "x": 80, "y": 90}))
            await ws.send(json.dumps({"t": "mu", "b": 0, "x": 80, "y": 90}))
            await ws.send(json.dumps({"t": "sc", "dx": -1, "dy": 2, "x": 80, "y": 90}))
            await ws.send(json.dumps({"t": "kd", "code": "KeyA", "k": "a"}))
            await ws.send(json.dumps({"t": "ku", "code": "KeyA", "k": "a"}))
            await ws.send(json.dumps({"t": "kd", "code": "!", "k": "!"}))
            await ws.send(json.dumps({"t": "ku", "code": "!", "k": "!"}))
            await asyncio.sleep(0.8)

            reader.cancel()

        a_code = ew.keycode(XK.string_to_keysym("a"))
        shift_code = ew.keycode(XK.XK_Shift_L)
        one_code = ew.keycode(XK.string_to_keysym("1"))

        checks = [
            ("pointer motion", lambda: ew.seen(lambda e: e[0] == "motion" and 0 <= e[1] <= 420 and 0 <= e[2] <= 320)),
            ("left click press", lambda: ew.seen(lambda e: e == ("button", X.ButtonPress, 1))),
            ("left click release", lambda: ew.seen(lambda e: e == ("button", X.ButtonRelease, 1))),
            ("vertical scroll", lambda: ew.seen(lambda e: e == ("button", X.ButtonPress, 5))),
            ("horizontal scroll", lambda: ew.seen(lambda e: e == ("button", X.ButtonPress, 6))),
            ("a key press", lambda: ew.seen(lambda e: e == ("key", X.KeyPress, a_code))),
            ("a key release", lambda: ew.seen(lambda e: e == ("key", X.KeyRelease, a_code))),
            ("shifted symbol shift", lambda: ew.seen(lambda e: e == ("key", X.KeyPress, shift_code))),
            ("shifted symbol base", lambda: ew.seen(lambda e: e == ("key", X.KeyPress, one_code))),
        ]
        for name, pred in checks:
            if not pred():
                failures.append(name)
    finally:
        ew.stop()

    if failures:
        print("FAIL — missing events:")
        for f in failures:
            print(f"  - {f}")
        print(f"Recorded events: {ew.events[:80]}")
        sys.exit(1)
    print("PASS — X11 pointer, scroll, key, and shifted-symbol input delivered")


asyncio.run(main())
