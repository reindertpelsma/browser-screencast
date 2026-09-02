#!/usr/bin/env python3
"""
browser-screencast: browser-based Linux/Windows remote desktop over SSH.

ssh -L 6081:localhost:6081 user@host
open http://localhost:6081/?token=...
"""
import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import sys

log = logging.getLogger("browser_screencast")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def parse_args():
    p = argparse.ArgumentParser(description="browser-screencast server")
    p.add_argument("--listen", default=os.environ.get("LISTEN", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "6081")))
    p.add_argument("--fps", type=int, default=int(os.environ.get("FPS", "20")),
                   help="Initial/max responsive fps (default 20)")
    p.add_argument("--max-fps", type=int, default=int(os.environ.get("MAX_FPS", "60")),
                   help="Upper fps limit when bandwidth allows (default 60)")
    p.add_argument("--codec", choices=["auto", "h264", "h265", "av1", "vp9", "jpeg"],
                   default=os.environ.get("CODEC", "h264"),
                   help="Initial codec target (default h264; browser caps may override)")
    p.add_argument("--initial-bitrate", type=int,
                   default=int(os.environ.get("INITIAL_BITRATE", "1000000")),
                   help="Initial encoder bitrate in bits/sec (default 1000000)")
    p.add_argument("--password", default=os.environ.get("BROWSER_SCREENCAST_PASSWORD", ""),
                   help="Optional access token for the URL (?token=...)")
    p.add_argument("--generate-token", action="store_true",
                   help="Generate and print a token if --password is not set")

    p.add_argument("--capture", choices=["auto", "x11", "x11grab", "wayland", "windows", "mss", "vnc", "none"],
                   default=os.environ.get("CAPTURE_MODE", "auto"),
                   help="Capture backend (default auto)")
    p.add_argument("--input", choices=["auto", "x11", "x11grab", "windows", "vnc", "none"],
                   default=os.environ.get("INPUT_MODE", "auto"),
                   help="Input backend (default auto)")
    p.add_argument("--monitor", type=int, default=int(os.environ.get("MONITOR", "1")),
                   help="mss monitor index; 1 is the primary physical monitor")

    p.add_argument("--vnc-host", default=os.environ.get("VNC_HOST", "127.0.0.1"))
    p.add_argument("--vnc-port", type=int, default=int(os.environ.get("VNC_PORT", "5900")))
    p.add_argument("--vnc-pass", default=os.environ.get("VNC_PASS", ""))
    p.add_argument("--vnc-only", action="store_true",
                   help="Use VNC for both capture and input")
    p.add_argument("--display", default=os.environ.get("DISPLAY", ":1.0"),
                   help="X display for x11grab capture (default :1.0)")
    p.add_argument("--draw-mouse", choices=["auto", "on", "off"],
                   default=os.environ.get("DRAW_MOUSE", "auto"),
                   help="Bake the cursor into the captured frame. Default auto: "
                        "off when the cursor can be sent as metadata and drawn "
                        "client-side (no round-trip lag), on only as a fallback "
                        "when XFixes cursor tracking is unavailable.")
    p.add_argument("--headless", action="store_true",
                   default=_truthy_env("HEADLESS"),
                   help="Start an Xvfb display and lightweight window manager on Linux")
    p.add_argument("--headless-display", default=os.environ.get("HEADLESS_DISPLAY", ":99"),
                   help="Xvfb display for --headless (default :99)")
    p.add_argument("--headless-size", default=os.environ.get("HEADLESS_SIZE", "1920x1080x24"),
                   help="Xvfb screen spec for --headless (default 1920x1080x24)")
    p.add_argument("--headless-wm", default=os.environ.get("HEADLESS_WM", "auto"),
                   help="Window manager for --headless: auto, none, openbox, xfwm4, fluxbox, i3")
    p.add_argument("--no-audio", action="store_true",
                   help="Disable the /audio websocket")
    p.add_argument("--print-caps", action="store_true",
                   help="Print detected server codec capabilities and exit")
    args = p.parse_args()

    # x11grab display
    args.x11_display = args.display

    # Compatibility attributes consumed by the portable RFB client.
    args.macos_user = ""
    args.macos_pass = ""
    args.manage_screensharingd = False

    if args.vnc_only:
        args.capture = "vnc"
        args.input = "vnc"
    if args.codec == "auto":
        args.codec = "h264"
    if args.generate_token and not args.password:
        args.password = secrets.token_urlsafe(24)
    return args


async def _main(cfg, bridge):
    from websockets import serve
    from mvs.handler import make_http_handler, make_ws_handler

    http_handler = make_http_handler(cfg, bridge)
    ws_handler = make_ws_handler(cfg, bridge)

    log.info("Listening %s:%d codec=%s max_fps=%d capture=%s input=%s",
             cfg.listen, cfg.port, cfg.codec, cfg.max_fps, cfg.capture, cfg.input)
    async with serve(lambda ws: ws_handler(ws), cfg.listen, cfg.port,
                     process_request=http_handler,
                     max_size=None, compression=None):
        await asyncio.Future()


def _build_bridge(cfg):
    if cfg.capture == "vnc":
        from mvs.vnc import VNCBridge
        if not cfg.vnc_pass:
            log.warning("VNC selected without --vnc-pass; only unauthenticated VNC servers will work")
        bridge = VNCBridge(cfg)
        bridge.start()
        return bridge

    if cfg.capture == "x11grab":
        from mvs.x11grab import X11GrabBridge
        # Allow --display override; fall back to DISPLAY env var or :1
        import os
        if not hasattr(cfg, 'x11_display') or not cfg.x11_display:
            cfg.x11_display = os.environ.get('DISPLAY', ':1.0')
        bridge = X11GrabBridge(cfg)
        bridge.start()
        return bridge

    from mvs.platform import create_platform_bridge, detect_capture_mode
    cfg.capture = detect_capture_mode(cfg.capture)
    bridge = create_platform_bridge(cfg)
    if not bridge.start():
        raise RuntimeError(f"capture backend failed to start: {cfg.capture}")
    return bridge


def main():
    cfg = parse_args()
    if sys.platform == "darwin":
        log.error("macOS detected. Use macscreencast instead: "
                  "https://github.com/reindertpelsma/macscreencast")
        raise SystemExit(2)

    from mvs.codec import _AV_OK, probe_server_codecs

    if cfg.print_caps:
        print(json.dumps(probe_server_codecs(), indent=2, sort_keys=True))
        return

    log.info("PyAV: %s", "yes" if _AV_OK else "NO — pip install av for video codecs")
    log.info("Server codec caps: %s", probe_server_codecs())

    bridge = None
    headless = None
    try:
        if (cfg.headless or
                (sys.platform.startswith("linux") and cfg.capture == "auto"
                 and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
                 and shutil.which("Xvfb"))):
            from mvs.headless import HeadlessSession
            headless = HeadlessSession(cfg)
            headless.start()
            if cfg.capture == "auto":
                cfg.capture = "x11"
            if cfg.input == "auto":
                cfg.input = "x11"

        bridge = _build_bridge(cfg)
        if cfg.password:
            log.info("-" * 60)
            log.info("Token:  %s", cfg.password)
            log.info("URL:    http://localhost:%d/?token=%s", cfg.port, cfg.password)
            log.info("SSH:    ssh -L %d:localhost:%d user@<host>", cfg.port, cfg.port)
            log.info("-" * 60)
        else:
            log.warning("No access token set. Bind remains %s; use --password or --generate-token.",
                        cfg.listen)

        asyncio.run(_main(cfg, bridge))
    finally:
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass
        if headless is not None:
            headless.stop()


if __name__ == "__main__":
    main()
