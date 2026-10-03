#!/bin/sh
# guardrails.sh: no argument = the PreToolUse hook, `session-start`, or `cli ARGS...`
mode=hook
case ${1-} in session-start | cli) mode=$1; shift ;; esac
lib=${0%/*}/../lib
proj=${CLAUDE_PROJECT_DIR:-/nonexistent}
here=$PWD
if [ "$here" = / ] || [ "$here" = "$HOME" ]; then here=/nonexistent; fi

trusted() {
  case $1 in "$proj"/* | "$here"/*) return 1 ;; esac
  [ -x "$1" ] || return 1
  perms=$(/bin/ls -ldL "$1" 2>/dev/null) || return 1
  case $perms in ????????w*) return 1 ;; esac
}

find_python() {
  py=
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if trusted "$cand"; then py=$cand; return; fi
  done
  if [ "$(/usr/bin/uname -s)" = Linux ] && trusted /home/linuxbrew/.linuxbrew/bin/python3; then
    py=/home/linuxbrew/.linuxbrew/bin/python3
    return
  fi
  oldifs=$IFS
  IFS=:
  for dir in $PATH; do
    if trusted "$dir/python3"; then py=$dir/python3; break; fi
  done
  IFS=$oldifs
}

if [ "$mode" = hook ] && [ -n "${CLAUDE_PLUGIN_DATA-}" ] && read -r id < "$lib/runtime-id"; then
  rt=$CLAUDE_PLUGIN_DATA/runtime/$id
  case $rt/ in
    "$proj"/* | "$here"/*) ;;
    *) if [ -f "$rt/marker.json" ] && [ -O "$rt/venv/bin/python" ] && [ -x "$rt/venv/bin/python" ]; then
         exec "$rt/venv/bin/python" -I "$lib/guard.py"
       fi ;;
  esac
fi

find_python
if [ -z "$py" ]; then
  text='[guardrails plugin notice] No usable python3 (3.9 or newer) was found, so the guardrails runtime cannot be installed and guardrails rules are NOT enforced. Tell the user about this now.'
  case $mode in
    hook) printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}' "$text" "$text" ;;
    session-start) printf '{"systemMessage":"%s"}' "$text" ;;
    *) printf '%s\n' "$text" >&2; exit 2 ;;
  esac
  exit 0
fi
if [ "$mode" = cli ]; then mode=run; fi
exec "$py" -I -S "$lib/bootstrap.py" "$mode" "$@"
