#!/usr/bin/env bash
set -euo pipefail

case "$(uname -s)" in
    Darwin)
        printf '\033[31mERROR: This repo is for Linux/Windows. macOS users:\033[0m\n'
        printf '\033[33m  bash <(curl -fsSL https://raw.githubusercontent.com/reindertpelsma/macscreencast/main/install.sh)\033[0m\n'
        exit 1 ;;
esac

cd "$(dirname "$0")"
export BROWSER_SCREENCAST_SOURCE_DIR="$(pwd)"
exec bash ./install.sh "$@"
