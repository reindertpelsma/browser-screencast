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
- Rootless Linux/BSD install with optional `systemd --user` unit.
- PowerShell install path with optional Scheduled Task.

Still planned:

- Native Wayland capture through PipeWire portals.
- Linux headless mode that starts `Xvfb` and a lightweight window manager.
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
