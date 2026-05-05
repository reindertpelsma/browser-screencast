# Project status

Last updated: 2026-05-05

`browser-screencast` is an early Linux/Windows port of `macscreencast`.
The portable pieces are copied over intentionally: frontend, WebSocket session
loop, encoder wrapper, VNC client, and congestion controller.

## What is implemented

- Linux X11 and Windows full-screen capture through `mss`.
- X11 input through XTest.
- Windows input through `SendInput`.
- Clipboard sync through `pyperclip` plus common clipboard commands.
- Optional VNC pass-through.
- WebCodecs decode in the browser with HW/SW capability reporting.
- Opus audio WebSocket using FFmpeg loopback capture when available.
- Rootless Linux installer and PowerShell installer.

## Local VM validation

- Python bytecode compile for `server.py`, `mvs/*.py`, and `tests/*.py`.
- Codec probe regression: `tests/test_codec_probe.py`.
- X11 shifted-character translation regression: `tests/test_platform_keys.py`.
- Xvfb/openbox H.264 no-audio video smoke with saturated 640x360 content:
  433 frames in 8 seconds, about 54 fps.

## What is not yet proven

- Real-machine soak tests on Linux and Windows.
- Native Wayland capture/input.
- Native Windows.Graphics.Capture.
- Headless Xvfb auto-spawn.
- Hardware encoder behavior across NVENC/QSV/AMF/VAAPI hosts.
- Long-duration audio stability.

## Known limitations

Wayland currently needs an X11 session or VNC pass-through. Windows capture uses
the portable `mss` fallback rather than Windows.Graphics.Capture. Hardware
encoder detection is advisory; `EncoderPipeline` still validates by opening the
encoder and falls back to software/JPEG when needed.
