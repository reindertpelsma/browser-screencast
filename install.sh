#!/usr/bin/env bash
# install.sh — install browser-screencast from a git checkout or via curl|bash.
#
# Usage (curl-bash):
#   bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/browser-screencast/main/install.sh)
#
# Usage (from checkout):
#   bash install.sh [--headless] [--systemd] [--port PORT] [--no-deps] [--start]
#
# Flags forwarded to the wrapper launcher:
#   --port PORT    web listen port (default 6081)
#   --headless     start Xvfb + window manager; no physical display needed
#   --systemd      install and enable a systemd --user unit
#   --no-deps      skip Python and system dependency installation
#   --start        start the server in the foreground after setup

set -euo pipefail

# ── Helpers ───────────────────────────────────────────────────────────────────
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()    { red "ERROR: $*"; exit 1; }

# ── OS guard ──────────────────────────────────────────────────────────────────
case "$(uname -s)" in
    Darwin)
        red "  This installer is for Linux. macOS users:"
        yellow "  bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/macscreencast/main/install.sh)"
        exit 1 ;;
    Linux|FreeBSD|OpenBSD) ;;
    CYGWIN*|MINGW*|MSYS*|Windows_NT)
        red "  Windows detected — use install.ps1 from PowerShell:"
        yellow "  powershell -ExecutionPolicy Bypass -File install.ps1"
        exit 1 ;;
    *) die "Unsupported OS: $(uname -s)" ;;
esac

# ── Arg parsing ───────────────────────────────────────────────────────────────
REPO_URL="${BROWSER_SCREENCAST_REPO:-https://github.com/reindertpelsma/browser-screencast.git}"
INSTALL_DIR="${BROWSER_SCREENCAST_HOME:-$HOME/.local/share/browser-screencast}"
BIN_DIR="${BROWSER_SCREENCAST_BIN:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/browser-screencast"
PORT=6081
WITH_SYSTEMD=0
HEADLESS=0
SKIP_DEPS=0
START_NOW=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)     PORT="$2"; shift 2 ;;
        --systemd)  WITH_SYSTEMD=1; shift ;;
        --headless) HEADLESS=1; shift ;;
        --no-deps)  SKIP_DEPS=1; shift ;;
        --start)    START_NOW=1; shift ;;
        --help|-h)
            cat <<'HELP'
browser-screencast installer

  --port PORT    web listen port (default 6081)
  --headless     start Xvfb + window manager (no physical display needed)
  --systemd      install and enable a systemd --user unit
  --no-deps      skip Python and system dependency installation
  --start        start server in the foreground after setup
HELP
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

step "browser-screencast installer"

# ── Python ────────────────────────────────────────────────────────────────────
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    if   command -v python3 >/dev/null 2>&1; then PYTHON=python3
    elif command -v python  >/dev/null 2>&1; then PYTHON=python
    else die "Python 3.9+ is required. Install python3 with your package manager."
    fi
fi
PY_VER="$("$PYTHON" -c 'import sys; print(sys.version_info[:2])')"
green "  Python: $PYTHON ($PY_VER)"

# ── Display detection ─────────────────────────────────────────────────────────
if   [[ -n "${WAYLAND_DISPLAY:-}" ]]; then DPY=wayland
elif [[ -n "${DISPLAY:-}" ]];         then DPY=x11
elif command -v Xvfb >/dev/null 2>&1; then DPY=headless-available
else                                       DPY=none
fi
green "  Display: $DPY"
if [[ "$DPY" == "wayland" ]]; then
    yellow "  Wayland capture not yet implemented — use an XWayland session or --capture vnc."
fi

# ── System packages ───────────────────────────────────────────────────────────
if [[ "$SKIP_DEPS" -eq 0 ]] && command -v apt-get >/dev/null 2>&1; then
    step "Installing system packages (apt)"
    PKGS=(python3-pip)
    # Headless needs Xvfb and a window manager
    if [[ "$HEADLESS" -eq 1 ]]; then
        PKGS+=(xvfb openbox x11-utils)
    fi
    # x11grab needs xlib headers and python-xlib
    PKGS+=(python3-xlib libx11-dev)
    apt-get install -y -q "${PKGS[@]}" 2>/dev/null \
        || yellow "  apt-get failed (no sudo?); continuing — pip will fill the gaps."
fi

# ── Source ────────────────────────────────────────────────────────────────────
step "Setting up source"
if [[ -n "${BROWSER_SCREENCAST_SOURCE_DIR:-}" ]]; then
    SRC="$BROWSER_SCREENCAST_SOURCE_DIR"
    green "  Using source dir: $SRC"
elif [[ -f "./server.py" && -f "./requirements.txt" ]]; then
    SRC="$(pwd)"
    green "  Using current directory: $SRC"
else
    command -v git >/dev/null 2>&1 \
        || die "git is required for curl-bash install. Install git or clone the repo and run setup.sh."
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        yellow "  Updating existing clone at $INSTALL_DIR"
        git -C "$INSTALL_DIR" pull --ff-only \
            || yellow "  Pull failed (local changes?). Continuing with existing tree."
    else
        yellow "  Cloning $REPO_URL → $INSTALL_DIR"
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    SRC="$INSTALL_DIR"
    green "  Source ready: $SRC"
fi

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$CONFIG_DIR"

if [[ "$SRC" != "$INSTALL_DIR" ]]; then
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude .git "$SRC"/ "$INSTALL_DIR"/
    else
        rm -rf "$INSTALL_DIR"
        mkdir -p "$INSTALL_DIR"
        cp -R "$SRC"/. "$INSTALL_DIR"/
        rm -rf "$INSTALL_DIR/.git"
    fi
    green "  Copied to $INSTALL_DIR"
fi

# ── Python dependencies ───────────────────────────────────────────────────────
if [[ "$SKIP_DEPS" -eq 0 ]]; then
    step "Installing Python dependencies"
    if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
        die "pip not found. Install python3-pip and rerun."
    fi
    # Try --user first; fall back to --break-system-packages for PEP 668 hosts
    # (Ubuntu 24.04+ marks the system Python as externally managed).
    if ! "$PYTHON" -m pip install --user -q -r "$INSTALL_DIR/requirements.txt" 2>/dev/null; then
        yellow "  --user install failed; trying --break-system-packages (PEP 668 host)"
        "$PYTHON" -m pip install --break-system-packages -q -r "$INSTALL_DIR/requirements.txt"
    fi
    green "  Python dependencies installed"
fi

# ── Token ─────────────────────────────────────────────────────────────────────
TOKEN_FILE="$CONFIG_DIR/token"
if [[ ! -s "$TOKEN_FILE" ]]; then
    "$PYTHON" - <<'PY' > "$TOKEN_FILE"
import secrets
print(secrets.token_urlsafe(24))
PY
    chmod 600 "$TOKEN_FILE"
fi
TOKEN="$(cat "$TOKEN_FILE")"

# ── Wrapper script ────────────────────────────────────────────────────────────
step "Writing launcher"
cat > "$BIN_DIR/browser-screencast" <<EOF
#!/usr/bin/env sh
exec "$PYTHON" "$INSTALL_DIR/server.py" --password "$TOKEN" "\$@"
EOF
chmod +x "$BIN_DIR/browser-screencast"
green "  Launcher: $BIN_DIR/browser-screencast"

# ── systemd unit ──────────────────────────────────────────────────────────────
if [[ "$WITH_SYSTEMD" -eq 1 ]]; then
    step "Installing systemd user unit"
    command -v systemctl >/dev/null 2>&1 \
        || die "systemctl not found — cannot install systemd unit."
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$UNIT_DIR"
    TEMPLATE="browser-screencast.service.template"
    [[ "$HEADLESS" -eq 1 ]] && TEMPLATE="browser-screencast-headless.service.template"
    sed \
        -e "s#__BIN__#$BIN_DIR/browser-screencast#g" \
        -e "s#__PORT__#$PORT#g" \
        "$INSTALL_DIR/systemd/$TEMPLATE" \
        > "$UNIT_DIR/browser-screencast.service"
    systemctl --user daemon-reload
    systemctl --user enable --now browser-screencast.service
    green "  Unit enabled: browser-screencast.service"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
green "browser-screencast installed."
echo "  Token:  $TOKEN"
echo "  Run:    $BIN_DIR/browser-screencast --port $PORT"
if [[ "$HEADLESS" -eq 1 ]]; then
    echo "  Mode:   headless Xvfb (--headless --capture x11grab --input x11)"
fi
echo "  SSH:    ssh -L $PORT:localhost:$PORT user@<host>"
echo "  Open:   http://localhost:$PORT/?token=$TOKEN"
if [[ "$WITH_SYSTEMD" -eq 0 ]]; then
    echo
    yellow "  Tip: run with --systemd to install as a background service."
fi
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    yellow "  Add $BIN_DIR to your PATH or use the full path above."
fi

if [[ "$START_NOW" -eq 1 ]]; then
    step "Starting server"
    if [[ "$HEADLESS" -eq 1 ]]; then
        exec "$BIN_DIR/browser-screencast" --headless --capture x11grab --input x11 --port "$PORT"
    else
        exec "$BIN_DIR/browser-screencast" --port "$PORT"
    fi
fi
