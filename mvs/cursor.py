"""Client-side cursor plane — the server reports *what the cursor is*, never pixels in the frame.

Every real remote-desktop protocol (RDP, SPICE, VNC's cursor pseudo-encoding)
keeps the pointer out of the video and composites it on the client, because a
cursor baked into the frame arrives a full round-trip late. This module is the
server half of that: it watches the remote cursor and publishes a tiny state
record that the browser turns into a locally-drawn pointer.

The state is deliberately shaped to carry *either* of the two things a capture
backend can know about a cursor:

  • a NAME (X11/XFixes) — mapped here to a CSS cursor keyword, so the browser
    draws the viewer's own native pointer: correct theme, correct DPI, no
    bitmap on the wire at all.
  • a BITMAP + hotspot — the only thing a future PipeWire/xdg-desktop-portal
    (native Wayland) backend can supply, since `SPA_META_Cursor` has no name.

The client prefers the name when present and falls back to the bitmap, so
adding the Wayland path later is additive rather than a wire break.

Change detection is event-driven: XFixesSelectCursorInput makes the X server
send a DisplayCursorNotify whenever the displayed cursor changes, so a stable
cursor costs exactly nothing. See XFixesCursorTracker.
"""
import logging
import os
import select
import threading

log = logging.getLogger("browser_screencast")

# ---------------------------------------------------------------------------
# Wire enum: index → CSS cursor keyword.
#
# The wire carries the index, not the keyword — a cursor change is then ~30
# bytes of JSON, which matters because the shape changes many times a second
# while a pointer crosses links, text fields and window edges.
#
# APPEND ONLY. The list is mirrored in frontend/index.html (CURSOR_CSS) and
# tests/test_cursor_plane.py asserts the two stay identical; reordering it
# would silently give an older client the wrong pointer.
# ---------------------------------------------------------------------------
CURSOR_CSS = [
    "default",        # 0
    "text",           # 1
    "pointer",        # 2
    "crosshair",      # 3
    "move",           # 4
    "wait",           # 5
    "progress",       # 6
    "ew-resize",      # 7
    "ns-resize",      # 8
    "nesw-resize",    # 9
    "nwse-resize",    # 10
    "col-resize",     # 11
    "row-resize",     # 12
    "not-allowed",    # 13
    "help",           # 14
    "grab",           # 15
    "grabbing",       # 16
    "zoom-in",        # 17
    "zoom-out",       # 18
    "all-scroll",     # 19
    "n-resize",       # 20
    "s-resize",       # 21
    "e-resize",       # 22
    "w-resize",       # 23
    "ne-resize",      # 24
    "nw-resize",      # 25
    "se-resize",      # 26
    "sw-resize",      # 27
    "alias",          # 28
    "copy",           # 29
    "context-menu",   # 30
    "cell",           # 31
    "vertical-text",  # 32
    "no-drop",        # 33
]
CSS_INDEX = {name: i for i, name in enumerate(CURSOR_CSS)}
CSS_DEFAULT = 0

# X11 cursor names → CSS keyword. Two naming worlds land here:
#   • the old core-cursor-font names ("left_ptr", "xterm", "sb_h_double_arrow"),
#     still what GTK/Qt/X apps set through libXcursor;
#   • the freedesktop cursor-spec names, which are the CSS names themselves —
#     those need no entry, the fallback in css_index_for_name() catches them.
X11_CURSOR_CSS = {
    "left_ptr": "default", "arrow": "default", "top_left_arrow": "default",
    "x_cursor": "default", "draft_large": "default", "draft_small": "default",
    "right_ptr": "default",
    "xterm": "text", "ibeam": "text",
    "hand1": "pointer", "hand2": "pointer", "pointing_hand": "pointer",
    "hand": "pointer",
    "cross": "crosshair", "cross_reverse": "crosshair", "tcross": "crosshair",
    "diamond_cross": "crosshair", "plus": "cell",
    "fleur": "move", "size_all": "move",
    "watch": "wait",
    "left_ptr_watch": "progress", "half-busy": "progress",
    "sb_h_double_arrow": "ew-resize", "h_double_arrow": "ew-resize",
    "size_hor": "ew-resize",
    "sb_v_double_arrow": "ns-resize", "v_double_arrow": "ns-resize",
    "size_ver": "ns-resize",
    "fd_double_arrow": "nwse-resize", "size_fdiag": "nwse-resize",
    "bd_double_arrow": "nesw-resize", "size_bdiag": "nesw-resize",
    "split_h": "col-resize", "split_v": "row-resize",
    "circle": "not-allowed", "crossed_circle": "not-allowed",
    "forbidden": "not-allowed", "pirate": "not-allowed",
    "dnd-no-drop": "no-drop",
    "question_arrow": "help", "whats_this": "help",
    "openhand": "grab", "closedhand": "grabbing", "dnd-move": "grabbing",
    "dnd-copy": "copy", "dnd-link": "alias", "link": "alias",
    "top_side": "n-resize", "bottom_side": "s-resize",
    "right_side": "e-resize", "left_side": "w-resize",
    "top_left_corner": "nw-resize", "top_right_corner": "ne-resize",
    "bottom_left_corner": "sw-resize", "bottom_right_corner": "se-resize",
    "sb_up_arrow": "n-resize", "sb_down_arrow": "s-resize",
    "sb_left_arrow": "w-resize", "sb_right_arrow": "e-resize",
    "based_arrow_up": "n-resize", "based_arrow_down": "s-resize",
    "center_ptr": "all-scroll",
}


def css_index_for_name(name):
    """X11 cursor name → index into CURSOR_CSS. Unknown/empty → `default`.

    Never raises and never returns something the client cannot render: an
    unrecognised cursor is a normal, frequent event (apps ship custom cursors
    with no name at all) and must degrade to a plain arrow, not to an error.
    """
    if not name:
        return CSS_DEFAULT
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="replace")
    key = name.strip().lower()
    if not key:
        return CSS_DEFAULT
    mapped = X11_CURSOR_CSS.get(key)
    if mapped is not None:
        return CSS_INDEX[mapped]
    # freedesktop themes name cursors exactly like CSS does ("ns-resize", …)
    if key in CSS_INDEX:
        return CSS_INDEX[key]
    if key.replace("_", "-") in CSS_INDEX:
        return CSS_INDEX[key.replace("_", "-")]
    return CSS_DEFAULT


def cursor_message(visible, css=None, png_data_url=None, hotspot=None):
    """Build the `{"t":"cursor", …}` payload.

    Field set is intentionally small and optional-heavy so a backend can supply
    a name (`css`), a bitmap (`img`/`hx`/`hy`), or neither:

        {"t":"cursor","vis":1,"css":2}                    # X11 name → CSS keyword
        {"t":"cursor","vis":1,"img":"data:…","hx":4,"hy":4}   # future Wayland bitmap
        {"t":"cursor","vis":0}                            # remote hid its cursor
    """
    msg = {"t": "cursor", "vis": 1 if visible else 0}
    if visible:
        if css is not None:
            msg["css"] = int(css)
        if png_data_url:
            msg["img"] = png_data_url
            hx, hy = hotspot or (0, 0)
            msg["hx"] = int(hx)
            msg["hy"] = int(hy)
    return msg


class CursorPublisher:
    """Sequence-stamped holder for the current cursor state.

    `seq` only advances when the state the *client* would render changes. The
    XFixes serial is the cheap first filter (no request at all while it is
    unchanged); this is the second: two different cursor serials that both map
    to `text` are one state as far as the wire is concerned, and re-sending
    would be pure waste.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._msg = None
        self._key = None
        self.updates = 0   # state changes published (what goes on the wire)
        self.events = 0    # XFixes notifications seen (before dedupe)

    @property
    def cursor_seq(self):
        with self._lock:
            return self._seq

    @property
    def cursor_state(self):
        with self._lock:
            return dict(self._msg) if self._msg else None

    def publish(self, visible, css=None, png_data_url=None, hotspot=None):
        key = (bool(visible), css, png_data_url, tuple(hotspot) if hotspot else None)
        with self._lock:
            if key == self._key:
                return False
            self._key = key
            self._msg = cursor_message(visible, css, png_data_url, hotspot)
            self._seq += 1
            self.updates += 1
            return True


class XFixesCursorTracker(CursorPublisher):
    """Event-driven X11 cursor watcher (XFixes).

    Opens its *own* X connection: the bridge's display is driven from the input
    thread (XTEST) and a blocking event read on a shared connection would
    interleave badly. Costs one extra socket, nothing else.

    Not a poller. XFixesSelectCursorInput(root, DisplayCursorNotifyMask) makes
    the server push a DisplayCursorNotify on every cursor change, so a still
    pointer generates zero traffic and a moving one is reported the instant it
    changes shape.
    """

    #: XFixes minor opcodes we issue by hand — python-xlib's xfixes module is a
    #: partial implementation and stops at GetCursorImage (opcode 4).
    _OP_GET_CURSOR_IMAGE_AND_NAME = 25

    def __init__(self, display_str=None):
        super().__init__()
        self._display_str = display_str or os.environ.get("DISPLAY") or ":0"
        self._d = None
        self._root = None
        self._opcode = None
        self._req = None
        self._thread = None
        self._stop = threading.Event()
        self._serial = None

    # -- setup ---------------------------------------------------------
    def start(self):
        """Connect + subscribe. Returns True when the cursor plane is live.

        Every failure path (no X, no XFixes, too-old XFixes) returns False
        rather than raising: a backend without cursor metadata is a supported
        configuration, the client just keeps its own fallback pointer.
        """
        try:
            from Xlib import display as Xdisplay
            from Xlib.ext import xfixes
        except Exception as e:
            log.info("cursor plane: python-xlib unavailable (%s)", e)
            return False
        try:
            d = Xdisplay.Display(self._display_str)
            if not d.query_extension("XFIXES"):
                log.info("cursor plane: XFIXES not present on %s", self._display_str)
                return False
            ver = d.xfixes_query_version()
            if int(ver.major_version) < 2:
                # GetCursorImageAndName is XFixes 2+. Older servers still get a
                # cursor plane, just always the generic arrow shape.
                log.info("cursor plane: XFIXES %s.%s — names unavailable",
                         ver.major_version, ver.minor_version)
            self._d = d
            self._root = d.screen().root
            self._opcode = d.display.get_extension_major("XFIXES")
            self._req = _build_get_cursor_image_and_name()
            d.xfixes_select_cursor_input(self._root,
                                         xfixes.XFixesDisplayCursorNotifyMask)
            d.sync()
        except Exception as e:
            log.info("cursor plane: XFixes init failed on %s: %s",
                     self._display_str, e)
            self._d = None
            return False
        self._refresh()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="cursor-plane")
        self._thread.start()
        log.info("cursor plane: XFixes cursor tracking on %s", self._display_str)
        return True

    def stop(self):
        self._stop.set()
        d, self._d = self._d, None
        try:
            if d is not None:
                d.close()
        except Exception:
            pass

    # -- event loop ----------------------------------------------------
    def _loop(self):
        fd = self._d.fileno()
        while not self._stop.is_set():
            try:
                # select() rather than a blocking next_event() so stop() is
                # honoured promptly; the timeout is a liveness tick, not a poll
                # (no X request is issued unless an event arrived).
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    continue
                changed = False
                for _ in range(self._d.pending_events()):
                    ev = self._d.next_event()
                    # NOT isinstance(ev, xfixes.DisplayCursorNotify):
                    # extension_add_subevent() builds a fresh event class per
                    # Display, so the delivered event is never an instance of
                    # the module-level class. Match on the one field only a
                    # cursor notification carries.
                    if hasattr(ev, "cursor_serial"):
                        self.events += 1
                        changed = True
                # Coalesce: a burst of notifications is one refresh. Dragging
                # across a row of links can fire several before we wake up.
                if changed:
                    self._refresh()
            except Exception as e:
                if self._stop.is_set():
                    return
                log.debug("cursor plane loop error: %s", e)
                return

    # -- state ---------------------------------------------------------
    def _refresh(self):
        info = self._read_cursor()
        if info is None:
            return
        serial, visible, name = info
        self._serial = serial
        self.publish(visible, css=css_index_for_name(name))

    def _read_cursor(self):
        """(serial, visible, name) for the currently displayed cursor."""
        d = self._d
        if d is None:
            return None
        try:
            r = self._req(display=d.display, opcode=self._opcode)
            npix = int(r.width) * int(r.height)
            data = r.data
            name = bytes(bytearray(data[npix * 4: npix * 4 + int(r.nbytes)]))
            # The image is premultiplied ARGB, so a fully transparent cursor is
            # all-zero bytes whatever the byte order. That is how a cursor that
            # the remote app has *hidden* looks (games set a 1x1 empty pixmap):
            # no alpha anywhere → nothing to draw → tell the client to hide too.
            visible = any(data[:npix * 4])
            return int(r.cursor_serial), visible, name.decode("utf-8", "replace")
        except Exception as e:
            log.debug("GetCursorImageAndName failed (%s) — falling back", e)
        try:
            img = d.xfixes_get_cursor_image(self._root)
            visible = any((w & 0xFF000000) for w in img.cursor_image)
            return int(img.cursor_serial), visible, ""
        except Exception as e:
            log.debug("GetCursorImage failed: %s", e)
            return None


def _build_get_cursor_image_and_name():
    """XFixesGetCursorImageAndName (XFixes 2+, minor opcode 25).

    python-xlib does not implement it, so the reply is declared here. The
    trailing image+name is read as one raw byte list and sliced by hand:
    rq.Struct cannot express "width*height CARD32s, then nbytes of string".
    """
    from Xlib.protocol import rq

    class GetCursorImageAndName(rq.ReplyRequest):
        _request = rq.Struct(rq.Card8('opcode'),
                             rq.Opcode(XFixesCursorTracker._OP_GET_CURSOR_IMAGE_AND_NAME),
                             rq.RequestLength())
        _reply = rq.Struct(rq.ReplyCode(),
                           rq.Pad(1),
                           rq.Card16('sequence_number'),
                           rq.ReplyLength(),
                           rq.Int16('x'), rq.Int16('y'),
                           rq.Card16('width'), rq.Card16('height'),
                           rq.Card16('xhot'), rq.Card16('yhot'),
                           rq.Card32('cursor_serial'),
                           rq.Card32('cursor_atom'),
                           rq.Card16('nbytes'),
                           rq.Pad(2),
                           rq.List('data', rq.Card8))

    return GetCursorImageAndName


def make_cursor_tracker(display_str=None):
    """Start a cursor tracker, or return None when this host cannot provide one.

    The None case is normal — Windows, native Wayland, VNC, a headless box with
    no XFixes — and callers must treat it as "no cursor metadata", not an error.
    """
    tracker = XFixesCursorTracker(display_str)
    try:
        if tracker.start():
            return tracker
    except Exception as e:  # belt and braces: start() already swallows
        log.info("cursor plane unavailable: %s", e)
    return None
