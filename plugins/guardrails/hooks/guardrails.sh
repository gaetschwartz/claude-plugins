#!/bin/sh
data=${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/guardrails-gaetans-claude-plugins}
py=$data/venv/bin/python
flags="-I"
if ! [ -x "$py" ] || ! [ -f "$data/venv/guardrails-ast-ready" ]; then
  flags="-I -S"
  py=
  oldifs=$IFS
  IFS=:
  for dir in $PATH; do
    if [ -x "$dir/python3" ]; then py=$dir/python3; break; fi
  done
  IFS=$oldifs
  if [ -z "$py" ]; then
    for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
      if [ -x "$cand" ]; then py=$cand; break; fi
    done
  fi
  if [ -z "$py" ] && [ -x /home/linuxbrew/.linuxbrew/bin/python3 ] && [ "$(uname -s)" = Linux ]; then
    py=/home/linuxbrew/.linuxbrew/bin/python3
  fi
  if [ -z "$py" ] && [ -x /usr/bin/python3 ]; then py=/usr/bin/python3; fi
  if [ -z "$py" ]; then
    printf '%s' '{"systemMessage":"guardrails: no python3 was found, so this command was not checked."}'
    exit 0
  fi
fi
exec "$py" $flags "${0%/*}/../lib/guard.py" "$@"
