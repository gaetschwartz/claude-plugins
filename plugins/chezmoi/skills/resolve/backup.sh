#!/usr/bin/env bash
# usage: backup.sh <path>...
# Copy each existing path into a timestamped backup directory, keeping its place relative to $HOME,
# and print the directory. Run before anything that overwrites a deployed file.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
. "$here/../../lib/remote.sh"
czr_dispatch "$0" "$@"
set -- ${CZR_ARGS[@]+"${CZR_ARGS[@]}"}

d="${XDG_STATE_HOME:-$HOME/.local/state}/chezmoi-resolve/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$d"
for p in "$@"; do
  [ -e "$p" ] || continue
  rel=${p#"$HOME"/}
  mkdir -p "$d/$(dirname "$rel")"
  cp -p "$p" "$d/$rel"
done
echo "$d"
