#!/usr/bin/env bash
# Emit one marked block per drifted chezmoi entry, diffed in the direction that drifted.
# Consumed by the chezmoi:diff skill; the markers are for the rendering agent, not the user.
set -uo pipefail

CAP=${CHEZMOI_DIFF_CAP:-150}
MAX_ENTRIES=${CHEZMOI_DIFF_MAX_ENTRIES:-15}
common=(--no-pager --color=false --no-tty --skip-secrets)

if ! status=$(chezmoi status "${common[@]}" --path-style=absolute ${1+"$@"} 2>&1); then
  echo "$status"
  exit 0
fi
if [ -z "$status" ]; then
  echo "(no difference)"
  exit 0
fi

n=0
total=$(printf '%s\n' "$status" | wc -l | tr -d ' ')
while IFS= read -r line; do
  st=${line:0:2}
  path=${line:3}
  n=$((n + 1))
  if [ "$n" -gt "$MAX_ENTRIES" ]; then
    echo "(+$((total - MAX_ENTRIES)) more drifted entries not shown)"
    break
  fi
  src=$(chezmoi source-path "$path" 2>/dev/null)
  if [ "${st:1:1}" = "R" ] && [ "${st:0:1}" = " " ]; then
    echo "== $path :: script :: source=$src =="
    continue
  fi
  if [ "${st:0:1}" != " " ]; then
    dir=dest-ahead
    flag=--reverse
  else
    dir=source-ahead
    flag=
  fi
  echo "== $path :: $dir :: status='$st' :: source=$src =="
  out=$(chezmoi diff "${common[@]}" --exclude=scripts ${flag:+"$flag"} -- "$path" 2>&1)
  lines=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
  if [ "$lines" -gt "$CAP" ]; then
    printf '%s\n' "$out" | head -n "$CAP"
    echo "(truncated: $((lines - CAP)) more lines)"
  else
    printf '%s\n' "$out"
  fi
done <<< "$status"
