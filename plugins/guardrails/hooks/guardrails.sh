#!/bin/sh
# guardrails.sh: no argument = the PreToolUse hook, `session-start`, or `cli ARGS...`
mode=hook
case ${1-} in session-start | cli) mode=$1; shift ;; esac
lib=${0%/*}/../lib
proj=${CLAUDE_PROJECT_DIR:-/nonexistent}
here=$PWD
for dir in / "$HOME"; do
  if [ "$proj" = "$dir" ]; then proj=/nonexistent; fi
  if [ "$here" = "$dir" ]; then here=/nonexistent; fi
done

usable() {
  case $1 in /*) [ -x "$1" ] ;; *) return 1 ;; esac
}

works() { "$1" -I -S -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; }

find_python() {
  py=
  broken=
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if usable "$cand"; then
      if works "$cand"; then py=$cand; return; fi
      : "${broken:=$cand}"
    fi
  done
  if [ "$(/usr/bin/uname -s)" = Linux ] && usable /home/linuxbrew/.linuxbrew/bin/python3; then
    if works /home/linuxbrew/.linuxbrew/bin/python3; then py=/home/linuxbrew/.linuxbrew/bin/python3; return; fi
    : "${broken:=/home/linuxbrew/.linuxbrew/bin/python3}"
  fi
  oldifs=$IFS
  IFS=:
  for dir in $PATH; do
    case $dir/ in "$proj"/* | "$here"/*) continue ;; esac
    if usable "$dir/python3"; then
      if works "$dir/python3"; then py=$dir/python3; break; fi
      : "${broken:=$dir/python3}"
    fi
  done
  IFS=$oldifs
}

say() {
  case $mode in
    hook) printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}' "$1" "$1" ;;
    session-start) printf '{"systemMessage":"%s"}' "$1" ;;
    *) printf '%s\n' "$1" >&2; exit 2 ;;
  esac
  exit 0
}

if [ "$mode" != session-start ] && [ -n "${CLAUDE_PLUGIN_DATA-}" ] && read -r id < "$lib/runtime-id"; then
  rt=$CLAUDE_PLUGIN_DATA/runtime/$id
  case $rt in
    /*)
      if [ -f "$rt/marker.json" ] && [ ! -e "$rt/broken" ] && [ -x "$rt/venv/bin/python" ]; then
        "$rt/venv/bin/python" -I "$lib/guard.py" "$@"
        code=$?
        if [ "$mode" = cli ]; then exit "$code"; fi
        if [ "$code" -ne 0 ]; then
          say "[guardrails plugin notice] the guardrails hook failed to run (exit $code), so guardrails rules are NOT enforced and this command was not checked. Tell the user about this now."
        fi
        exit 0
      fi
      ;;
  esac
fi

find_python
if [ -z "$py" ]; then
  if [ -n "$broken" ]; then
    clean=$(printf '%s' "$broken" | /usr/bin/tr -c 'A-Za-z0-9/._-' '?')
    [ "$mode" = cli ] || printf 'guardrails: broken python3 at %s\n' "$clean" >&2
    say "[guardrails plugin notice] python3 is broken (it cannot run), so the guardrails runtime cannot be installed and guardrails rules are NOT enforced. Tell the user about this now."
  fi
  say "[guardrails plugin notice] No usable python3 (3.9 or newer) was found, so the guardrails runtime cannot be installed and guardrails rules are NOT enforced. Tell the user about this now."
fi
if [ "$mode" = cli ]; then mode=run; fi
exec "$py" -I -S "$lib/bootstrap.py" "$mode" "$@"
