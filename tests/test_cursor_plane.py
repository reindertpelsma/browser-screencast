#!/usr/bin/env python3
"""Client-side cursor plane: the pointer is metadata, not pixels in the frame.

Covers the three things that can silently rot:
  • change detection — nothing goes on the wire unless the rendered cursor
    actually changed (and nothing at all per frame),
  • graceful degradation — a backend with no cursor metadata must produce
    silence, not an exception,
  • the server↔client enum, which is the wire format and must not drift.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mvs import cursor as mvs_cursor
from mvs.cursor import (CURSOR_CSS, CursorPublisher, css_index_for_name,
                        cursor_message)
from mvs.handler import cursor_update

ROOT = Path(__file__).resolve().parents[1]


class CursorNameMappingTests(unittest.TestCase):
    def _css(self, name):
        return CURSOR_CSS[css_index_for_name(name)]

    def test_core_font_names_map_to_css_keywords(self):
        self.assertEqual(self._css("left_ptr"), "default")
        self.assertEqual(self._css("xterm"), "text")
        self.assertEqual(self._css("hand2"), "pointer")
        self.assertEqual(self._css("hand1"), "pointer")
        self.assertEqual(self._css("sb_h_double_arrow"), "ew-resize")
        self.assertEqual(self._css("sb_v_double_arrow"), "ns-resize")
        self.assertEqual(self._css("fleur"), "move")
        self.assertEqual(self._css("watch"), "wait")
        self.assertEqual(self._css("crosshair"), "crosshair")

    def test_freedesktop_theme_names_are_already_css_names(self):
        # Modern cursor themes name cursors exactly like CSS does; those must
        # pass through rather than fall back to an arrow.
        self.assertEqual(self._css("ns-resize"), "ns-resize")
        self.assertEqual(self._css("not-allowed"), "not-allowed")
        self.assertEqual(self._css("zoom-in"), "zoom-in")

    def test_unknown_and_missing_names_fall_back_to_default(self):
        for name in ("", None, b"", "some_app_custom_cursor", "   "):
            self.assertEqual(css_index_for_name(name), 0, name)
        self.assertEqual(self._css("left_ptr".upper()), "default")
        self.assertEqual(css_index_for_name(b"xterm"), CURSOR_CSS.index("text"))

    def test_frontend_enum_matches_server_enum(self):
        html = (ROOT / "frontend" / "index.html").read_text()
        match = re.search(r"const CURSOR_CSS=\[(.*?)\];", html, re.S)
        self.assertIsNotNone(match, "CURSOR_CSS not found in frontend")
        js = re.findall(r"'([^']+)'", match.group(1))
        self.assertEqual(js, CURSOR_CSS,
                         "wire enum drifted: an old client would render the "
                         "wrong pointer shape")


class CursorMessageTests(unittest.TestCase):
    def test_name_form_is_tiny(self):
        msg = cursor_message(True, css=2)
        self.assertEqual(msg, {"t": "cursor", "vis": 1, "css": 2})
        self.assertLess(len(json.dumps(msg, separators=(",", ":"))), 40)

    def test_hidden_carries_no_shape(self):
        msg = cursor_message(False, css=2)
        self.assertEqual(msg, {"t": "cursor", "vis": 0})

    def test_bitmap_form_carries_hotspot(self):
        # The shape a nameless backend (native Wayland/PipeWire SPA_META_Cursor)
        # would have to use. Same message type, different optional fields.
        msg = cursor_message(True, png_data_url="data:image/png;base64,AAAA",
                             hotspot=(4, 5))
        self.assertEqual(msg["img"], "data:image/png;base64,AAAA")
        self.assertEqual((msg["hx"], msg["hy"]), (4, 5))


class ChangeDetectionTests(unittest.TestCase):
    def test_identical_state_does_not_bump_seq(self):
        pub = CursorPublisher()
        self.assertTrue(pub.publish(True, css=1))
        seq = pub.cursor_seq
        for _ in range(50):
            self.assertFalse(pub.publish(True, css=1))
        self.assertEqual(pub.cursor_seq, seq)
        self.assertEqual(pub.updates, 1)

    def test_shape_and_visibility_changes_bump_seq(self):
        pub = CursorPublisher()
        pub.publish(True, css=0)
        first = pub.cursor_seq
        self.assertTrue(pub.publish(True, css=1))
        self.assertEqual(pub.cursor_seq, first + 1)
        self.assertTrue(pub.publish(False))
        self.assertEqual(pub.cursor_state, {"t": "cursor", "vis": 0})
        self.assertEqual(pub.cursor_seq, first + 2)

    def test_different_serials_with_the_same_shape_are_one_state(self):
        # Two distinct X cursors that both mean "text" are one wire state; the
        # XFixes serial is only the cheap first-level filter.
        pub = CursorPublisher()
        pub.publish(True, css=css_index_for_name("xterm"))
        seq = pub.cursor_seq
        pub.publish(True, css=css_index_for_name("ibeam"))
        self.assertEqual(pub.cursor_seq, seq)


class _FakeBridge:
    def __init__(self, seq=None, state=None):
        if seq is not None:
            self.cursor_seq = seq
            self.cursor_state = state


class HandlerCursorUpdateTests(unittest.TestCase):
    def test_bridge_without_cursor_support_is_silent(self):
        # mss / VNC / Windows / headless-without-XFixes: no attribute at all.
        self.assertIsNone(cursor_update(_FakeBridge(), None))
        self.assertIsNone(cursor_update(object(), 7))

    def test_bridge_with_seq_but_no_state_is_silent(self):
        self.assertIsNone(cursor_update(_FakeBridge(3, None), None))

    def test_update_sent_once_per_change(self):
        bridge = _FakeBridge(1, {"t": "cursor", "vis": 1, "css": 0})
        known = None
        got = cursor_update(bridge, known)
        self.assertIsNotNone(got)
        known, msg = got
        self.assertEqual(msg["css"], 0)
        # Same seq → nothing more, however many times we are asked (this is the
        # per-frame path: it must not put a byte on the wire).
        for _ in range(1000):
            self.assertIsNone(cursor_update(bridge, known))
        bridge.cursor_seq = 2
        bridge.cursor_state = {"t": "cursor", "vis": 1, "css": 1}
        self.assertIsNotNone(cursor_update(bridge, known))


class CaptureIntegrationTests(unittest.TestCase):
    """x11grab must stop baking the cursor into the frame."""

    def _bridge(self, **cfg_kwargs):
        from mvs.x11grab import X11GrabBridge

        class Cfg:
            x11_display = ":99"
            max_fps = 60
        cfg = Cfg()
        for k, v in cfg_kwargs.items():
            setattr(cfg, k, v)
        return X11GrabBridge(cfg)

    def test_draw_mouse_is_off_when_the_cursor_plane_is_live(self):
        bridge = self._bridge()
        with _patched(mvs_cursor, "make_cursor_tracker", lambda d: CursorPublisher()):
            bridge._start_cursor()
        self.assertFalse(bridge._draw_mouse)
        self.assertIsNotNone(bridge.cursor_seq)

    def test_draw_mouse_falls_back_when_no_cursor_metadata(self):
        # A box with no XFixes gets the old baked-in cursor rather than none.
        bridge = self._bridge()
        with _patched(mvs_cursor, "make_cursor_tracker", lambda d: None):
            bridge._start_cursor()
        self.assertTrue(bridge._draw_mouse)
        self.assertIsNone(bridge.cursor_seq)
        self.assertIsNone(bridge.cursor_state)

    def test_draw_mouse_can_be_forced(self):
        bridge = self._bridge(draw_mouse="on")
        bridge._start_cursor()
        self.assertTrue(bridge._draw_mouse)
        bridge = self._bridge(draw_mouse="off")
        with _patched(mvs_cursor, "make_cursor_tracker", lambda d: None):
            bridge._start_cursor()
        self.assertFalse(bridge._draw_mouse)

    def test_capture_options_use_the_flag_not_a_constant(self):
        src = (ROOT / "mvs" / "x11grab.py").read_text()
        self.assertIn("'draw_mouse': '1' if self._draw_mouse else '0'", src)


class _patched:
    def __init__(self, obj, attr, value):
        self.obj, self.attr, self.value = obj, attr, value

    def __enter__(self):
        self.old = getattr(self.obj, self.attr)
        setattr(self.obj, self.attr, self.value)

    def __exit__(self, *exc):
        setattr(self.obj, self.attr, self.old)
        return False


class FrontendCursorPlaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "frontend" / "index.html").read_text()

    def _plane_js(self):
        match = re.search(r"// CURSOR_PLANE_BEGIN\n(?P<body>.*?)\n// CURSOR_PLANE_END",
                          self.source, re.S)
        self.assertIsNotNone(match, "cursor plane block not found")
        return match.group("body")

    def test_cursor_is_never_an_inline_style(self):
        # resize() assigns canvas.style.cssText wholesale, so an inline cursor
        # silently disappears on the first window resize.
        compact = re.sub(r"\s+", "", self.source)
        self.assertNotIn("canvas.style.cursor=", compact)
        self.assertIn("<styleid=\"cursor-plane\">", compact)

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_precedence_and_fallbacks(self):
        script = textwrap.dedent(f"""
            let _hideCursor=false,_plockActive=false;
            const _styleEl={{textContent:''}};
            const cur={{style:{{display:''}}}};
            const document={{getElementById:()=>_styleEl}};
            {self._plane_js()}
            function assertEq(a,b,label){{
              if(a!==b)throw new Error(label+': expected '+JSON.stringify(b)+', got '+JSON.stringify(a));
            }}

            // 1. No server message ever: browser default + generic dot visible.
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'','no metadata → no rule');
            assertEq(cur.style.display,'','no metadata → fallback dot shown');

            // 2. Named shape → the viewer's own native pointer, dot retired.
            _remoteCursor={{vis:true,css:2}};
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:pointer}}','css index');
            assertEq(cur.style.display,'none','real cursor replaces the dot');

            // 3. Remote hid its cursor (an FPS grabbing the mouse).
            _remoteCursor={{vis:false,css:2}};
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:none}}','hidden remote cursor');

            // 4. Unknown index from a newer server → arrow, not a broken rule.
            _remoteCursor={{vis:true,css:9999}};
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:default}}','unknown index');

            // 5. Bitmap form (what a native-Wayland backend would send).
            _remoteCursor={{vis:true,img:'data:image/png;base64,AAAB',hx:3,hy:4}};
            _applyCursorStyle();
            assertEq(_styleEl.textContent,
                     'canvas{{cursor:url("data:image/png;base64,AAAB") 3 4, default}}','bitmap');

            // 6. Anything that is not a plain image data URL is refused, so a
            //    hostile/corrupt payload cannot break out of url().
            _remoteCursor={{vis:true,img:'javascript:alert(1)'}};
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:default}}','bad data url');

            // 7. Manual "Hide cursor" toggle overrides everything.
            _remoteCursor={{vis:true,css:1}};
            _hideCursor=true;
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas,body{{cursor:none}}','manual override');
            assertEq(cur.style.display,'none','manual override hides dot');

            // 8. Pointer lock does the same without touching the toggle.
            _hideCursor=false;_plockActive=true;
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas,body{{cursor:none}}','pointer lock');
            _plockActive=false;
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:text}}','restored after lock');
        """)
        subprocess.run(["node", "-e", script], check=True)


@unittest.skipIf(shutil.which("Xvfb") is None, "Xvfb not installed")
class XFixesTrackerTests(unittest.TestCase):
    """End-to-end against a real X server: event-driven, named, deduplicated."""

    DISPLAY = ":91"

    @classmethod
    def setUpClass(cls):
        try:
            from Xlib import display  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("python-xlib not installed")
        cls.xvfb = subprocess.Popen(
            ["Xvfb", cls.DISPLAY, "-screen", "0", "640x480x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            time.sleep(0.1)
            try:
                from Xlib import display
                display.Display(cls.DISPLAY).close()
                break
            except Exception:
                continue
        else:
            cls.xvfb.terminate()
            raise unittest.SkipTest("Xvfb did not come up")

    @classmethod
    def tearDownClass(cls):
        cls.xvfb.terminate()
        cls.xvfb.wait(timeout=5)

    def setUp(self):
        from Xlib import display
        from Xlib.protocol import rq

        class SetCursorName(rq.Request):
            _request = rq.Struct(rq.Card8('opcode'), rq.Opcode(23),
                                 rq.RequestLength(), rq.Cursor('cursor'),
                                 rq.LengthOf('name', 2), rq.Pad(2),
                                 rq.String8('name'))

        self.d = display.Display(self.DISPLAY)
        self.d.xfixes_query_version()
        self.op = self.d.display.get_extension_major('XFIXES')
        self.font = self.d.open_font('cursor')
        self._SetCursorName = SetCursorName
        self.addCleanup(self.d.close)

    def _set_named_cursor(self, glyph, name):
        c = self.font.create_glyph_cursor(self.font, glyph, glyph + 1,
                                          (0, 0, 0), (65535, 65535, 65535))
        self._SetCursorName(display=self.d.display, opcode=self.op,
                            cursor=c, name=name.encode())
        self.d.screen().root.change_attributes(cursor=c)
        self.d.sync()

    def _set_invisible_cursor(self):
        root = self.d.screen().root
        pm = root.create_pixmap(1, 1, 1)
        c = pm.create_cursor(pm, (0, 0, 0), (0, 0, 0), 0, 0)
        root.change_attributes(cursor=c)
        self.d.sync()

    def _wait(self, tracker, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = tracker.cursor_state
            if state and predicate(state):
                return state
            time.sleep(0.05)
        self.fail("cursor state never became %s (last=%r)"
                  % (predicate, tracker.cursor_state))

    def test_named_cursor_changes_arrive_as_events(self):
        from mvs.cursor import make_cursor_tracker
        tracker = make_cursor_tracker(self.DISPLAY)
        self.assertIsNotNone(tracker, "XFixes tracker failed to start on Xvfb")
        self.addCleanup(tracker.stop)

        self._set_named_cursor(152, "xterm")          # XC_xterm
        self._wait(tracker, lambda s: s.get("css") == CURSOR_CSS.index("text"))
        self._set_named_cursor(60, "hand2")           # XC_hand2
        self._wait(tracker, lambda s: s.get("css") == CURSOR_CSS.index("pointer"))

        # Re-selecting a cursor with the same shape fires an XFixes event but
        # must NOT produce a new wire message.
        seq = tracker.cursor_seq
        updates = tracker.updates
        self._set_named_cursor(60, "hand2")
        time.sleep(0.5)
        self.assertEqual(tracker.cursor_seq, seq)
        self.assertEqual(tracker.updates, updates)
        self.assertGreater(tracker.events, 0, "no XFixes events were delivered")

    def test_transparent_cursor_reports_hidden(self):
        from mvs.cursor import make_cursor_tracker
        self._set_named_cursor(152, "xterm")
        tracker = make_cursor_tracker(self.DISPLAY)
        self.assertIsNotNone(tracker)
        self.addCleanup(tracker.stop)
        self._wait(tracker, lambda s: s["vis"] == 1)
        self._set_invisible_cursor()
        self._wait(tracker, lambda s: s["vis"] == 0)

    def test_tracker_returns_none_when_there_is_no_display(self):
        from mvs.cursor import make_cursor_tracker
        self.assertIsNone(make_cursor_tracker(":nonexistent-display"))


if __name__ == "__main__":
    unittest.main()
