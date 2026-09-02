"""X11GrabBridge — capture via PyAV x11grab, input via python-xlib XTEST.

No VNC protocol; direct X11 framebuffer capture. Eliminates the VNC
request-response cycle and Python ZRLE decode overhead that made the VNC
path slower than noVNC.
"""
import logging
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger("browser_screencast")

# VNC keysym constants shared with the VNC bridge
from mvs.vnc import KEYSYM


class X11GrabBridge:
    """Drop-in bridge replacement: x11grab capture + XTEST input injection."""

    continuous_push = True  # x11grab delivers frames continuously; handler uses seq-based skip

    def __init__(self, cfg):
        self._cfg = cfg
        self._display_str = getattr(cfg, 'x11_display', ':1.0')
        self._lock = threading.Lock()
        self._fb = None        # latest RGB frame as numpy (H, W, 3)
        self._fb_seq = 0
        self._fb_ms = 0
        self._W = 0
        self._H = 0
        self._fbu_count = 0
        self._cached_fb = None
        self._cached_seq = -1
        self.server_clipboard = None
        self.server_clipboard_seq = 0
        # Cursor plane: the pointer is metadata, not pixels. See mvs/cursor.py.
        self._cursor = None
        self._draw_mouse = False
        # python-xlib state (initialised in _start_input)
        self._xd = None         # Xlib Display
        self._root = None
        self._xtest = None
        self._last_buttons = 0  # current button state (VNC bitmask)

    @property
    def dimensions(self):
        with self._lock:
            return self._W, self._H

    def get_frame_if_newer(self, known_seq):
        with self._lock:
            if self._fb is None or self._fb_seq == known_seq:
                return None, known_seq, 0
            if self._fb_seq != self._cached_seq:
                self._cached_fb = self._fb.copy()
                self._cached_seq = self._fb_seq
            return self._cached_fb, self._fb_seq, self._fb_ms

    def get_current_frame(self):
        with self._lock:
            if self._fb is None:
                return None, 0
            if self._fb_seq != self._cached_seq:
                self._cached_fb = self._fb.copy()
                self._cached_seq = self._fb_seq
            return self._cached_fb, self._fb_ms

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------
    def _capture_loop(self):
        from mvs.codec import _av
        fps = float(getattr(self._cfg, 'max_fps', 60))
        # x11grab rate: at max_fps; PyAV will pace frame delivery.
        while True:
            try:
                container = _av.open(
                    self._display_str,
                    format='x11grab',
                    options={
                        'video_size': f'{self._W or 1920}x{self._H or 1080}',
                        'framerate': str(int(fps)),
                        # 0 by design: the cursor travels as metadata and is
                        # composited by the client at the LOCAL pointer, so it
                        # is never a round-trip behind. Only flipped back to 1
                        # when the cursor plane could not start (see start()).
                        'draw_mouse': '1' if self._draw_mouse else '0',
                    })
                vstream = container.streams.video[0]
                W = vstream.codec_context.width
                H = vstream.codec_context.height
                with self._lock:
                    self._W, self._H = W, H
                log.info("x11grab: %dx%d @%dfps on %s", W, H, int(fps), self._display_str)
                for packet in container.demux(vstream):
                    for frame in packet.decode():
                        rgb = frame.to_ndarray(format='rgb24')
                        now_ms = int(time.time() * 1000)
                        with self._lock:
                            self._fb = rgb
                            self._fb_seq += 1
                            self._fb_ms = now_ms
                            self._fbu_count += 1
            except Exception as e:
                log.warning("x11grab error: %s — retry in 1s", e)
                time.sleep(1)

    # ------------------------------------------------------------------
    # Input injection via python-xlib XTEST
    # ------------------------------------------------------------------
    def _start_input(self):
        try:
            from Xlib import X, display as Xd
            from Xlib.ext import xtest as Xtest
            self._xd = Xd.Display(self._display_str)
            self._root = self._xd.screen().root
            self._xtest = Xtest
            self._X = X
            log.info("x11grab input: XTEST ready on %s", self._display_str)
        except Exception as e:
            log.warning("XTEST init failed: %s — input disabled", e)

    def _xsync(self):
        try:
            self._xd.sync()
        except Exception:
            pass

    def send_pointer(self, buttons, x, y):
        if self._xd is None:
            return
        try:
            X = self._X; xd = self._xd; xt = self._xtest
            # Move cursor
            xt.fake_input(xd, X.MotionNotify, False, x=x, y=y)
            # Button state diff
            changed = buttons ^ self._last_buttons
            for bit, xbtn in [(1, 1), (2, 2), (4, 3), (8, 4), (16, 5)]:
                if changed & bit:
                    evt = X.ButtonPress if (buttons & bit) else X.ButtonRelease
                    xt.fake_input(xd, evt, xbtn)
            self._last_buttons = buttons
            self._xsync()
        except Exception as e:
            log.debug("send_pointer err: %s", e)

    def send_key(self, down, keysym):
        if self._xd is None:
            return
        try:
            X = self._X; xd = self._xd; xt = self._xtest
            kc = xd.keysym_to_keycode(keysym)
            if kc:
                evt = X.KeyPress if down else X.KeyRelease
                xt.fake_input(xd, evt, kc)
                self._xsync()
        except Exception as e:
            log.debug("send_key err: %s", e)

    def send_key_event(self, down, code, key):
        ks = KEYSYM.get(code) or KEYSYM.get(key) or (ord(key) if key and len(key) == 1 else None)
        if not ks:
            return False
        self.send_key(down, ks)
        return True

    def send_key_reset(self):
        if self._xd is None:
            return
        try:
            X = self._X; xd = self._xd; xt = self._xtest
            for ks in [0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE9, 0xFFEA]:
                kc = xd.keysym_to_keycode(ks)
                if kc:
                    xt.fake_input(xd, X.KeyRelease, kc)
            if self._last_buttons:
                for bit, xbtn in [(1, 1), (2, 2), (4, 3)]:
                    if self._last_buttons & bit:
                        xt.fake_input(xd, X.ButtonRelease, xbtn)
                self._last_buttons = 0
            self._xsync()
        except Exception as e:
            log.debug("send_key_reset err: %s", e)

    def send_clipboard(self, text):
        try:
            env = {'DISPLAY': self._display_str}
            subprocess.run(
                ['xdotool', 'type', '--clearmodifiers', '--', text],
                env={**__import__('os').environ, **env},
                timeout=2, check=False)
        except Exception as e:
            log.debug("send_clipboard err: %s", e)

    def paste_text(self, text, paste=True, type_chars=False):
        if type_chars and text:
            env = {'DISPLAY': self._display_str}
            try:
                subprocess.run(
                    ['xdotool', 'type', '--clearmodifiers', '--delay', '5', '--', text],
                    env={**__import__('os').environ, **env},
                    timeout=10, check=False)
            except Exception as e:
                log.debug("type_chars err: %s", e)
            return True
        if text:
            self._set_x11_clipboard(text)
        if paste:
            # Ctrl+V
            for ks, down in [(0xFFE3, True), (ord('v'), True),
                              (ord('v'), False), (0xFFE3, False)]:
                self.send_key(down, ks)
                time.sleep(0.01)
        return True

    def _set_x11_clipboard(self, text):
        try:
            env = {**__import__('os').environ, 'DISPLAY': self._display_str}
            subprocess.run(
                ['xdotool', 'type', '--clearmodifiers', '--', text],
                env=env, timeout=2, input=text.encode(), check=False)
        except Exception:
            pass

    def _clipboard_poll(self):
        """Poll X clipboard for server→browser clipboard sync."""
        import hashlib as hl
        last_hash = b''
        while True:
            time.sleep(1.5)
            try:
                env = {**__import__('os').environ, 'DISPLAY': self._display_str}
                r = subprocess.run(
                    ['xdotool', 'getactivewindow'],
                    env=env, capture_output=True, timeout=1)
                # Try xclip or xsel if available
                for cmd in [['xclip', '-selection', 'clipboard', '-o'],
                             ['xsel', '--clipboard', '--output']]:
                    try:
                        r2 = subprocess.run(cmd, env=env, capture_output=True, timeout=1)
                        if r2.returncode == 0:
                            text = r2.stdout.decode('utf-8', errors='replace')
                            h = hl.md5(text.encode()).digest()
                            if h != last_hash:
                                last_hash = h
                                with self._lock:
                                    self.server_clipboard = text
                                    self.server_clipboard_seq += 1
                            break
                    except Exception:
                        pass
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Cursor plane (metadata, not pixels)
    # ------------------------------------------------------------------
    @property
    def cursor_seq(self):
        return self._cursor.cursor_seq if self._cursor else None

    @property
    def cursor_state(self):
        return self._cursor.cursor_state if self._cursor else None

    @property
    def cursor_state_exact(self):
        return self._cursor.cursor_state_exact if self._cursor else None

    def request_cursor_bitmaps(self):
        if self._cursor:
            self._cursor.request_cursor_bitmaps()

    def _start_cursor(self):
        """Start XFixes cursor tracking; decide whether ffmpeg draws the mouse.

        `--draw-mouse auto` (the default) means: don't bake the cursor into the
        frame if we can ship it as metadata instead, but do bake it in on a box
        where XFixes is missing — a baked-in cursor is late, no cursor at all is
        unusable.
        """
        mode = str(getattr(self._cfg, 'draw_mouse', 'auto') or 'auto').lower()
        if mode == 'on':
            self._draw_mouse = True
            return
        from mvs.cursor import make_cursor_tracker
        self._cursor = make_cursor_tracker(self._display_str)
        if mode == 'off':
            self._draw_mouse = False
        else:
            self._draw_mouse = self._cursor is None
        if self._draw_mouse:
            log.warning("no cursor metadata available — falling back to "
                        "draw_mouse=1 (cursor baked into the frame, one "
                        "round-trip late)")

    # ------------------------------------------------------------------
    def start(self):
        self._start_input()
        self._start_cursor()
        # Probe display size before capture loop starts
        try:
            if self._xd:
                g = self._root.get_geometry()
                with self._lock:
                    self._W, self._H = g.width, g.height
        except Exception:
            pass
        threading.Thread(target=self._capture_loop, daemon=True, name='x11grab').start()
        threading.Thread(target=self._clipboard_poll, daemon=True, name='x11grab-clip').start()

    def stop(self):
        if self._cursor is not None:
            try:
                self._cursor.stop()
            except Exception:
                pass
            self._cursor = None
