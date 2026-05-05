#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export BROWSER_SCREENCAST_SOURCE_DIR="$(pwd)"
exec bash ./install.sh "$@"
