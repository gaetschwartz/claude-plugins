#!/usr/bin/env bash
set -uo pipefail

PATH="/home/linuxbrew/.linuxbrew/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v python3 >/dev/null 2>&1 || exit 0
exec python3 "$HERE/shell_guard.py"
