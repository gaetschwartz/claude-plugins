#!/usr/bin/env bash
# Remote transport shared by the facts scripts and bin/czr. Sourced, never run.
#
# --ssh <value>: a single word is a host (`ssh <word>`); anything with a space is the whole
# command (`ssh -p 2222 me@box`). Scripts are shipped as a tar over the same connection and run
# on the remote, so the remote needs bash, chezmoi, git, jq, python3 >= 3.11, tar.

CZR_PATHFIX='PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/opt/homebrew/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"; export PATH; '

czr_sha256() { if command -v shasum >/dev/null 2>&1; then shasum -a 256; else sha256sum; fi | cut -d' ' -f1; }

czr_sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# One argument as the remote shell must see it. A leading ~/ or this machine's $HOME means the
# remote user's home, so a path typed for either side lands in the right place.
czr_arg() {
  case $1 in
    '~') printf '"$HOME"' ;;
    '~/'*) printf '"$HOME"/%s' "$(czr_sq "${1#'~/'}")" ;;
    "$HOME") printf '"$HOME"' ;;
    "$HOME"/*) printf '"$HOME"/%s' "$(czr_sq "${1#"$HOME"/}")" ;;
    *) czr_sq "$1" ;;
  esac
}

czr_untilde() {
  case $1 in
    '~') printf '%s' "$HOME" ;;
    '~/'*) printf '%s' "$HOME/${1#'~/'}" ;;
    *) printf '%s' "$1" ;;
  esac
}

czr_set_transport() {
  local v=$1 first
  case $v in
    '') return 1 ;;
    *[[:space:]]*) read -r -a CZR_SSH <<<"$v"; CZR_FLAG="--ssh $(czr_sq "$v")"; CZR_HOST=${CZR_SSH[${#CZR_SSH[@]} - 1]} ;;
    *) CZR_SSH=(ssh "$v"); CZR_FLAG="--ssh $v"; CZR_HOST=$v ;;
  esac
  first=${CZR_SSH[0]##*/}
  if [ "$first" = ssh ]; then
    CZR_SSH=("${CZR_SSH[0]}" -o BatchMode=yes -o "ConnectTimeout=${CZR_CONNECT_TIMEOUT:-10}" -o ServerAliveInterval=15 -o ServerAliveCountMax=2 "${CZR_SSH[@]:1}")
  fi
}

# Replaced by a script that wants its own failure layout.
czr_fail() { echo "ERRORS: $1"; exit 0; }

czr_unreachable() {
  local msg
  msg=$(grep . "$1" | head -n 1)
  czr_fail "cannot reach ${CZR_HOST}: ${msg:-ssh exited with 255}"
}

# czr_run_script <script path> [args]: run one of the plugin's scripts on the remote.
czr_run_script() {
  local self=$1 root rel cmd a err rc
  shift
  root=$(cd "$(dirname "$self")/../.." && pwd -P)
  rel=$(basename "$(dirname "$self")")/$(basename "$self")
  cmd="${CZR_PATHFIX}d=\$(mktemp -d) || exit 97; trap 'rm -rf \"\$d\"' EXIT; tar -xf - -C \"\$d\" || exit 98; "
  cmd+="CZR_REMOTE=1 CZR_HOST=$(czr_sq "$CZR_HOST") CZR_FLAG=$(czr_sq "$CZR_FLAG") bash \"\$d/skills/$rel\""
  for a in "$@"; do cmd+=" $(czr_arg "$a")"; done
  err=$(mktemp)
  COPYFILE_DISABLE=1 tar -C "$root" -cf - skills lib 2>/dev/null | "${CZR_SSH[@]}" "$cmd" 2>"$err"
  rc=${PIPESTATUS[1]}
  case $rc in
    255) czr_unreachable "$err" ;;
    97 | 98) czr_fail "remote setup failed on ${CZR_HOST} (exit $rc): $(grep . "$err" | head -n 1)" ;;
  esac
  rm -f "$err"
  return "$rc"
}

# Top of every script that takes --ssh:  czr_dispatch "$0" "$@"; set -- ${CZR_ARGS[@]+"${CZR_ARGS[@]}"}
# Runs the script remotely and exits when --ssh is given; otherwise leaves the other arguments in CZR_ARGS.
czr_dispatch() {
  local self=$1 ssh= a
  shift
  CZR_ARGS=()
  while [ $# -gt 0 ]; do
    case $1 in
      --ssh)
        if [ $# -lt 2 ]; then czr_fail "--ssh needs a host or an ssh command"; fi
        ssh=$2; shift 2 ;;
      --ssh=*) ssh=${1#--ssh=}; shift ;;
      *) CZR_ARGS+=("$1"); shift ;;
    esac
  done
  if [ -n "${CZR_REMOTE:-}" ]; then
    local out=()
    for a in ${CZR_ARGS[@]+"${CZR_ARGS[@]}"}; do out+=("$(czr_untilde "$a")"); done
    CZR_ARGS=(${out[@]+"${out[@]}"})
    return 0
  fi
  [ -n "$ssh" ] || return 0
  czr_set_transport "$ssh" || czr_fail "--ssh needs a host or an ssh command"
  czr_run_script "$self" ${CZR_ARGS[@]+"${CZR_ARGS[@]}"}
  exit 0
}

# Restricted file access on the machine that holds the files: reads and writes the chezmoi source
# and config directories, and reads managed targets. Never .git, never private or encrypted entries.
read -r -d '' CZR_FILE_SH <<'SH'
verb=$1 p=$2
deny() { echo "czr: $1" >&2; exit 2; }
case $p in
  "~") p=$HOME ;;
  "~/"*) p=$HOME/${p#"~/"} ;;
esac
case $p in /*) ;; *) deny "absolute path required: $p" ;; esac
case /$p/ in */../* | */.git/*) deny "path not allowed: $p" ;; esac
anc=$p
while [ ! -d "$anc" ]; do anc=$(dirname "$anc"); done
real=$(cd "$anc" && pwd -P)${p#"$anc"}
case /$real/ in */.git/*) deny "path not allowed: $p" ;; esac
src=$(chezmoi source-path 2>/dev/null) || deny "chezmoi source directory unknown"
src=$(cd "$src" && pwd -P)
cfg=${XDG_CONFIG_HOME:-$HOME/.config}/chezmoi
[ -d "$cfg" ] && cfg=$(cd "$cfg" && pwd -P)
ok=0 s=
case $real in "$src" | "$src"/* | "$cfg" | "$cfg"/*) ok=1 ;; esac
if [ "$ok" = 0 ] && [ "$verb" = get ]; then
  s=$(chezmoi source-path "$p" 2>/dev/null) && ok=1
fi
[ "$ok" = 1 ] || deny "outside the chezmoi source and config directories: $p"
case ${s:-$real} in *encrypted_* | */private_* | private_*) [ "$verb" = get ] && deny "contents are private or encrypted: $p"; case $real in *encrypted_*) deny "encrypted entry: $p" ;; esac ;; esac
case $verb in
  get)
    [ -f "$real" ] || deny "no such file: $p"
    cat "$real" ;;
  put)
    t=$(mktemp) || deny "no temporary file"
    cat >"$t"
    mkdir -p "$(dirname "$real")" || deny "cannot create the directory for $p"
    cat "$t" >"$real" || { rm -f "$t"; deny "cannot write $p"; }
    echo "czr: wrote $(wc -c <"$t" | tr -d ' ') bytes to $real"
    rm -f "$t" ;;
  *) deny "unknown verb: $verb" ;;
esac
SH

# czr_file get|put <path>: stdin is the content for put.
czr_file() {
  local verb=$1 path=$2 rc
  if [ -z "${CZR_SSH+x}" ]; then
    sh -c "$CZR_FILE_SH" sh "$verb" "$path"
    return
  fi
  "${CZR_SSH[@]}" "${CZR_PATHFIX}sh -c $(czr_sq "$CZR_FILE_SH") sh $verb $(czr_arg "$path")"
  rc=$?
  [ "$rc" -eq 255 ] && echo "czr: cannot reach ${CZR_HOST}" >&2
  return "$rc"
}
