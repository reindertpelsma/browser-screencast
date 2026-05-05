#!/usr/bin/env python3
"""End-to-end X11 clipboard sync test through the websocket."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 6081
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
WS_URL = f"ws://{HOST}:{PORT}/" + (f"?token={TOKEN}" if TOKEN else "")


def _clip_get():
    if shutil.which("xclip"):
        return subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                              capture_output=True, text=True, timeout=2).stdout
    if shutil.which("xsel"):
        return subprocess.run(["xsel", "-b", "-o"],
                              capture_output=True, text=True, timeout=2).stdout
    raise RuntimeError("xclip or xsel is required")


def _clip_set(text):
    if shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"], input=text,
                       text=True, timeout=2, check=True)
        return
    if shutil.which("xsel"):
        subprocess.run(["xsel", "-b", "-i"], input=text,
                       text=True, timeout=2, check=True)
        return
    raise RuntimeError("xclip or xsel is required")


async def _wait_for_local_clip(text, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _clip_get() == text:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.1)
    return False


async def main():
    try:
        import websockets
    except ImportError:
        print("SKIP — websockets not installed")
        return
    if not os.environ.get("DISPLAY"):
        print("SKIP — DISPLAY is unset")
        return

    failures = []
    local_text = f"browser-screencast-client-to-x11-{time.time_ns()}"
    remote_text = f"browser-screencast-x11-to-client-{time.time_ns()}"

    async with websockets.connect(WS_URL, open_timeout=10, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "t": "caps",
            "webcodecs": True,
            "codecs": ["h264"],
            "codecCaps": {"h264": {"hw": False, "sw": True}},
            "w": 640,
            "h": 360,
        }))

        await ws.send(json.dumps({"t": "setclip", "text": local_text}))
        if not await _wait_for_local_clip(local_text):
            failures.append("client setclip did not reach X11 clipboard")

        _clip_set(remote_text)
        deadline = time.monotonic() + 6.0
        saw_remote = False
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, str):
                try:
                    ev = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if ev.get("t") == "clipboard" and ev.get("text") == remote_text:
                    saw_remote = True
                    break
        if not saw_remote:
            failures.append("X11 clipboard change did not reach websocket client")

    if failures:
        print("FAIL — clipboard sync errors:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("PASS — X11 clipboard sync works in both directions")


asyncio.run(main())
