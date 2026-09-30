#!/bin/sh
data=${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/guardrails-gaetans-claude-plugins}
guard=${0%/*}/../lib/guard.py
venv=$data/venv/bin/python
proj=${CLAUDE_PROJECT_DIR:-/nonexistent}
here=$PWD
if [ "$here" = / ] || [ "$here" = "$HOME" ]; then here=/nonexistent; fi

find_python() {
  py=
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$cand" ]; then py=$cand; return; fi
  done
  if [ "$(uname -s)" = Linux ] && [ -x /home/linuxbrew/.linuxbrew/bin/python3 ]; then
    py=/home/linuxbrew/.linuxbrew/bin/python3
    return
  fi
  oldifs=$IFS
  IFS=:
  for dir in $PATH; do
    case $dir/ in "$proj"/* | "$here"/*) continue ;; esac
    if [ -x "$dir/python3" ]; then py=$dir/python3; break; fi
  done
  IFS=$oldifs
  if [ -z "$py" ] && [ -x /usr/bin/python3 ]; then py=/usr/bin/python3; fi
}

if [ -x "$venv" ] && [ -f "$data/venv/guardrails-ast-ready" ]; then
  if [ $# -gt 0 ]; then exec "$venv" -I "$guard" "$@"; fi
  input=$(cat)
  if out=$(printf '%s' "$input" | "$venv" -I "$guard"); then
    printf '%s' "$out"
    exit 0
  fi
  find_python
  if [ -n "$py" ]; then
    printf '%s' "$input" | "$py" -I -S "$guard" --venv-failed
    exit 0
  fi
  printf '%s' '{"systemMessage":"guardrails: the venv python failed and no other python3 was found, so this command was not checked."}'
  exit 0
fi

find_python
if [ -z "$py" ]; then
  printf '%s' '{"systemMessage":"guardrails: no python3 was found, so this command was not checked."}'
  exit 0
fi
exec "$py" -I -S "$guard" "$@"
