# browser-screencast — implementation plan

> This document is written so a fresh Claude session can pick it up
> cold. It includes project background, the architecture and lessons
> learned from the sibling project `macscreencast`, the platform
> backends needed for Linux / Windows / BSD, the codec-selection
> spec, and a phased shipping plan.

---

## TL;DR

`browser-screencast` is the Linux/BSD/Windows sibling of
`macscreencast` (formerly `mac-vnc-stream`). Same UX:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/browser-screencast/main/install.sh)
ssh -L 6081:localhost:6081 user@host
open http://localhost:6081/?token=…
```

…browser-based remote desktop at up to 60fps, hardware-decoded video
in WebCodecs, Opus audio, clipboard sync, no third-party relay, just
SSH and a browser.

**What this project adds vs macscreencast:**

- Rootless install on Linux (no LaunchAgent equivalent — no apple-tax
  TCC ceremony either)
- Optional headless mode: auto-spawn `Xvfb` on a Linux box that has
  no display. *This is a killer feature the Mac side cannot have.*
- Inline-or-daemon: same binary runs as a foreground subprocess, a
  `systemd --user` unit, or a Windows Service.
- Multi-backend capture: X11 (XComposite/XShm), Wayland (PipeWire),
  Windows (DXGI Desktop Duplication / Windows.Graphics.Capture).
- Generic codec negotiation that prefers hardware on both ends.
- Optional VNC pass-through: forward to an existing VNC server (e.g.
  TigerVNC, x11vnc) instead of capturing directly. Makes
  browser-screencast a drop-in upgrade for any host with VNC already
  set up.

**Mac users running the install script get redirected to
`reindertpelsma/macscreencast`.** macOS-specific quirks (Tahoe
`gui/$UID`, `screensharingd` AppleDH, ScreenCaptureKit, CGEvent,
TCC.db, py2app bundling, compositor-keepalive subprocess) are all
load-bearing on Mac — they have nothing to do here.

---

## Background: the sibling project (`macscreencast`)

Before reading further, fetch and read the macscreencast repo:

```bash
git clone https://github.com/reindertpelsma/macscreencast.git
```

…specifically:

- `README.md` and `docs/` — the user-facing pitch, architecture
  overview, configuration, security model, VNC bootstrap path.
- `CLAUDE.md` — agent-facing notes, including:
  - Apple VideoToolbox failure modes and what doesn't work (the
    `constant_bit_rate=1` collapse story; HEVC/AV1 lacking strict
    rate options); and what was tried-and-reverted.
  - The disconnect-loop bug analysis (3 stacked causes; the actual
    trigger was `ping_monitor` closing on pong-timeout while pongs
    were queued behind buffered video).
  - Hard rules (e.g. "ping_monitor MUST NOT close the connection on
    pong timeout").
- `mvs/congestion.py` — the adaptive rate-control state machine
  (drain-pause, wb-aware, age-aware). Pure logic, no platform
  deps, **must reuse exactly as-is** in browser-screencast.
- `mvs/handler.py` — WebSocket session loop, ping/pong handling,
  capture-loop wiring. Mostly portable; the Mac-specific parts are
  marked.
- `mvs/encoder.py` — VideoToolbox encoder via PyAV. The PyAV layer
  is portable; only the codec-name strings (`h264_videotoolbox`,
  `hevc_videotoolbox`) are Mac-specific. Replace with platform-
  appropriate encoders (NVENC / VAAPI / QuickSync / AMF / software).
- `frontend/` — the browser-side single-page app. WebCodecs decoder,
  audio, clipboard, keyboard. Almost entirely reusable.
- `mvs/vnc.py` — RFB protocol client. Fully portable. Reuse for the
  optional VNC pass-through.
- `STATUS.md` — honest readme about test coverage and gaps.

**The single most important thing to read** is `CLAUDE.md` ▸
"Things that DON'T work, do not retry without strong validation".
That list represents real iterations that cost real time on the Mac
side; many will recur on Linux/Windows in some form (e.g. encoder
overshoot is not a Mac-specific problem; ping/pong queuing is not a
Mac-specific problem). The lessons there are 80% transferable.

---

## What macscreencast looks like architecturally

```
Browser (any OS)
  │
  │ ─── WebSocket (over SSH tunnel) ───→ macscreencast Python server (on Mac)
  │                                                │
  │ ←── H.264/H.265 frames (WebCodecs) ───────────│←── ScreenCaptureKit
  │                                                │
  │ ─── mouse/keyboard events ────────────────────│──→ CGEventPost (kCGHIDEventTap)
  │                                                │
  │ ←── Opus audio frames ────────────────────────│←── ScreenCaptureKit (system audio)
  │                                                │
  │ ←─→ clipboard JSON ────────────────────────────│←─→ pbpaste / pbcopy
```

The whole architecture is browser ←→ WS ←→ server ←→ OS APIs. We
keep the first two arrows identical and replace OS APIs.

---

## Code reuse map (macscreencast → browser-screencast)

| File / dir from macscreencast | Reuse strategy | Notes |
|---|---|---|
| `frontend/` (entire dir) | **Direct copy**, then edit keymap | Only Cmd→Super rename + audio mode-string flexibility |
| `mvs/codec.py` (codec ID constants) | **Direct copy** | Add AV1, VP9, H.266 if not already present |
| `mvs/congestion.py` | **Direct copy, do not modify** | Pure logic; would be malpractice to fork it |
| `mvs/handler.py` | **Copy + abstract platform calls** | Most code portable; capture-bridge interface needs cleanup |
| `mvs/encoder.py` | **Copy + replace codec-name strings** | PyAV is cross-platform; just need the right encoder names per platform |
| `mvs/vnc.py` (RFB client) | **Direct copy** | Used in optional VNC pass-through mode |
| `mvs/sck.py` | **REPLACE entirely** | Mac-specific. → `capture/x11.py`, `capture/wayland.py`, `capture/windows.py`, `capture/vnc.py` (pass-through) |
| `mvs/cgevent.py` | **REPLACE entirely** | Mac-specific. → `input/x11.py`, `input/wayland.py`, `input/windows.py` |
| `mvs/audio.py` | **REPLACE entirely** | Mac SCK audio. → `audio/pulse.py`, `audio/pipewire.py`, `audio/wasapi.py` |
| `mvs/keepalive.py` | **DROP** | Mac WindowServer 3Hz idle-throttle is Mac-specific |
| `mvs/caffeinate.py` | **REPLACE** with platform equivalent | Linux: `systemd-inhibit` / `xset s noblank`. Windows: `SetThreadExecutionState`. |
| `server.py` | **Copy + remove Mac-specific flag plumbing** | `--api-only`, `--vnc-only` semantics change here (no SCK; "capture mode" becomes X11/Wayland/Windows/VNC) |
| `setup.sh` | **REWRITE** | No .app bundle, no TCC, no LaunchAgent. Linux: pip + optional systemd unit. Windows: WSL/native PowerShell variant. |
| `install.sh` | **REWRITE** | Curl-bash entry, OS detection (Mac → redirect), distro detection (apt/dnf/pacman/zypper), Wayland/X11 detection. |
| `build_app.py` | **DROP** | py2app is macOS-only. We don't bundle. |
| `launchagent.plist.template` | **DROP** | LaunchAgent is macOS-only. Replace with `systemd/macscreencast.service.template`. |
| `docs/` | **Direct copy + platform edits** | Most behavioral docs apply unchanged. |
| `tests/tcp_throttle.py` | **Direct copy** | Real-TCP throttle harness is platform-agnostic. Reuse. |

**~70% of the code is portable as-is. ~30% is platform backends.**
This estimate is from `project_linux_windows_port_plan` memory.

---

## Codec selection: spec

The codec a session ends up using is the highest-priority codec
that is supported by **both** the server's encoder layer and the
browser's WebCodecs decoder. Priority is two-tier:

### Criterion 1 (group): hardware coverage

Higher group = higher priority. Within the same group, fall through
to Criterion 2.

| Group | Server | Client | Example |
|---|---|---|---|
| **G1** | hardware encode | hardware decode | server: NVENC AV1 + client: WebCodecs AV1 with HW decode |
| **G2** | hardware encode | software decode | server: NVENC AV1 + client: WebCodecs AV1 SW decode (rare; AV1 SW decode in browsers is slow) |
| **G3** | software encode | hardware decode | server: libsvtav1 + client: WebCodecs AV1 HW decode |
| **G4** | software encode | software decode | server: libx264 + client: WebCodecs H.264 SW decode |
| **G5** | JPEG fallback | image decode | last resort, when WebCodecs unavailable |

### Criterion 2 (codec): efficiency order

Within each group, prefer codecs in this order:

```
av1 > h.266 (VVC) > h.265 (HEVC) > vp9 > h.264 (AVC)
```

JPEG is its own group (G5), not part of this ordering.

### Algorithm (pseudo-code)

```python
def select_codec(server_caps, client_caps):
    # server_caps = {(codec, hw): True/False, ...}
    #    e.g. {("av1", "hw"): True, ("av1", "sw"): True, ("h264", "hw"): True, ...}
    # client_caps = same shape from WebCodecs probe in the browser

    codec_priority = ["av1", "h266", "h265", "vp9", "h264"]
    group_priority = [
        ("hw", "hw"),  # G1
        ("hw", "sw"),  # G2
        ("sw", "hw"),  # G3
        ("sw", "sw"),  # G4
    ]

    for (s_kind, c_kind) in group_priority:
        for codec in codec_priority:
            if server_caps.get((codec, s_kind)) and client_caps.get((codec, c_kind)):
                return (codec, s_kind, c_kind)

    if server_caps.get(("jpeg", "sw")) and client_caps.get(("jpeg", "sw")):
        return ("jpeg", "sw", "sw")

    raise NoSharedCodec()
```

### Practical detection

**Server side** (probe at startup, log the matrix):

- Linux: `ffmpeg -encoders | grep -E 'nvenc|vaapi|qsv|amf'` for HW;
  presence of `libsvtav1`, `libvvenc`, `libx265`, `libvpx-vp9`,
  `libx264` for SW. NVENC requires NVIDIA + driver; VAAPI requires
  Intel/AMD + `/dev/dri/render*` accessible.
- Windows: probe via `ffmpeg -encoders` from a bundled FFmpeg, or
  via Windows Media Foundation / DirectX Video Acceleration APIs
  (more involved; PyAV with FFmpeg is simpler).
- For the actual encode path, prefer **PyAV** (already used on Mac
  for VideoToolbox) — it's a thin wrapper over libav and is
  cross-platform. Just supply the right encoder name.

**Client side** (probe at session start, send to server in handshake):

```js
// In the browser, query WebCodecs for HW vs SW decode capability
const codecs = ["av01.0.05M.08", "vvc1...", "hev1.1.6.L120.B0",
                "vp09.00.10.08", "avc1.42E01F"];
const result = {};
for (const c of codecs) {
  const cfg = await VideoDecoder.isConfigSupported({codec: c});
  if (cfg.supported) {
    // hardwareAcceleration field tells us prefer-hardware/software/no-preference
    // The browser sets it based on actual capability, not our request
    result[c] = cfg.config.hardwareAcceleration === "prefer-hardware"
              ? "hw" : "sw";
  }
}
ws.send(JSON.stringify({type: "codec-caps", caps: result}));
```

The frontend already does some codec negotiation; extend it to
report HW vs SW per codec and to encode the same `(codec, kind)`
shape the server expects.

### Why this ordering matters

- **G1 (hw/hw)** is the only configuration where 60fps at low
  bandwidth is feasible without melting one of the two CPUs.
- **G2 (hw/sw)** is rare in practice and often a bad combination
  (e.g. AV1 hardware encoders are so efficient at high motion that
  they outpace browser SW decode and the client can't keep up).
- **G3 (sw/hw)** is realistic for Linux servers without a discrete
  GPU but with modern Intel/AMD client hardware. libsvtav1 +
  AV1 HW decode in Chrome on M-series Mac is genuinely fast.
- **G4 (sw/sw)** is the floor before JPEG. H.264 SW everywhere is
  the safe baseline.
- **G5 (JPEG)** matches macscreencast's WebCodecs-unavailable path.
  Practically only fires on very old browsers.

---

## Platform backends

### Linux

#### Capture

| Display server | API | Library | Rootless? | Notes |
|---|---|---|---|---|
| X11 | XShm + XComposite | python-mss, python-xlib, or direct ctypes | yes | Most cloud Linuxes are X11 still. mss is the simplest cross-platform option but doesn't expose XComposite — for windowed capture we'd need direct Xlib. Default to full-screen capture via mss; address per-window later. |
| Wayland | PipeWire via xdg-desktop-portal | GStreamer `pipewiresrc`, or `pyscreenshot` (slow), or direct PipeWire bindings | yes | Requires user to grant permission via portal dialog. GNOME/KDE both support it. Headless Wayland is harder; cage / wlroots-based compositors needed. |
| TTY / no display | Xvfb auto-spawn | xvfbwrapper or direct subprocess | yes | The killer feature. Spin up `Xvfb :99 -screen 0 1920x1080x24`, set DISPLAY, optionally launch a window manager (xfwm4 / openbox / i3) and a session ($DESKTOP_SESSION). Capture from $DISPLAY. |
| VNC pass-through | RFB protocol | macscreencast's `mvs/vnc.py` (reuse) | yes | Connect to existing TigerVNC / x11vnc / kasmvnc. Useful when user already has VNC set up. |

**Recommended default:** detect display server via `$WAYLAND_DISPLAY`
vs `$DISPLAY`, fall back to "headless" prompt if neither.

#### Input injection

| Display | API | Rootless? | Notes |
|---|---|---|---|
| X11 | XTest extension | yes | Pretty much universal on X11; python-xlib supports it. |
| Wayland | `wtype` / `ydotool` (compositor permitting) | mostly yes | Wayland sandboxes input strictly. `wtype` works on wlroots compositors (sway, hyprland). GNOME/KDE need `ydotool` which itself needs `/dev/uinput` access (typically root or `input` group). **Document this as a Wayland limitation.** |
| Headless Xvfb | XTest | yes | Same as X11. |

#### Audio

| API | Library | Notes |
|---|---|---|
| PulseAudio | `pulsectl` (Python) or direct via FFmpeg | Default on most desktops still. |
| PipeWire | GStreamer `pipewiresrc audio` | Modern Fedora / Ubuntu 22.10+ default. |
| ALSA | direct via FFmpeg `-f alsa` | Lowest level; works headless. |

**Recommended:** detect via `pactl info` → PulseAudio? `pw-cli info
0` → PipeWire? else ALSA.

For headless Xvfb users, audio is usually irrelevant; allow `--no-audio`.

#### Daemon

```ini
# ~/.config/systemd/user/browser-screencast.service
[Unit]
Description=browser-screencast server
After=graphical-session.target

[Service]
ExecStart=/home/%u/.local/bin/browser-screencast --listen 127.0.0.1 --port 6081
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now browser-screencast
```

Rootless. No sudo needed. The user can SSH in, enable the unit, log
out, and the server keeps running across SSH disconnects — and
**doesn't** survive a reboot unless they also `loginctl enable-linger`
(which is fine, document it).

### Windows

#### Capture

| API | Library | Notes |
|---|---|---|
| **Windows.Graphics.Capture** | `windows-capture` (PyPI), or `pywinrt` | Modern, hardware-accelerated, Win 10 1803+. **Default.** |
| DXGI Desktop Duplication | direct ctypes wrapper or via FFmpeg `gdigrab` for fallback | Older but reliable Win 8+. Lower-level. |
| GDI BitBlt | pywin32 / mss | Slow, last-resort. |

#### Input injection

| API | Library | Notes |
|---|---|---|
| `SendInput` | pywin32 or ctypes | Standard Win32 input injection. Rootless. Works for keyboard + mouse. |
| `keybd_event` (legacy) | — | Don't use. Superseded by SendInput. |

#### Audio

| API | Library | Notes |
|---|---|---|
| **WASAPI loopback** | `pyaudio` with WASAPI loopback flag, or direct via FFmpeg `dshow` | Standard system-audio capture. Win Vista+. |
| MME / DirectSound | — | Don't use. Legacy. |

#### Daemon

Two paths:

1. **NSSM / sc.exe service**: register the Python script as a
   Windows Service. Survives logout, requires Local System or a
   service account. More setup, more friction.
2. **Scheduled Task at logon**: simpler. `schtasks /create /tn ...
   /tr "python C:\path\server.py" /sc onlogon /ru %USERNAME%`.
   Runs only when user is logged in, no service-account headaches.

Recommended default: scheduled task. Document service path for
power users.

### BSD (FreeBSD, OpenBSD)

X11 only (Wayland support on BSD is pre-production for our purposes).
Same path as Linux X11. ALSA/OSS via FFmpeg for audio. No systemd —
use `rc.d` script (just calls `daemon -f` or similar). Lower priority
than Linux + Windows; ship after the v0.1 milestone.

---

## Headless mode (Linux only, late feature)

The flow:

```bash
# User runs install on a fresh Linux box with no DISPLAY:
$ bash <(curl -fsSL .../install.sh)
[detected] no $DISPLAY, no $WAYLAND_DISPLAY
[detected] xvfb available at /usr/bin/Xvfb
[prompt] Headless mode? [Y/n]
[install] writing systemd unit with Xvfb pre-start
$ systemctl --user start browser-screencast
$ # SSH from laptop: ssh -L 6081:localhost:6081 user@host
$ # open http://localhost:6081/?token=…
$ # → user sees a working desktop with a window manager + browser
```

Implementation order:

1. **Phase 4a:** raw Xvfb (no WM, just an empty rootless display).
   Useful for CI / scripted testing.
2. **Phase 4b:** Xvfb + lightweight WM (xfwm4, openbox, or i3). Now
   the user can actually do work in there.
3. **Phase 4c:** offer to apt/dnf-install Xvfb and a WM if not
   present. Optional, gated behind explicit user opt-in.

Headless mode does not need a graphical-session.target dep; it's
entirely contained.

---

## Install / setup script (`install.sh`)

### Mac redirect

```bash
case "$(uname -s)" in
  Darwin)
    echo "macOS detected — please use macscreencast instead:"
    echo "  bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/macscreencast/main/install.sh)"
    exit 0 ;;
  Linux|FreeBSD|OpenBSD) ;;
  CYGWIN*|MINGW*|MSYS*|Windows_NT)
    echo "Windows detected — see install.ps1 for the PowerShell variant."
    exit 0 ;;
  *)
    echo "Unsupported OS: $(uname -s)" >&2
    exit 1 ;;
esac
```

### Distro detection (Linux)

```bash
if command -v apt-get >/dev/null;     then PKG=apt
elif command -v dnf >/dev/null;       then PKG=dnf
elif command -v pacman >/dev/null;    then PKG=pacman
elif command -v zypper >/dev/null;    then PKG=zypper
elif command -v xbps-install >/dev/null; then PKG=xbps
else PKG=unknown; fi
```

### Display server detection

```bash
if   [ -n "$WAYLAND_DISPLAY" ]; then DPY=wayland
elif [ -n "$DISPLAY" ];          then DPY=x11
elif command -v Xvfb >/dev/null; then DPY=headless-available
else DPY=none; fi
```

### Install steps

1. Mac/Windows redirect.
2. Detect distro + display server.
3. `pip install --user` Python deps (websockets, av, numpy, mss,
   python-xlib for X11, etc).
4. Detect / list capture/encoder/decoder capabilities → store in a
   `caps.json` for the server to surface in `/v1/status`.
5. Prompt: inline run, `systemd --user` unit, or `nohup` daemon.
6. If headless: prompt for Xvfb auto-spawn config.
7. Generate access token, print connect command.

**Critically, no sudo unless the user requests system-wide install**
(e.g. `--system` flag). Default is `~/.local/bin` + `~/.config/systemd/user/`.

### `setup.sh` (the local-clone path)

Exactly the same as install.sh except it runs from the cloned repo
without curl. Same flags, same output.

---

## Frontend changes

The macscreencast frontend (`frontend/index.html`) is a single
self-contained file with the WebCodecs decoder, audio pipe, and
keyboard handling. Reuse it as-is and apply these edits:

1. **Cmd → Super.** The "extra keys" tab (visual UI) currently shows
   "Cmd". Rename label to "Super". The actual key code sent to the
   server should be a generic "super" / "meta" event; the server's
   X11 / Wayland / Win32 input layer maps that to the appropriate
   target-OS keysym (Super_L on Linux, VK_LWIN on Windows).

2. **Cmd-via-Safari fallback.** On Safari (running on a Mac), the
   user's physical Cmd key is still the natural modifier. Translate
   browser Cmd events to Super on the wire. This is purely a
   client-side translation; if the user is on a Mac driving a Linux
   box, Cmd in the browser → Super sent to the Linux server feels
   correct. (Optional; can ship without it and address later.)

3. **Audio remains.** No changes needed; the audio WS endpoint
   contract is the same.

4. **Codec negotiation.** Send the new HW/SW capability matrix in
   the handshake (see "Codec selection" above).

5. **Branding.** Title, footer, app icon: "browser-screencast"
   instead of "macscreencast".

---

## Phasing plan

The macscreencast project shipped in 5 days; this one will be
faster because ~70% of the code is reuse-or-copy. Estimate is
**3–4 weeks calendar for both Linux and Windows** at the same
solo-engineer-with-LLM pace.

### Phase 0 — Repo skeleton (1 day)

- New repo `reindertpelsma/browser-screencast`
- Copy from macscreencast: `frontend/`, `mvs/codec.py`, `mvs/congestion.py`,
  `mvs/handler.py`, `mvs/encoder.py`, `mvs/vnc.py`, `tests/tcp_throttle.py`
- Empty stubs for `capture/`, `input/`, `audio/`, `os/` packages
- README: project pitch + Mac redirect note
- Mac-redirect logic in install.sh
- CI: just lint + unit tests for the portable code

### Phase 1 — Linux X11 v0.1 (3–4 days)

- `capture/x11.py` — XShm capture via mss or python-xlib
- `input/x11.py` — XTest input injection
- `audio/pulse.py` — PulseAudio default (most common)
- Codec selection — hard-coded to G3-G4 (software encode + browser
  HW/SW decode); leave NVENC/VAAPI for Phase 3
- `setup.sh` minimal — pip install, run inline
- Test on a real Linux desktop + via SSH tunnel from a Mac
- **Ship as v0.1 — gives a working "SSH-tunneled browser RDP for Linux"**

### Phase 2 — Windows v0.1 (3–4 days)

- `capture/windows.py` — Windows.Graphics.Capture via `windows-capture`
- `input/windows.py` — SendInput via pywin32
- `audio/wasapi.py` — WASAPI loopback
- Same software encoder set as Linux
- `install.ps1` — PowerShell installer; same UX as install.sh
- Scheduled Task daemon mode; Service mode behind a flag
- Test on Windows 11 + Windows Server 2022

### Phase 3 — Hardware codec backends (3–5 days)

- Server-side encoder probe: detect NVENC, VAAPI, QuickSync, AMF,
  VideoToolbox (yes — let macscreencast share a codebase if we
  ever consolidate), libsvtav1, libvvenc, libx265
- Client-side codec capability matrix (frontend update)
- Implement the codec-selection algorithm spec above
- Test matrix: server with each HW backend × client with each
  browser × each codec
- Document the matrix in `docs/codecs.md`

### Phase 4 — Linux headless / Wayland / VNC pass-through (1–2 weeks)

- **Phase 4a:** Wayland capture via PipeWire + `wtype`/`ydotool`
  input. (Hard but worth it; covers modern desktops.)
- **Phase 4b:** Headless mode with Xvfb auto-spawn. (Killer feature
  for CI / cloud Linux without GUI.)
- **Phase 4c:** Optional WM install + DESKTOP_SESSION launch in
  headless mode. (Ergonomic.)
- **Phase 4d:** VNC pass-through mode (`--vnc-target host:5900`)
  reusing `mvs/vnc.py`. Useful for users with existing TigerVNC
  setups; lower priority but cheap given the code is already there.

### Phase 5 — BSD (1–2 days)

- FreeBSD + OpenBSD X11 path
- Lower priority; ship after v0.1 of Linux + Windows

### Phase 6 — Polish / announcement (3–5 days)

- Soak test on real boxes (Hetzner Linux VPS, AWS Windows EC2)
- Real-TCP throttle test (the macscreencast test harness ports
  directly)
- Cross-continent latency test (the same Paris↔GH-runner geometry
  used for macscreencast — the memory file
  `project_mvs_real_world_test_geometry` has the setup)
- Public announcement; lead with the same framing as macscreencast
  (browser-only, SSH-only, no relay) plus the headless-Xvfb angle
  that's unique to Linux

---

## Things to definitely NOT do

These are anti-patterns inherited from macscreencast's hard lessons:

1. **Don't port the macscreencast `keepalive` subprocess.** The
   ~3Hz WindowServer throttle is a macOS-specific behavior. Linux
   X11 / Windows DXGI don't have an equivalent. Skip the entire
   subprocess.

2. **Don't reimplement `mvs/congestion.py`.** Just copy it. The
   wb-aware drain logic, the on_lag hooks, the rate enforcement at
   the network layer (not the encoder layer): all pure logic, all
   load-bearing, all already validated by the macscreencast cross-
   continent test. Forking it is the path to "the disconnect-loop
   bug, but again."

3. **Don't close the WS on ping/pong timeout.** This is the same
   bug the Mac hit and it will hit here. Ping timeouts are a
   *congestion signal*, not a *liveness signal*. Use the same
   pattern: feed timeout to the controller as `on_lag`, leave the
   connection alive. Browser-side 15s stall detector + websockets
   library's keepalive are the actual liveness checks.

4. **Don't hard-code `constant_bit_rate=1` (NVENC equivalent: CBR
   mode with strict cap).** The macscreencast lesson was that
   strict-CBR mode collapsed VideoToolbox output to ~10% of target
   on simple content. NVENC's `-rc cbr` has its own quirks. Always
   prefer VBR with rate-target enforcement at the network layer
   (drain pause), not at the encoder.

5. **Don't store passwords for rootless install.** macscreencast
   stores macOS login passwords on cloud Macs because
   `screensharingd`'s AppleDH auth requires it. Linux/Windows have
   no equivalent. The whole rationale evaporates. Don't ask for a
   password during install.

6. **Don't bundle as a .exe / AppImage / Flatpak in v0.1.** Plain
   Python install is the v0.1 target. Distribution packaging is a
   later optimization, not a launch blocker. (This is what the
   macscreencast project memory ▸ "Discovery language ≠ ship
   language" says: prototype in the language with the fastest
   inner loop, port later only after the moat is identified.)

7. **Don't add Mac-style "auto-upgrade" path.** macscreencast
   auto-upgrades from VNC → SCK when TCC permissions land mid-
   session. There's no equivalent on Linux/Windows because there's
   no permission gate; the user's chosen capture backend either
   works or it doesn't.

8. **Don't use Wayland-mandatory paths in v0.1.** Lots of cloud
   Linux is still X11; lots of physical Linux desktops are X11.
   Wayland support is a Phase-4 feature, not a Phase-1 blocker.

9. **Don't require root for the install.** Default to `~/.local/`
   + `systemd --user`. Document `--system` for users who genuinely
   want a system service.

10. **Don't break the macscreencast contract on the WebSocket
    wire.** The frontend should be the same code as macscreencast,
    branding aside. If we diverge the WS protocol, we lose the
    "same browser tab works for any backend" property.

---

## References

### macscreencast (sibling project)

- Repo: https://github.com/reindertpelsma/macscreencast
- Docs to read first: `README.md`, `CLAUDE.md`,
  `docs/vnc-bootstrap.md`, `STATUS.md`
- Project memories (in this Claude environment):
  `project_macscreencast_market_positioning.md`,
  `project_macscreencast_v018_ship_milestone.md`,
  `project_linux_windows_port_plan.md`,
  `feedback_velocity_requires_test_rig_density.md`

### Capture libraries

- python-mss: https://github.com/BoboTiG/python-mss
- python-xlib: https://github.com/python-xlib/python-xlib
- xvfbwrapper: https://github.com/cgoldberg/xvfbwrapper
- windows-capture: https://github.com/NiiightmareXD/windows-capture
- PipeWire portal screencast: https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html
- GStreamer pipewiresrc: https://gstreamer.freedesktop.org/documentation/pipewire/pipewiresrc.html

### Input libraries

- python-xlib XTest: see `Xlib.ext.xtest`
- pywin32 SendInput: see `win32api.SendInput`
- wtype (Wayland keystroke injection): https://github.com/atx/wtype
- ydotool: https://github.com/ReimuNotMoe/ydotool

### Audio libraries

- pulsectl: https://pypi.org/project/pulsectl/
- PyAudio (WASAPI): https://people.csail.mit.edu/hubert/pyaudio/
- FFmpeg backends: `-f pulse`, `-f pipewire`, `-f alsa`, `-f dshow`,
  `-f wasapi` (via dshow on Windows)

### Codec / encoder libraries

- PyAV (libav binding): https://github.com/PyAV-Org/PyAV
- NVENC: included in FFmpeg via `--enable-nvenc`
- VAAPI: included in FFmpeg, requires `libva-dev`
- libsvtav1: included in FFmpeg via `--enable-libsvtav1`
- libvvenc (H.266): https://github.com/fraunhoferhhi/vvenc

### Browser WebCodecs reference

- WebCodecs API: https://www.w3.org/TR/webcodecs/
- VideoDecoder.isConfigSupported: tells you HW vs SW per codec
- Codec strings reference: https://www.w3.org/TR/webcodecs-codec-registry/

---

## Open questions for the user

1. **Wayland input injection on GNOME/KDE.** `ydotool` requires
   `/dev/uinput` access, typically only root or `input` group.
   Acceptable to document this as "join the `input` group" rather
   than ship a setuid helper?
2. **Windows daemon default.** Scheduled Task or Service? Scheduled
   Task is simpler but doesn't survive logout. Service does but
   needs a service account or Local System.
3. **Headless WM choice.** `xfwm4`, `openbox`, `i3`, or just no WM
   at all in v0.1? My recommendation: openbox in v0.1 (lightweight,
   no config required to be useful).
4. **Naming.** This plan assumes `browser-screencast` per the
   linux-windows-port-plan memory. Confirm or rename before Phase 0.

---

## What "v0.1 done" looks like

- `bash <(curl -fsSL .../install.sh)` on a fresh Ubuntu 24.04
  desktop with X11 → working server bound on `127.0.0.1:6081`.
- `bash <(curl -fsSL .../install.ps1)` on a fresh Windows 11 →
  working server bound on `127.0.0.1:6081`.
- From a different machine: `ssh -L 6081:localhost:6081 …` + open
  the URL → 60fps browser remote desktop.
- Audio works.
- Clipboard works (both directions).
- Keyboard works including Super.
- Codec selection picks at minimum G4 (software encode, browser HW
  decode where possible).
- Mac users running the install script see the redirect message.

That's the bar. Everything else is iteration.
