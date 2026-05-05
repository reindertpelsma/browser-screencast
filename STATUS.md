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
- Linux headless mode through server-managed `Xvfb` plus `openbox`/another WM.
- Rootless Linux installer and PowerShell installer.

## Local VM validation

- Python bytecode compile for `server.py`, `mvs/*.py`, and `tests/*.py`.
- Codec probe regression: `tests/test_codec_probe.py`.
- X11 shifted-character translation regression: `tests/test_platform_keys.py`.
- Strict Linux matrix: `python tests/linux_matrix.py --fast`.
- Full 2 Mbps proxy throttle case: 90 seconds at 960x540 full-frame noise,
  peak warmup lag about 3.0s, post-warmup max lag 1ms, 2.01 Mbps tail link,
  about 53 fps, and no proxy buffer overrun.
- X11 input e2e: pointer, click, scroll, normal key, shifted symbol.
- X11 clipboard e2e in both directions.
- PulseAudio loopback audio with null sink and generated tone:
  230 real Opus packets in 5 seconds.
- Headless Xvfb/openbox H.264 no-audio video smoke:
  about 58-60 fps at 640x360 animated content.
- ARM64 Ubuntu host (`aarch64`, Python 3.14): `python tests/linux_matrix.py --fast`
  passed, including audio, headless mode, X11 input/clipboard, and shortened
  2 Mbps throttle.
- Windows 10 LTSC VM through `dockurr/windows`: OpenSSH terminal access,
  Python 3.12 venv install, bytecode compile, `server.py --print-caps`,
  interactive Scheduled Task capture probe (`mss` 800x600 BGRA frame) and
  `SendInput` startup passed. With an animated desktop and audio disabled,
  host-side `tests/ci_smoke.py 6081 wintest` passed at 161 frames in 8 seconds.

## What is not yet proven

- Real-machine soak tests on Linux and Windows.
- Windows audio capture on real hardware.
- Native Wayland capture/input.
- Native Windows.Graphics.Capture.
- Hardware encoder behavior across NVENC/QSV/AMF/VAAPI hosts.
- Long-duration audio stability beyond the smoke test.

## Known limitations

Wayland currently needs an X11 session, headless Xvfb, or VNC pass-through.
Windows capture uses the portable `mss` fallback rather than
Windows.Graphics.Capture. On Windows, capture must run in the logged-in
interactive desktop session; direct OpenSSH-launched capture is expected to
fail with GDI/BitBlt session isolation. Hardware encoder detection is advisory;
`EncoderPipeline` still validates by opening the encoder and falls back to
software/JPEG when needed.
