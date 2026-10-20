#!/usr/bin/env bash
# Gather every fact the chezmoi:status report needs and print it as labelled blocks.
# The agent renders the report from these facts; nothing here is final layout, except
# the lines marked "copy verbatim". Always exits 0: a non-zero exit from an injected
# command aborts the skill.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
. "$here/../../lib/remote.sh"
czr_fail() {
  echo "STATUS_LINE (copy verbatim): ❗ errors"
  echo
  echo "### ERRORS"
  echo "$1"
  exit 0
}
czr_dispatch "$0" "$@"
set -- ${CZR_ARGS[@]+"${CZR_ARGS[@]}"}

CAP=${CHEZMOI_REPORT_CAP:-40}
common=(--no-pager --color=false --no-tty --skip-secrets)
stale='/^chezmoi: warning: config file template has changed/d'

if ! command -v chezmoi >/dev/null 2>&1; then
  echo "ERRORS: chezmoi is not installed or not on PATH"
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

short() { printf '%s' "${1/#$HOME/\~}"; }
tildify() { sed "s|$HOME|~|g"; }
sha() { czr_sha256; }
capped() {
  local text lines
  text=$(cat)
  lines=$(printf '%s\n' "$text" | wc -l | tr -d ' ')
  if [ "$lines" -gt "$CAP" ]; then
    printf '%s\n' "$text" | head -n "$CAP"
    echo "(+$((lines - CAP)) more lines)"
  else
    printf '%s\n' "$text"
  fi
}
numbered() {
  awk '
    /^@@/ {
      if (seen) print "..."
      seen = 1
      match($0, /-[0-9]+/); o = substr($0, RSTART + 1, RLENGTH - 1) + 0
      match($0, /\+[0-9]+/); n = substr($0, RSTART + 1, RLENGTH - 1) + 0
      inh = 1; next
    }
    !inh { next }
    /^-/  { printf "- %3d | %s\n", o++, substr($0, 2); next }
    /^\+/ { printf "+ %3d | %s\n", n++, substr($0, 2); next }
    /^ /  { printf "  %3d | %s\n", n++, substr($0, 2); o++; next }
    /^$/  { printf "  %3d |\n", n++; o++; next }
  '
}
counts() {
  awk '/^@@/ { inh = 1; next } inh && /^-/ { r++ } inh && /^\+/ { a++ } END { printf "%d %d\n", r, a }'
}
delta() {
  local r=$1 a=$2
  if [ "$r" -gt 0 ] && [ "$a" -eq 0 ]; then printf '−%d' "$r"
  elif [ "$r" -eq 0 ] && [ "$a" -gt 0 ]; then printf '+%d' "$a"
  else printf '±%d' $((r > a ? r : a)); fi
}

srcdir=$(chezmoi source-path 2>/dev/null || true)
rel() { printf '%s' "${1#"$srcdir"/}"; }

raw=$(chezmoi status "${common[@]}" --path-style=absolute </dev/null 2>&1 | sed "$stale")
entries=$(printf '%s\n' "$raw" | grep -E '^[ ADMR]{2} /' || true)
errors=$(printf '%s\n' "$raw" | grep -vE '^[ ADMR]{2} /' | grep . || true)

state=$(chezmoi state dump --format json 2>/dev/null || echo '{}')
entry_sha() { printf '%s' "$state" | jq -r --arg p "$1" '.entryState[$p].contentsSHA256 // empty'; }
script_ran() { printf '%s' "$state" | jq -r --arg n "$1" '[.scriptState[] | select(.name == $n)] | length'; }

# ---- repo
in_repo=0 upstream= uncommitted= unpushed= unpulled=
if chezmoi git -- rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  in_repo=1
  uncommitted=$(chezmoi git -- status --short 2>/dev/null | head -40 | grep . || true)
  if upstream=$(chezmoi git -- rev-parse --abbrev-ref '@{u}' 2>/dev/null); then
    unpushed=$(chezmoi git -- log --reverse --format='%h' '@{u}..HEAD' 2>/dev/null || true)
    unpulled=$(chezmoi git -- log --reverse --format='%h' 'HEAD..@{u}' 2>/dev/null || true)
  fi
fi
n_unpushed=$(printf '%s' "$unpushed" | grep -c . || true)
n_unpulled=$(printf '%s' "$unpulled" | grep -c . || true)
n_uncommitted=$(printf '%s' "$uncommitted" | grep -c . || true)

# ---- config drift
cfg_state=none cfg_new= cfg_old= fmt= tmplf= preview=
[ -n "$srcdir" ] && [ "$in_repo" = 1 ] && tmplf=$(compgen -G "$srcdir/.chezmoi.*.tmpl" | head -n 1 || true)
if [ -n "$tmplf" ]; then
  fmt=${tmplf%.tmpl}; fmt=${fmt##*.}
  preview=$(chezmoi init --dry-run --verbose --color=false --no-pager --no-tty </dev/null 2>&1)
  if [ $? -ne 0 ]; then
    cfg_state=unknown
  elif printf '%s\n' "$preview" | grep -q '^@@'; then
    cfg_state=stale
    cfg_old=$(chezmoi data --format json 2>/dev/null | jq -r '.chezmoi.configFile // empty')
    cfg_new=$tmp/new.$fmt
    chezmoi execute-template --init <"$tmplf" >"$cfg_new" 2>/dev/null || cfg_state=unknown
  fi
fi

# ---- classify status entries
: >"$tmp/paths"
scripts=()
while IFS= read -r line; do
  [ -n "$line" ] || continue
  p=${line:3}
  if [ "${line:0:1}" = " " ] && [ "${line:1:1}" = "R" ]; then scripts+=("$p"); else printf '%s\n' "$p" >>"$tmp/paths"; fi
done <<<"$entries"

# templates whose rendering changes when the config is regenerated, drifted or not
if [ "$cfg_state" = stale ]; then
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    s=$(chezmoi source-path "$p" 2>/dev/null)
    case $s in *.tmpl) ;; *) continue ;; esac
    case $s in *encrypted_* | */private_* | private_*) continue ;; esac
    if ! cmp -s <(chezmoi cat "${common[@]}" -- "$p" 2>/dev/null) \
      <(chezmoi --config "$cfg_new" cat "${common[@]}" -- "$p" 2>/dev/null); then
      printf '%s\n' "$p" >>"$tmp/paths"
    fi
  done < <(chezmoi managed "${common[@]}" --include=files --path-style=absolute 2>/dev/null)
fi
sort -u "$tmp/paths" -o "$tmp/paths"

n_hand=0 n_both=0 n_ahead=0 any_hidden=0
: >"$tmp/blocks"

# ---- files
while IFS= read -r p; do
  [ -n "$p" ] || continue
  st=$(printf '%s\n' "$entries" | awk -v p="$p" 'substr($0, 4) == p { print substr($0, 1, 2); exit }')
  [ -n "$st" ] || st="  "
  s=$(chezmoi source-path "$p" 2>/dev/null)
  case $s in
    *encrypted_*) kind=encrypted; emoji=📄 ;;
    *.tmpl) kind=template; emoji=🧩 ;;
    *) kind=plain; emoji=📄 ;;
  esac
  hidden=0
  case $s in *encrypted_* | */private_* | private_*) hidden=1; emoji=🔒 ;; esac

  disk_edited=no source_ahead=no
  [ "${st:0:1}" != " " ] && disk_edited=yes
  es=$(entry_sha "$p")
  if [ -n "$es" ] && [ "$hidden" = 0 ]; then
    ts=$(chezmoi cat "${common[@]}" -- "$p" 2>/dev/null | sha)
    [ "$ts" != "$es" ] && source_ahead=yes
  fi
  mark=
  if [ "$disk_edited" = yes ] && [ "$source_ahead" = yes ]; then mark=" 💥"; n_both=$((n_both + 1))
  elif [ "$disk_edited" = yes ]; then mark=" ⚠️"; n_hand=$((n_hand + 1))
  elif [ "$source_ahead" = yes ]; then n_ahead=$((n_ahead + 1)); fi

  {
    echo "### FILE $(short "$p")"
    echo "kind: $kind"
    echo "emoji: $emoji"
    echo "status: ${st:-none}"
    echo "source: $(rel "$s")"
    echo "disk_edited: $disk_edited"
    echo "source_ahead: $source_ahead"
    if [ "$hidden" = 1 ]; then
      any_hidden=1
      echo "contents: hidden (private or encrypted)"
      echo "file_delta (copy verbatim): ${mark# }"
      echo
      continue
    fi
    if [ "$st" != "  " ]; then
      d=$(chezmoi diff "${common[@]}" --exclude=scripts -- "$p" 2>/dev/null | sed "$stale")
      read -r r a < <(printf '%s\n' "$d" | counts)
      echo "file_delta (copy verbatim): $(delta "${r:-0}" "${a:-0}")$mark"
      echo "apply_diff (\`-\` = on disk now, \`+\` = what apply writes; numbers are line numbers on each side):"
      printf '%s\n' "$d" | numbered | capped
    else
      echo "file_delta: none (no drift, listed only because re-init changes its rendering)"
    fi
    if [ "$kind" = template ]; then
      echo "template_source ($(rel "$s")):"
      awk '{ printf "%3d | %s\n", NR, $0 }' "$s"
      if [ "$cfg_state" = stale ]; then
        rd=$(diff -u <(chezmoi cat "${common[@]}" -- "$p" 2>/dev/null) \
          <(chezmoi --config "$cfg_new" cat "${common[@]}" -- "$p" 2>/dev/null) | tail -n +3 | numbered)
        echo "reinit_diff (\`-\` = apply result today, \`+\` = apply result after re-init):"
        if [ -n "$rd" ]; then printf '%s\n' "$rd" | capped; else echo "(none)"; fi
      fi
    fi
    echo
  } >>"$tmp/blocks"
done <"$tmp/paths"

# ---- config block
cfg_block=
if [ "$cfg_state" = stale ]; then
  items=$(python3 -I - "$cfg_old" "$cfg_new" "$fmt" <<'PY'
import json, re, sys, tomllib

sys.stdout.reconfigure(encoding="utf-8")  # stdout is captured by $(...); Windows would default to the locale code page
old_p, new_p, fmt = sys.argv[1:4]
if fmt != "toml":
    sys.exit(0)

secret = re.compile(r"(token|secret|passw|api_?key|credential)", re.I)

def flat(d, pre=""):
    out = {}
    for k, v in d.items():
        key = pre + k
        out.update(flat(v, key + ".")) if isinstance(v, dict) else out.__setitem__(key, v)
    return out

def load(p):
    with open(p, "rb") as f:
        return flat(tomllib.load(f))

def label(key):
    head, _, tail = key.rpartition(".")
    return f"[{head}] {tail}" if head else tail

old, new = load(old_p), load(new_p)
for k in sorted(set(old) | set(new)):
    a, b = old.get(k), new.get(k)
    if k in old and k in new and a == b:
        continue
    show = lambda present, v: "(absent)" if not present else ("<redacted>" if secret.search(k) else json.dumps(v))
    sym = "➕" if k not in old else ("➖" if k not in new else "✏️")
    print(f"- {sym} {label(k)}: {show(k in old, a)} -> {show(k in new, b)}")
PY
  )
  modes=$(printf '%s\n' "$preview" | awk -v f="/$(basename "$cfg_old")" '
    /^diff --git/ { cur = ($NF ~ (f "$")) ? 1 : 0; next }
    cur && /^old mode / { om = substr($3, length($3) - 2) }
    cur && /^new mode / { nm = substr($3, length($3) - 2) }
    END { if (om != "" && nm != "" && om != nm) print om, nm }')
  if [ -n "$modes" ]; then
    read -r om nm <<<"$modes"
    items="${items:+$items
}- ✏️ file mode: 0$om -> 0$nm"
  fi
  total=$(printf '%s\n' "$items" | grep -c . || true)
  adds=$(printf '%s\n' "$items" | grep -c '^- ➕' || true)
  dels=$(printf '%s\n' "$items" | grep -c '^- ➖' || true)
  if [ "$adds" -eq "$total" ]; then cd="+$total"; elif [ "$dels" -eq "$total" ]; then cd="−$total"; else cd="±$total"; fi
  cfg_block="### CONFIG $(short "$cfg_old")
emoji: ⚙️
file_delta (copy verbatim): $cd
changes (local config -> config the template would generate):
$items
"
fi

# ---- scripts block
: >"$tmp/scripts"
for p in "${scripts[@]}"; do
  s=$(chezmoi source-path "$p" 2>/dev/null)
  b=$(basename "$s")
  name=$(basename "$p")
  case $b in
    run_once_*) tag="[first run]" ;;
    run_onchange_*) if [ "$(script_ran "$name")" -gt 0 ]; then tag="[content changed]"; else tag="[first run]"; fi ;;
    run_before_*) tag="[every apply · before files]" ;;
    *) tag="[every apply]" ;;
  esac
  {
    echo "### SCRIPT $name"
    echo "emoji: ▶️"
    echo "tag (copy verbatim): $tag"
    echo "body:"
    chezmoi cat "${common[@]}" -- "$p" 2>/dev/null | head -n 40 | tildify
    echo
  } >>"$tmp/scripts"
done

# ---- status line
parts=()
[ "$n_hand" -gt 0 ] && parts+=("⚠️ $n_hand")
[ "$n_both" -gt 0 ] && parts+=("💥 $n_both")
[ "$n_ahead" -gt 0 ] && parts+=("📦 $n_ahead")
[ "${#scripts[@]}" -gt 0 ] && parts+=("▶️ ${#scripts[@]}")
[ "$cfg_state" = stale ] && parts+=("⚙️ 1")
[ "$cfg_state" = unknown ] && parts+=("⚙️ ?")
[ "$n_unpushed" -gt 0 ] && parts+=("⬆️ $n_unpushed")
[ "$n_unpulled" -gt 0 ] && parts+=("⬇️ $n_unpulled")
[ "$n_uncommitted" -gt 0 ] && parts+=("📝 $n_uncommitted")
[ "$in_repo" = 1 ] && [ -z "$upstream" ] && parts+=("🔗 no upstream")
[ -n "$errors" ] && parts+=("❗ errors")

host=
[ -n "${CZR_HOST:-}" ] && host=" · 🌐 $CZR_HOST"

if [ "${#parts[@]}" -eq 0 ]; then
  echo "✅ in sync$host"
  exit 0
fi

joined=""
for x in "${parts[@]}"; do joined="${joined:+$joined · }$x"; done
echo "STATUS_LINE (copy verbatim): $joined$host"
[ -n "${CZR_FLAG:-}" ] && echo "REMOTE (copy verbatim): $CZR_FLAG"
echo "OFFER (copy verbatim): 🛠️ \`chezmoi:resolve${CZR_FLAG:+ $CZR_FLAG}\` can fix all of this"
if [ -s "$tmp/blocks" ] || [ -n "$cfg_block" ] || [ -s "$tmp/scripts" ]; then
echo "LEGEND (copy verbatim): 📄 plain · 🧩 template · ⚙️ config · 🔒 hidden · ± lines · ➕ added · ➖ removed · ✏️ changed · 🔁 only on re-init · ⚠️ erases a hand edit · 💥 both sides changed · ▶️ script · ∅ absent · blank = same as Apply"
fi
echo

if [ -n "$errors" ]; then
  echo "### ERRORS"
  printf '%s\n' "$errors" | capped
  echo
fi
cat "$tmp/blocks"
[ -n "$cfg_block" ] && printf '%s\n' "$cfg_block"
if [ "$cfg_state" = unknown ]; then
  echo "### CONFIG"
  echo "could not compare: chezmoi init --dry-run failed"
  printf '%s\n' "$preview" | head -n 4
  echo
fi
cat "$tmp/scripts"

# ---- repo block
if [ "$in_repo" = 1 ] && { [ -n "$uncommitted" ] || [ -n "$unpushed" ] || [ -n "$unpulled" ] || [ -z "$upstream" ]; }; then
  echo "### REPO"
  [ -z "$upstream" ] && echo "upstream: none"
  if [ -n "$uncommitted" ]; then
    echo "uncommitted:"
    while IFS= read -r l; do echo "- ${l:3}"; done <<<"$uncommitted"
  fi
  commit_lines() {
    local h
    while IFS= read -r h; do
      [ -n "$h" ] || continue
      subj=$(chezmoi git -- log -1 --format='%s' "$h" 2>/dev/null)
      files=$(chezmoi git -- diff-tree --no-commit-id --name-only -r "$h" 2>/dev/null | paste -sd, - | sed 's/,/, /g')
      echo "- $h $subj (touches: $files)"
    done
  }
  if [ -n "$unpushed" ]; then echo "unpushed (oldest first):"; printf '%s\n' "$unpushed" | commit_lines; fi
  if [ -n "$unpulled" ]; then
    echo "unpulled, as of the last fetch (oldest first):"; printf '%s\n' "$unpulled" | commit_lines
    overlap=
    while IFS= read -r l; do
      f=${l:3}
      for h in $unpulled; do
        if chezmoi git -- diff-tree --no-commit-id --name-only -r "$h" 2>/dev/null | grep -qxF "$f"; then overlap="${overlap:+$overlap, }$f"; fi
      done
    done <<<"$uncommitted"
    [ -n "$overlap" ] && echo "uncommitted files also touched by an unpulled commit: $overlap"
  fi
fi
exit 0
