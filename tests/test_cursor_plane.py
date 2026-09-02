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
                        cursor_message, lookup_css_index, png_data_url)
from mvs.handler import CursorChannel

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


class BitmapFallbackTests(unittest.TestCase):
    """Cursors with no name we recognise travel as pixels instead."""

    def test_unknown_names_are_reported_as_unknown(self):
        self.assertIsNone(lookup_css_index("some_app_custom_cursor"))
        self.assertIsNone(lookup_css_index(""))
        self.assertEqual(lookup_css_index("xterm"), CURSOR_CSS.index("text"))

    def test_premultiplied_alpha_is_divided_back_out(self):
        # Skipping this turns every antialiased cursor edge into a dark halo.
        from PIL import Image
        import base64
        import io
        url = png_data_url(2, 2, [0x80402010, 0x00000000, 0xFFFFFFFF, 0xFF804020])
        self.assertTrue(url.startswith("data:image/png;base64,"))
        img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.getpixel((0, 0)), (128, 64, 32, 128))  # unpremultiplied
        self.assertEqual(img.getpixel((1, 0)), (0, 0, 0, 0))
        self.assertEqual(img.getpixel((1, 1)), (128, 64, 32, 255))

    def test_oversized_or_empty_bitmaps_are_refused(self):
        # Browsers cap CSS cursor images at 128x128; above that a keyword is
        # the only thing that will render.
        self.assertIsNone(png_data_url(0, 0, []))
        self.assertIsNone(png_data_url(256, 256, [0] * (256 * 256)))
        self.assertIsNone(png_data_url(4, 4, [0] * 3))

    def test_bitmap_message_omits_the_keyword(self):
        # The client prefers `css` when present, so a bitmap cursor must not
        # also claim to be an arrow.
        msg = cursor_message(True, css=None, png_data_url="data:image/png;base64,AA",
                             hotspot=(1, 2))
        self.assertNotIn("css", msg)
        self.assertEqual(msg["img"], "data:image/png;base64,AA")


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

    def test_different_serials_with_the_same_shape_reach_the_wire_once(self):
        # Two distinct X cursor objects that both mean "text". The publisher
        # must notice (they are different cursors, and `exact` mode has to see
        # the new one), but the client must not be told twice in native mode.
        pub = CursorPublisher()
        text = css_index_for_name("xterm")
        pub.publish(True, css=text, cid=1)
        seq = pub.cursor_seq
        pub.publish(True, css=text, cid=2)
        self.assertEqual(pub.cursor_seq, seq + 1, "publisher must track identity")

        bridge = _FakeBridge(1, {"t": "cursor", "vis": 1, "id": 1, "css": text})
        ch = CursorChannel()
        self.assertIsNotNone(ch.update(bridge))
        bridge.cursor_seq = 2
        bridge.cursor_state = {"t": "cursor", "vis": 1, "id": 2, "css": text}
        self.assertIsNone(ch.update(bridge), "same shape → no second message")


class _FakeBridge:
    """Bridge stub. Omitting `seq` models a backend with no cursor support."""

    def __init__(self, seq=None, state=None, exact=None):
        self.bitmaps_requested = False
        if seq is not None:
            self.cursor_seq = seq
            self.cursor_state = state
            self.cursor_state_exact = exact if exact is not None else state

    def request_cursor_bitmaps(self):
        self.bitmaps_requested = True


def _bitmap_state(cid, url="data:image/png;base64,AAAA"):
    return {"t": "cursor", "vis": 1, "id": cid, "img": url, "hx": 1, "hy": 2}


class CursorChannelTests(unittest.TestCase):
    def test_bridge_without_cursor_support_is_silent(self):
        # mss / VNC / Windows / headless-without-XFixes: no attribute at all.
        self.assertIsNone(CursorChannel().update(_FakeBridge()))
        self.assertIsNone(CursorChannel().update(object()))

    def test_bridge_with_seq_but_no_state_is_silent(self):
        self.assertIsNone(CursorChannel().update(_FakeBridge(3, None)))

    def test_native_mode_sends_keywords_without_an_id(self):
        bridge = _FakeBridge(1, {"t": "cursor", "vis": 1, "id": 5, "css": 2})
        ch = CursorChannel()
        msg = ch.update(bridge)
        self.assertEqual(msg, {"t": "cursor", "vis": 1, "css": 2})
        # Same seq → nothing more, however many times we are asked (this is the
        # per-frame path: it must not put a byte on the wire).
        for _ in range(1000):
            self.assertIsNone(ch.update(bridge))

    def test_exact_mode_asks_the_bridge_for_bitmaps(self):
        bridge = _FakeBridge(1, {"t": "cursor", "vis": 1, "id": 5, "css": 2},
                             exact=_bitmap_state(5))
        ch = CursorChannel()
        self.assertTrue(ch.set_mode("exact", bridge))
        self.assertTrue(bridge.bitmaps_requested,
                        "server must start producing bitmaps on demand")
        msg = ch.update(bridge)
        self.assertIn("img", msg)
        self.assertEqual((msg["id"], msg["hx"], msg["hy"]), (5, 1, 2))

    def test_exact_mode_sends_a_bitmap_once_per_id(self):
        bridge = _FakeBridge(1, _bitmap_state(5), exact=_bitmap_state(5))
        ch = CursorChannel()
        ch.set_mode("exact", bridge)
        first = ch.update(bridge)
        self.assertIn("img", first)

        # Different cursor…
        bridge.cursor_seq = 2
        bridge.cursor_state_exact = _bitmap_state(6, "data:image/png;base64,BBBB")
        self.assertIn("img", ch.update(bridge))

        # …then back to the first: the client still holds those pixels, so only
        # the id goes out.
        bridge.cursor_seq = 3
        bridge.cursor_state_exact = _bitmap_state(5)
        repeat = ch.update(bridge)
        self.assertNotIn("img", repeat)
        self.assertNotIn("hx", repeat)
        self.assertEqual(repeat, {"t": "cursor", "vis": 1, "id": 5})

    def test_client_cache_miss_resends_the_bitmap(self):
        bridge = _FakeBridge(1, _bitmap_state(5), exact=_bitmap_state(5))
        ch = CursorChannel()
        ch.set_mode("exact", bridge)
        ch.update(bridge)
        bridge.cursor_seq = 2
        bridge.cursor_state_exact = _bitmap_state(5)
        self.assertNotIn("img", ch.update(bridge))
        # Client evicted it and said so.
        ch.forget(5)
        resent = ch.update(bridge)
        self.assertIn("img", resent, "cursor_need must re-send the pixels")

    def test_sent_cache_is_bounded(self):
        bridge = _FakeBridge(1, _bitmap_state(0), exact=_bitmap_state(0))
        ch = CursorChannel()
        ch.set_mode("exact", bridge)
        for i in range(CursorChannel.CACHE_CAP * 3):
            bridge.cursor_seq = i + 1
            bridge.cursor_state_exact = _bitmap_state(i)
            ch.update(bridge)
        self.assertLessEqual(len(ch.sent), CursorChannel.CACHE_CAP)
        # The oldest ids were evicted, so they will be re-sent rather than
        # silently referenced by an id the client may no longer hold either.
        bridge.cursor_seq += 1
        bridge.cursor_state_exact = _bitmap_state(0)
        self.assertIn("img", ch.update(bridge))

    def test_off_mode_stays_on_the_cheap_flavour(self):
        # "off" is a client-side rendering choice; the server keeps sending the
        # cheap keyword so switching back is instant.
        bridge = _FakeBridge(1, {"t": "cursor", "vis": 1, "id": 5, "css": 2},
                             exact=_bitmap_state(5))
        ch = CursorChannel()
        ch.set_mode("off", bridge)
        self.assertFalse(bridge.bitmaps_requested)
        msg = ch.update(bridge)
        self.assertNotIn("img", msg)
        self.assertEqual(msg["css"], 2)

    def test_mode_switch_resends_in_the_new_flavour(self):
        bridge = _FakeBridge(1, {"t": "cursor", "vis": 1, "id": 5, "css": 2},
                             exact=_bitmap_state(5))
        ch = CursorChannel()
        self.assertIn("css", ch.update(bridge))
        self.assertIsNone(ch.update(bridge))       # nothing changed
        ch.set_mode("exact", bridge)
        # Same seq, but the client now needs the other flavour immediately.
        self.assertIn("img", ch.update(bridge))
        ch.set_mode("native", bridge)
        self.assertIn("css", ch.update(bridge))

    def test_unknown_mode_is_ignored(self):
        ch = CursorChannel()
        self.assertFalse(ch.set_mode("wobble", None))
        self.assertEqual(ch.mode, "native")

    def test_backend_with_an_embedded_cursor_is_silent_in_every_mode(self):
        """A Wayland/PipeWire-style bridge: the cursor is burned into the frame
        and there is no metadata at all (`cursor_state()` returns None, no
        `cursor_seq`, no `request_cursor_bitmaps`).

        Every mode must stay silent rather than raise, and -- the part that
        matters -- the client must then keep its own visible pointer instead of
        hiding it with nothing to put in its place.
        """
        class EmbeddedCursorBridge:
            cursor_mode = "embedded"

            def cursor_state(self):
                return None

        for mode in ("native", "exact", "off"):
            bridge = EmbeddedCursorBridge()
            ch = CursorChannel()
            ch.set_mode(mode, bridge)          # must not raise: no bitmap hook
            self.assertIsNone(ch.update(bridge), mode)
            self.assertIsNone(ch.last, mode)

    def test_exact_mode_falls_back_when_there_is_no_bitmap(self):
        # Oversized cursor / no PIL: cursor_state_exact degrades to the keyword.
        keyword = {"t": "cursor", "vis": 1, "id": 5, "css": 2}
        bridge = _FakeBridge(1, keyword, exact=keyword)
        ch = CursorChannel()
        ch.set_mode("exact", bridge)
        msg = ch.update(bridge)
        self.assertEqual(msg["css"], 2)


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


class PlatformBridgeCursorTests(unittest.TestCase):
    """mss capture draws no cursor at all, so it needs the plane most."""

    def _bridge(self):
        from mvs.platform import BackendSelection, NullInput, PlatformBridge

        class Cfg:
            pass
        return PlatformBridge(Cfg(), BackendSelection(capture=object(),
                                                      input=NullInput()))

    def test_no_display_means_no_cursor_metadata(self):
        bridge = self._bridge()
        with _env("DISPLAY", None):
            bridge._start_cursor()
        self.assertIsNone(bridge.cursor_seq)
        self.assertIsNone(bridge.cursor_state)

    def test_x11_session_gets_the_cursor_plane(self):
        bridge = self._bridge()
        pub = CursorPublisher()
        pub.publish(True, css=1)
        with _env("DISPLAY", ":0"), _patched(mvs_cursor, "make_cursor_tracker",
                                             lambda d: pub):
            bridge._start_cursor()
        self.assertEqual(bridge.cursor_state["css"], 1)

    def test_tracker_failure_degrades_instead_of_raising(self):
        bridge = self._bridge()

        def boom(_display):
            raise RuntimeError("no XFixes here")

        with _env("DISPLAY", ":0"), _patched(mvs_cursor, "make_cursor_tracker", boom):
            bridge._start_cursor()
        self.assertIsNone(bridge.cursor_seq)


class _env:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def __enter__(self):
        self.old = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.old
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

    def _pointer_js(self):
        match = re.search(r"// POINTER_INPUT_BEGIN\n(?P<body>.*?)\n// POINTER_INPUT_END",
                          self.source, re.S)
        self.assertIsNotNone(match, "pointer input block not found")
        return match.group("body")

    def _run_node(self, script):
        subprocess.run(["node", "-e", textwrap.dedent(script)], check=True)

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_precedence_and_fallbacks(self):
        self._run_node(f"""
            let _cursorMode='native',_plockActive=false;
            const _styleEl={{textContent:''}};
            const cur={{style:{{display:''}}}};
            const document={{getElementById:()=>_styleEl}};
            {self._plane_js()}
            function assertEq(a,b,label){{
              if(a!==b)throw new Error(label+': expected '+JSON.stringify(b)+', got '+JSON.stringify(a));
            }}

            // 1. No server message ever (Wayland, VNC, Windows, old server):
            //    browser default pointer + generic dot. Never cursor:none --
            //    hiding the local cursor with nothing to replace it would
            //    leave the user with no pointer at all.
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'','no metadata → no rule');
            assertEq(cur.style.display,'','no metadata → fallback dot shown');
            _cursorMode='exact';
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'','exact + no metadata → still no rule');
            assertEq(cur.style.display,'','exact + no metadata → dot still shown');
            _cursorMode='native';

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

            // 5. Bitmap form.
            _remoteCursor={{vis:true,img:'data:image/png;base64,AAAB',hx:3,hy:4}};
            _applyCursorStyle();
            assertEq(_styleEl.textContent,
                     'canvas{{cursor:url("data:image/png;base64,AAAB") 3 4, default}}','bitmap');

            // 6. Anything that is not a plain image data URL is refused, so a
            //    hostile/corrupt payload cannot break out of url().
            _remoteCursor={{vis:true,img:'javascript:alert(1)'}};
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:default}}','bad data url');

            // 7. Mode decides which representation wins when both are present.
            _remoteCursor={{vis:true,css:1,img:'data:image/png;base64,AAAB',hx:0,hy:0}};
            _cursorMode='native';
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:text}}','native prefers keyword');
            _cursorMode='exact';
            _applyCursorStyle();
            assertEq(_styleEl.textContent,
                     'canvas{{cursor:url("data:image/png;base64,AAAB") 0 0, default}}',
                     'exact prefers pixels');

            // 8. exact with only a keyword available (oversized bitmap, or a
            //    backend that has no pixels) still draws something.
            _remoteCursor={{vis:true,css:1}};
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:text}}','exact falls back to keyword');

            // 9. 'off' draws nothing, deliberately.
            _cursorMode='off';
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas,body{{cursor:none}}','off');
            assertEq(cur.style.display,'none','off hides the dot too');

            // 10. Pointer lock hides regardless of mode, and restores after.
            _cursorMode='native';_plockActive=true;
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas,body{{cursor:none}}','pointer lock');
            _plockActive=false;
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:text}}','restored after lock');
        """)

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_client_side_bitmap_cache(self):
        self._run_node(f"""
            let _cursorMode='exact',_plockActive=false;
            const _styleEl={{textContent:''}};
            const cur={{style:{{display:''}}}};
            const document={{getElementById:()=>_styleEl}};
            {self._plane_js()}
            function assertEq(a,b,label){{
              if(a!==b)throw new Error(label+': expected '+JSON.stringify(b)+', got '+JSON.stringify(a));
            }}
            const IMG='data:image/png;base64,AAAB';

            // First sighting carries the pixels and populates the cache.
            assertEq(_cursorFromMessage({{t:'cursor',vis:1,id:7,img:IMG,hx:2,hy:3}}),true,'first sighting');
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:url("'+IMG+'") 2 3, default}}','drawn');

            // A later message for the same id carries only the id.
            assertEq(_cursorFromMessage({{t:'cursor',vis:1,id:7}}),true,'cache hit');
            assertEq(_remoteCursor.img,IMG,'bitmap restored from cache');
            assertEq(_remoteCursor.hx,2,'hotspot restored from cache');
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:url("'+IMG+'") 2 3, default}}','same drawing');

            // An id we do not hold is a miss: report it, and keep drawing.
            assertEq(_cursorFromMessage({{t:'cursor',vis:1,id:99}}),false,'cache miss reported');
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:default}}','miss falls back, never blank');

            // A keyword message with an id is not a cache lookup.
            assertEq(_cursorFromMessage({{t:'cursor',vis:1,id:42,css:1}}),true,'keyword + id');
            _applyCursorStyle();
            assertEq(_styleEl.textContent,'canvas{{cursor:text}}','keyword drawn');

            // The cache is bounded, and evicts least-recently-used.
            for(let i=0;i<CURSOR_CACHE_CAP*3;i++)
              _cursorFromMessage({{t:'cursor',vis:1,id:1000+i,img:IMG,hx:0,hy:0}});
            assertEq(_cursorCache.size,CURSOR_CACHE_CAP,'cache bounded');
            assertEq(_cursorFromMessage({{t:'cursor',vis:1,id:7}}),false,'evicted id now misses');
        """)

    @unittest.skipIf(shutil.which("node") is None, "node not installed")
    def test_cursor_mode_never_changes_pointer_input(self):
        """The input contract: unlocked = absolute, locked = relative.

        Cursor mode decides only what is DRAWN. If a future cursor change ever
        reaches into the pointer handlers, this fails -- which is the point:
        breaking it silently breaks FPS mouselook or desktop clicking.
        """
        self._run_node(f"""
            function assertEq(a,b,label){{
              if(JSON.stringify(a)!==JSON.stringify(b))
                throw new Error(label+': expected '+JSON.stringify(b)+', got '+JSON.stringify(a));
            }}
            const results={{}};
            for(const mode of ['native','exact','off']){{
              for(const locked of [false,true]){{
                // --- environment the pointer handlers run in -----------------
                let _cursorMode=mode;               // the variable under suspicion
                let _plockActive=locked;
                let _plockVX=100,_plockVY=100;
                let _nativeW=1920,_nativeH=1080;
                let scaleX=2,scaleY=2,ox=10,oy=20,mBtn=0;
                const sent=[];
                const send=m=>sent.push(m);
                const _syncMods=()=>{{}};
                const cur={{style:{{}},classList:{{add(){{}},remove(){{}}}}}};
                const ki={{focus(){{}}}};
                const handlers={{}};
                const reg=name=>({{addEventListener:(t,f)=>{{handlers[name+':'+t]=f;}}}});
                const canvas=reg('canvas'), window=reg('window');
                const document={{body:reg('body')}};
                {self._pointer_js()}

                // --- drive it ------------------------------------------------
                handlers['body:mousemove']({{clientX:110,clientY:120,movementX:5,movementY:7,
                                            getModifierState:()=>false}});
                handlers['canvas:mousedown']({{button:0,clientX:110,clientY:120,
                                               preventDefault(){{}},getModifierState:()=>false}});
                handlers['window:mouseup']({{button:0,clientX:110,clientY:120,
                                             preventDefault(){{}}}});
                results[mode+(locked?':locked':':unlocked')]=sent;
              }}
            }}

            // Unlocked: ABSOLUTE, derived from clientX/clientY.
            //   x = (110-10)*2 = 200, y = (120-20)*2 = 200
            const absolute=[{{t:'mm',x:200,y:200,b:0}},
                            {{t:'md',b:0,x:200,y:200}},
                            {{t:'mu',b:0,x:200,y:200}}];
            // Locked: RELATIVE, derived from movementX/movementY.
            //   x = 100 + 5*2 = 110, y = 100 + 7*2 = 114; clientX ignored.
            const relative=[{{t:'mm',x:110,y:114,b:0}},
                            {{t:'md',b:0,x:110,y:114}},
                            {{t:'mu',b:0,x:110,y:114}}];
            for(const mode of ['native','exact','off']){{
              assertEq(results[mode+':unlocked'],absolute,mode+' unlocked must stay absolute');
              assertEq(results[mode+':locked'],relative,mode+' locked must stay relative');
            }}
        """)


@unittest.skipIf(shutil.which("Xvfb") is None, "Xvfb not installed")
class XFixesTrackerTests(unittest.TestCase):
    """End-to-end against a real X server: event-driven, named, deduplicated."""

    DISPLAY = ":91"

    @staticmethod
    def _connect(display_str, attempts=5):
        """Open an X connection, retrying briefly.

        Rapidly opening and closing X connections makes the server reset one
        every so often — reproducible with nothing but a bare python-xlib
        open/close loop, no project code involved, which is why this lives in
        the harness rather than in mvs/cursor.py. The server opens exactly one
        connection per session and never does this.
        """
        from Xlib import display
        last = None
        for _ in range(attempts):
            try:
                return display.Display(display_str)
            except Exception as e:
                last = e
                time.sleep(0.1)
        raise last

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
                cls._connect(cls.DISPLAY, attempts=1).close()
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
        from Xlib.protocol import rq

        class SetCursorName(rq.Request):
            _request = rq.Struct(rq.Card8('opcode'), rq.Opcode(23),
                                 rq.RequestLength(), rq.Cursor('cursor'),
                                 rq.LengthOf('name', 2), rq.Pad(2),
                                 rq.String8('name'))

        self.d = self._connect(self.DISPLAY)
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

    def _set_unnamed_cursor(self, glyph):
        c = self.font.create_glyph_cursor(self.font, glyph, glyph + 1,
                                          (0, 0, 0), (65535, 65535, 65535))
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

        self.assertGreater(tracker.events, 0, "no XFixes events were delivered")

        # Re-selecting the same shape as a NEW cursor object is a real identity
        # change (exact mode must see it), but it renders identically, so the
        # per-client channel must not put a second message on the wire.
        ch = CursorChannel()
        self.assertIsNotNone(ch.update(tracker))
        self._set_named_cursor(60, "hand2")
        time.sleep(0.5)
        self.assertIsNone(ch.update(tracker), "same shape → no second message")

    def test_exact_mode_gets_pixels_even_for_a_named_cursor(self):
        # The whole point of `exact`: a keyword would render the VIEWER's
        # I-beam, not the remote's.
        from mvs.cursor import make_cursor_tracker
        tracker = make_cursor_tracker(self.DISPLAY)
        self.assertIsNotNone(tracker)
        self.addCleanup(tracker.stop)
        self._set_named_cursor(152, "xterm")
        self._wait(tracker, lambda s: s.get("css") == CURSOR_CSS.index("text"))
        # Nothing is encoded until a client asks — that is what keeps the
        # default mode free.
        self.assertEqual(tracker.cursor_state_exact.get("css"),
                         CURSOR_CSS.index("text"))
        tracker.request_cursor_bitmaps()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            exact = tracker.cursor_state_exact
            if "img" in exact:
                break
            time.sleep(0.05)
        else:
            self.fail("exact flavour never produced a bitmap")
        self.assertTrue(exact["img"].startswith("data:image/png;base64,"))
        self.assertIn("id", exact)
        # …and the native flavour is still the cheap keyword.
        self.assertEqual(tracker.cursor_state.get("css"), CURSOR_CSS.index("text"))

    def test_nameless_cursor_falls_back_to_a_bitmap(self):
        from mvs.cursor import make_cursor_tracker
        tracker = make_cursor_tracker(self.DISPLAY)
        self.assertIsNotNone(tracker)
        self.addCleanup(tracker.stop)
        self._set_named_cursor(152, "xterm")
        self._wait(tracker, lambda s: s.get("css") == CURSOR_CSS.index("text"))
        # A cursor that no app bothered to name (or one named something we have
        # no keyword for) must arrive as pixels, not as a wrong arrow.
        self._set_unnamed_cursor(58)   # XC_gumby: nothing like it in CSS
        state = self._wait(tracker, lambda s: "img" in s)
        self.assertNotIn("css", state)
        self.assertTrue(state["img"].startswith("data:image/png;base64,"))
        self.assertIn("hx", state)
        self.assertIn("hy", state)

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
