#!/usr/bin/env bash
# usage: base.sh <target path>
# Print what chezmoi last wrote for a target, found by matching the hash in its persistent state
# against every historical version of the source file. Prints "(base not found)" when none matches.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
. "$here/../../lib/remote.sh"
czr_dispatch "$0" "$@"
set -- ${CZR_ARGS[@]+"${CZR_ARGS[@]}"}

p=${1:?target path}
want=$(chezmoi state dump --format json 2>/dev/null | jq -r --arg p "$p" '.entryState[$p].contentsSHA256 // empty')
if [ -z "$want" ]; then
  echo "(base not found: chezmoi has no recorded state for this path)"
  exit 0
fi

src=$(chezmoi source-path "$p" 2>/dev/null)
dir=$(chezmoi source-path 2>/dev/null)
rel=${src#"$dir"/}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

for h in $(chezmoi git -- log --format=%H -- "$rel" 2>/dev/null); do
  chezmoi git -- show "$h:$rel" >"$tmp/raw" 2>/dev/null || continue
  case $src in
    *.tmpl) chezmoi execute-template <"$tmp/raw" >"$tmp/out" 2>/dev/null || continue ;;
    *) cp "$tmp/raw" "$tmp/out" ;;
  esac
  if [ "$(czr_sha256 <"$tmp/out")" = "$want" ]; then
    cat "$tmp/out"
    exit 0
  fi
done
echo "(base not found: no version of the source in git history renders to the recorded hash)"
