#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${BROWSER_SCREENCAST_REPO:-https://github.com/reindertpelsma/browser-screencast.git}"
INSTALL_DIR="${BROWSER_SCREENCAST_HOME:-$HOME/.local/share/browser-screencast}"
BIN_DIR="${BROWSER_SCREENCAST_BIN:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/browser-screencast"
PORT="${PORT:-6081}"

usage() {
  cat <<'USAGE'
browser-screencast installer

Options:
  --systemd       install and start a systemd --user unit
  --headless      configure/start with server-managed Xvfb + lightweight WM
  --no-deps       skip Python dependency installation
  --start         start the server in the foreground after setup
  --help          show this help
USAGE
}

WITH_SYSTEMD=0
HEADLESS=0
SKIP_DEPS=0
START_NOW=0
for arg in "$@"; do
  case "$arg" in
    --systemd) WITH_SYSTEMD=1 ;;
    --headless) HEADLESS=1 ;;
    --no-deps) SKIP_DEPS=1 ;;
    --start) START_NOW=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage; exit 2 ;;
  esac
done

case "$(uname -s)" in
  Darwin)
    echo "macOS detected. Use macscreencast instead:"
    echo "  bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/macscreencast/main/install.sh)"
    exit 0
    ;;
  Linux|FreeBSD|OpenBSD) ;;
  CYGWIN*|MINGW*|MSYS*|Windows_NT)
    echo "Windows detected. Use install.ps1 from PowerShell:"
    echo "  powershell -ExecutionPolicy Bypass -File install.ps1"
    exit 0
    ;;
  *)
    echo "Unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac

if command -v apt-get >/dev/null; then PKG=apt
elif command -v dnf >/dev/null; then PKG=dnf
elif command -v pacman >/dev/null; then PKG=pacman
elif command -v zypper >/dev/null; then PKG=zypper
elif command -v xbps-install >/dev/null; then PKG=xbps
else PKG=unknown
fi

if [ -n "${WAYLAND_DISPLAY:-}" ]; then DPY=wayland
elif [ -n "${DISPLAY:-}" ]; then DPY=x11
elif command -v Xvfb >/dev/null; then DPY=headless-available
else DPY=none
fi

echo "[detected] package-manager=$PKG display=$DPY"
if [ "$DPY" = "wayland" ]; then
  echo "[warning] Wayland capture is not implemented in this v0.1 scaffold; use an X11 session or VNC pass-through."
elif [ "$DPY" = "none" ]; then
  echo "[warning] no DISPLAY/WAYLAND_DISPLAY found. Use --headless, start Xvfb manually, or use --capture vnc."
elif [ "$DPY" = "headless-available" ]; then
  echo "[detected] Xvfb is available. Use --headless for server-managed Xvfb."
fi

if [ "$HEADLESS" = "1" ]; then
  if ! command -v Xvfb >/dev/null; then
    echo "--headless requires Xvfb. Install xvfb with your package manager." >&2
    exit 1
  fi
  if ! command -v openbox >/dev/null && ! command -v xfwm4 >/dev/null && ! command -v fluxbox >/dev/null && ! command -v i3 >/dev/null; then
    echo "[warning] no lightweight window manager found; install openbox/xfwm4/fluxbox/i3 for usable headless desktop."
  fi
fi

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if command -v python3 >/dev/null; then PYTHON=python3
  elif command -v python >/dev/null; then PYTHON=python
  else echo "Python 3.9+ is required." >&2; exit 1
  fi
fi

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$CONFIG_DIR"
if [ -n "${BROWSER_SCREENCAST_SOURCE_DIR:-}" ]; then
  SRC="$BROWSER_SCREENCAST_SOURCE_DIR"
elif [ -f "./server.py" ] && [ -f "./requirements.txt" ]; then
  SRC="$(pwd)"
else
  if ! command -v git >/dev/null; then
    echo "git is required for curl-bash install. Install git or clone the repo and run setup.sh." >&2
    exit 1
  fi
  if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
  else
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
  SRC="$INSTALL_DIR"
fi

if [ "$SRC" != "$INSTALL_DIR" ]; then
  if command -v rsync >/dev/null; then
    rsync -a --delete --exclude .git "$SRC"/ "$INSTALL_DIR"/
  else
    rm -rf "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    cp -R "$SRC"/. "$INSTALL_DIR"/
    rm -rf "$INSTALL_DIR/.git"
  fi
fi

if [ "$SKIP_DEPS" = "0" ]; then
  if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "Python pip is required. Install python3-pip with your package manager and rerun setup." >&2
    exit 1
  fi
  "$PYTHON" -m pip install --user -r "$INSTALL_DIR/requirements.txt"
fi

TOKEN_FILE="$CONFIG_DIR/token"
if [ ! -s "$TOKEN_FILE" ]; then
  "$PYTHON" - <<'PY' > "$TOKEN_FILE"
import secrets
print(secrets.token_urlsafe(24))
PY
  chmod 600 "$TOKEN_FILE"
fi
TOKEN="$(cat "$TOKEN_FILE")"

cat > "$BIN_DIR/browser-screencast" <<EOF
#!/usr/bin/env sh
exec "$PYTHON" "$INSTALL_DIR/server.py" --password "$TOKEN" "\$@"
EOF
chmod +x "$BIN_DIR/browser-screencast"

if [ "$WITH_SYSTEMD" = "1" ]; then
  if ! command -v systemctl >/dev/null; then
    echo "systemctl not found; cannot install systemd --user unit." >&2
    exit 1
  fi
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UNIT_DIR"
  TEMPLATE="browser-screencast.service.template"
  if [ "$HEADLESS" = "1" ]; then
    TEMPLATE="browser-screencast-headless.service.template"
  fi
  sed \
    -e "s#__BIN__#$BIN_DIR/browser-screencast#g" \
    -e "s#__PORT__#$PORT#g" \
    "$INSTALL_DIR/systemd/$TEMPLATE" \
    > "$UNIT_DIR/browser-screencast.service"
  systemctl --user daemon-reload
  systemctl --user enable --now browser-screencast.service
fi

echo
echo "Installed browser-screencast."
echo "Token:  $TOKEN"
echo "Run:    $BIN_DIR/browser-screencast --port $PORT"
if [ "$HEADLESS" = "1" ]; then
  echo "Mode:   headless Xvfb"
fi
echo "SSH:    ssh -L $PORT:localhost:$PORT user@<host>"
echo "Open:   http://localhost:$PORT/?token=$TOKEN"
if [ "$WITH_SYSTEMD" = "0" ]; then
  echo "Daemon: $0 --systemd"
fi

if [ "$START_NOW" = "1" ]; then
  if [ "$HEADLESS" = "1" ]; then
    exec "$BIN_DIR/browser-screencast" --headless --capture x11 --input x11 --port "$PORT"
  else
    exec "$BIN_DIR/browser-screencast" --port "$PORT"
  fi
fi
