# browser-screencast

Low-latency browser remote desktop for Linux and headless cloud VMs.

No relay, no account, no browser extension. One Python server, one SSH tunnel.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/browser-screencast/main/install.sh)
```

Then forward the port over SSH and open the URL the installer prints:

```bash
ssh -L 6081:localhost:6081 user@host
# open http://localhost:6081/?token=YOUR_TOKEN
```

macOS users: use the sibling project instead —
`bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/macscreencast/main/install.sh)`

## Why it feels fast

- **Hardware H.265 / AV1 / VP9 / H.264** — WebCodecs in the browser decodes hardware-encoded frames. NVENC, VAAPI, or AMF on the server; software fallback (libx265/libvpx) when no GPU is present.
- **x11grab direct capture** — reads straight from the X framebuffer via FFmpeg/libav. No VNC round-trip, no RFB protocol overhead.
- **Adaptive congestion controller** — watches TCP backpressure and ping gradient rather than a fixed quality knob. Bitrate tracks the link.
- **Client-side cursor plane** — the pointer is never encoded into the video. The server reports only what the cursor *is* (~30 bytes, on change only) and the browser draws it at your local pointer, so cursor motion costs no round trip at all.

## Requirements

| Component | Minimum | Notes |
|-----------|---------|-------|
| Python | 3.9+ | |
| PyAV | 12+ | `pip install av` — needs FFmpeg libs |
| Browser | Chrome / Edge / Safari | WebCodecs required; Firefox not yet |
| Display | X11 or headless | Wayland: use an XWayland session for now |

Hardware encoders are auto-detected and preferred. A plain CPU with FFmpeg is enough for software H.265.

## Headless cloud VM / Docker

Tested on vast.ai NVIDIA GPU instances (no /dev/kvm, Docker container):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/browser-screencast/main/install.sh) --headless
```

The installer starts Xvfb + a lightweight window manager automatically. NVENC is used when an NVIDIA GPU is present; the server falls back to software encoding otherwise.

## Local X11 desktop

```bash
git clone https://github.com/reindertpelsma/browser-screencast.git
cd browser-screencast
bash setup.sh
browser-screencast --generate-token
```

## Daemon (systemd)

```bash
bash setup.sh --systemd
systemctl --user status browser-screencast
```

The unit is rootless and binds to `127.0.0.1` by default. To keep it alive after logout:

```bash
loginctl enable-linger "$USER"
```

For a headless daemon:

```bash
bash setup.sh --systemd --headless
```

## Windows

From PowerShell in a checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
browser-screencast.cmd --generate-token
```

## Codec selection

The server auto-negotiates the best codec both sides support:

| Codec | Hardware | Software |
|-------|----------|---------|
| H.265 | NVENC / VAAPI / VideoToolbox | libx265 |
| AV1   | NVENC AV1 / VAAPI AV1 | libsvtav1 |
| VP9   | VAAPI VP9 | libvpx-vp9 |
| H.264 | NVENC / VAAPI / AMF | libx264 |

Auto mode prefers H.265 for best quality/bitrate ratio. Force a codec with `--codec h264` etc.

## Cursor

The cursor is not part of the picture. Baking it into the frame (`draw_mouse=1`)
makes every mouse movement wait for a full round trip, which over an SSH tunnel
reads as lag even when the stream itself is current — so, like RDP, SPICE and
VNC's cursor pseudo-encoding, it travels as metadata and is composited by the
client at the *local* pointer position.

What travels is the cursor's **identity**, not its pixels. On X11 the server
watches XFixes (`XFixesSelectCursorInput`, event-driven — a still cursor costs
nothing) and sends the cursor's name mapped to a CSS keyword:

```json
{"t":"cursor","vis":1,"css":2}
```

The browser then draws *your own* platform's pointer — right theme, right DPI,
no scaling artefacts — for 30 bytes per change. Measured on a real GNOME
desktop, the same cursor as a PNG would have been 2022 bytes. A cursor with no
name we recognise (an app's custom pointer) falls back to the bitmap form
(`{"vis":1,"img":"data:image/png;base64,…","hx":7,"hy":7}`); `"vis":0` means the
remote hid its cursor, e.g. a game grabbing the mouse, and the client plane
hides with it.

Backends that cannot report a cursor — VNC, Windows, no XFixes — simply send
nothing, and the client falls back to a generic marker. `--draw-mouse on`
forces the old baked-in cursor; `auto` (the default) does that only when XFixes
tracking is unavailable. The dock's **Hide cursor** toggle remains a manual
override on top, and defaults to *off* — the cursor is now correct, so there is
nothing to hide.

## Quality vs latency

The UI has two presets:

- **Responsive** — sub-50 ms end-to-end; ideal for typing and mousing. Fast motion video may drop frames briefly.
- **Buffer 3 s** — smooth video playback at the cost of 3 s input lag. Equivalent to YouTube Live low-latency mode.

## Capture modes

| Mode | Flag | When to use |
|------|------|-------------|
| x11grab | `--capture x11grab` | Default on X11; direct framebuffer, lowest overhead |
| mss | `--capture mss` | Fallback if x11grab unavailable |
| VNC pass-through | `--capture vnc` | Point at an existing VNC server |

## Testing

```bash
pytest              # unit + protocol suite
```

Two files under `tests/` are not ordinary unit tests and are run directly:

- `tests/test_encoder_roundtrip.py` — an **encode → decode → PSNR harness** with
  its own runner: `python3 tests/test_encoder_roundtrip.py --codec h265`. It
  needs a GPU to exercise the NVENC paths; without one it covers the software
  encoders.
- `tests/test_2mbps.py` — a **throughput script against a live server**:
  `python3 tests/test_2mbps.py <port> <token>`. It connects to a running
  instance rather than starting one.

## Security model

The server binds to `127.0.0.1` by default. Reach it through SSH port forwarding:

```bash
ssh -L 6081:localhost:6081 user@host
```

Use `--password` or the installer-generated token. Do not bind to `0.0.0.0` without a separate access-control layer.
