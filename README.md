# browser-screencast

Browser-based Linux/Windows remote desktop over SSH.

No relay, no account, no browser extension. Run a Python server on the remote
machine, forward one localhost port over SSH, and open the UI in a browser.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/browser-screencast/main/install.sh)
ssh -L 6081:localhost:6081 user@host
open "http://localhost:6081/?token=YOUR_TOKEN"
```

macOS users should use the sibling project instead:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/macscreencast/main/install.sh)
```

## Status

This repo is an initial Linux/Windows port of `macscreencast` following
`PLAN.md`.

Implemented now:

- Same browser/WebSocket wire protocol and congestion controller.
- Linux X11 full-screen capture via `mss`.
- Windows full-screen capture fallback via `mss`.
- X11 input injection via XTest.
- Windows input injection via Win32 `SendInput`.
- Clipboard sync via `pyperclip` and common platform clipboard tools.
- Optional VNC pass-through with the portable RFB client.
- WebCodecs codec negotiation with HW/SW client capability reporting.
- Optional FFmpeg-backed Opus audio loopback when a supported source exists.
- Linux headless mode with server-managed `Xvfb` and a lightweight window manager.
- Rootless Linux/BSD install with optional `systemd --user` unit.
- PowerShell install path with optional Scheduled Task.

Still planned:

- Native Wayland capture through PipeWire portals.
- Native Windows.Graphics.Capture backend.
- Hardware encoder probe/open validation for VAAPI/QSV/AMF beyond the current
  PyAV candidate cascade.
- Broader real-machine test matrix.

## Local Setup

```bash
git clone https://github.com/reindertpelsma/browser-screencast.git
cd browser-screencast
bash setup.sh
browser-screencast --generate-token
```

On a Linux X11 desktop, the default auto mode should choose X11 capture/input.
On Wayland, use an X11 session for now or point the server at an existing VNC
server:

```bash
browser-screencast --capture vnc --input vnc --vnc-host 127.0.0.1 --vnc-port 5900 --vnc-pass "$VNC_PASS"
```

For a Linux VM with no display, use server-managed headless mode:

```bash
browser-screencast --headless --capture x11 --input x11
```

## Systemd User Unit

```bash
bash setup.sh --systemd
systemctl --user status browser-screencast
```

The unit is rootless and bound to `127.0.0.1` by default. To keep it running
after logout across reboots, enable linger yourself:

```bash
loginctl enable-linger "$USER"
```

For a headless user unit:

```bash
bash setup.sh --systemd --headless
```

## Windows

From PowerShell in a checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
browser-screencast.cmd --generate-token
```

To register a logon task:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -ScheduledTask
```

## Security Model

The server binds to `127.0.0.1` by default and is intended to be reached through
SSH port forwarding:

```bash
ssh -L 6081:localhost:6081 user@host
```

Use `--password` or the installer-generated token. Do not bind to `0.0.0.0`
unless you put another access-control layer in front of it.

## Linux Validation

The local Linux matrix is intentionally strict:

```bash
python tests/linux_matrix.py
```

It covers Xvfb/openbox video, X11 input, clipboard, PulseAudio loopback,
headless mode, and the 2 Mbps proxy congestion test. Use `--fast` for a
shorter local iteration run.
