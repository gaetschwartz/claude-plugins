#!/usr/bin/env bash
# Print the chezmoi state sections that have something to report, and nothing for the rest.
# Consumed by the chezmoi:status skill; the "==" lines are markers for the rendering agent,
# not the user. Always exits 0: a non-zero exit from an injected command aborts the skill.
set -uo pipefail

CAP=${CHEZMOI_STATE_CAP:-60}
MAX_ENTRIES=${CHEZMOI_STATE_MAX_ENTRIES:-8}
common=(--no-pager --color=false --no-tty --skip-secrets)
# The config section reports this itself; as a stray line it would pose as drift.
stale_warning='/^chezmoi: warning: config file template has changed/d'

if ! command -v chezmoi >/dev/null 2>&1; then
  echo "(chezmoi is not installed or not on PATH)"
  exit 0
fi

printed=0
workdir=
trap '[ -n "$workdir" ] && rm -rf "$workdir"' EXIT

section() {
  printed=1
  echo "== $1 =="
}

capped() {
  local text lines
  text=$(cat)
  lines=$(printf '%s\n' "$text" | wc -l | tr -d ' ')
  if [ "$lines" -gt "$CAP" ]; then
    printf '%s\n' "$text" | head -n "$CAP"
    echo "(truncated: $((lines - CAP)) more lines)"
  else
    printf '%s\n' "$text"
  fi
}

# Destination drift, with the source type of each entry so the caller need not look it up.
out=$(chezmoi status "${common[@]}" --path-style=absolute </dev/null 2>&1 | sed "$stale_warning")
entries=$(printf '%s\n' "$out" | grep -E '^[ ADMR]{2} /' || true)
errors=$(printf '%s\n' "$out" | grep -vE '^[ ADMR]{2} /' | grep . || true)
encrypted=()
if [ -n "$entries" ]; then
  section "destination drift"
  n=0
  while IFS= read -r line; do
    n=$((n + 1))
    if [ "$n" -gt 25 ]; then
      echo "(+$(($(printf '%s\n' "$entries" | wc -l) - 25)) more entries not shown)"
      break
    fi
    src=$(chezmoi source-path "${line:3}" 2>/dev/null)
    case $src in
      *encrypted_*) kind=encrypted; encrypted+=("${line:3}") ;;
      *.tmpl) kind=template ;;
      *) kind=plain ;;
    esac
    echo "$line  [$kind source: ${src:-unknown}]"
  done <<<"$entries"
  cat <<'EOF'
Columns: first = destination changed since chezmoi last wrote it; second = what apply would do.
M modified, A created, D deleted, R script will run. "MM" means apply would overwrite a local edit.
EOF
fi
if [ -n "$errors" ]; then
  section "chezmoi status errors"
  printf '%s\n' "$errors" | capped
fi

if [ ${#encrypted[@]} -gt 0 ]; then
  enc=$(chezmoi dump-config --format=json 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('encryption') or 'configured')" 2>/dev/null || echo configured)
  section "encryption"
  echo "encrypted entries drifted (encryption: $enc): re-add re-encrypts them on its own."
fi

# Source tree: uncommitted changes, then position against the remote.
in_repo=0
if chezmoi git -- rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  in_repo=1
  tree=$(chezmoi git -- status --short 2>&1 | head -40 | grep . || true)
  if [ -n "$tree" ]; then
    section "source repo: uncommitted changes"
    printf '%s\n' "$tree"
  fi
  if up=$(chezmoi git -- rev-parse --abbrev-ref '@{u}' 2>/dev/null); then
    ahead=$(chezmoi git -- rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
    behind=$(chezmoi git -- rev-list --count 'HEAD..@{u}' 2>/dev/null || echo 0)
    if [ "${ahead:-0}" != 0 ] || [ "${behind:-0}" != 0 ]; then
      section "sync position"
      echo "tracks $up: $ahead unpushed, $behind unpulled (as of the last fetch; run 'chezmoi git -- fetch' before claiming the remote has nothing new)"
    fi
  else
    section "sync position"
    echo "the source branch has no upstream, so there is nothing to push to or pull from"
  fi
else
  section "source repo"
  echo "the source directory is not a git repository (config drift was not checked)"
fi

# autoCommit / autoPush change what capturing an edit does.
auto=$(chezmoi dump-config --format=json 2>/dev/null | python3 -c "
import json, sys
g = {k.lower(): v for k, v in (json.load(sys.stdin).get('git') or {}).items()}
on = [n for k, n in (('autocommit', 'autoCommit'), ('autopush', 'autoPush')) if g.get(k)]
print(' and '.join(on))" 2>/dev/null || true)
if [ -n "$auto" ]; then
  section "capturing commits"
  echo "$auto enabled: add and re-add will also commit/push on their own."
fi

# Config drift: what a regenerated config would write, and what that would do to the tree.
src=$(chezmoi source-path 2>/dev/null || true)
tmpl=
# chezmoi init --dry-run still creates a git repository in a source that lacks one.
[ -n "$src" ] && [ "$in_repo" = 1 ] && tmpl=$(compgen -G "$src/.chezmoi.*.tmpl" | head -n 1 || true)
if [ -n "$tmpl" ]; then
  fmt=${tmpl%.tmpl}
  fmt=${fmt##*.}
  # init --dry-run is the authority on what would be written, and fails on a prompt it
  # cannot answer. execute-template --init does not fail there: it substitutes the question.
  # With --no-tty a prompt reads stdin, so an open pipe would hang it: close stdin.
  preview=$(chezmoi init --dry-run --verbose --color=false --no-pager --no-tty </dev/null 2>&1)
  rc=$?
  if [ $rc -ne 0 ]; then
    section "config: cannot preview"
    if printf '%s\n' "$preview" | grep -q '^panic:'; then
      echo "chezmoi init --dry-run panicked: $(printf '%s\n' "$preview" | head -n 1 | cut -c1-200)"
      echo "(a known failure when the config file lives outside the destination directory; the config was not compared)"
    else
      printf '%s\n' "$preview" | head -n 8
    fi
  else
    hunk=$(printf '%s\n' "$preview" | awk '
      /^diff --git/ { if (keep) printf "%s", block; block = ""; keep = 0 }
      { block = block $0 "\n" }
      /^@@/ { keep = 1 }
      END { if (keep) printf "%s", block }')
    if [ -n "$hunk" ]; then
      section "config: init would write"
      printf '%s\n' "$hunk" | awk '
        tolower($0) ~ /(token|secret|passw|api_?key|credential)[a-z0-9_]*[ \t]*=/ {
          sub(/=.*/, "= <redacted>"); redacted = 1
        }
        { print }
        END { if (redacted) print "(values of secret-looking keys redacted)" }' | capped
      env_vars=$(grep -o 'env "CHEZMOI_[A-Z0-9_]*"' "$tmpl" | tr -d '"' | sed 's/^env //' | sort -u)
      for v in $env_vars; do
        if [ -n "${!v+x}" ]; then echo "template reads $v (set here)"; else echo "template reads $v (unset here; the user's shell may differ)"; fi
      done

      # Impact on the tree under the regenerated config. The --init flags of status/diff/apply
      # keep the old [data] keys visible to templates, so they understate it; render the file
      # and point --config at it instead, which is what the config is after a real init.
      workdir=$(mktemp -d)
      newcfg=$workdir/config.$fmt
      if chezmoi execute-template --init <"$tmpl" >"$newcfg" 2>/dev/null; then
        new=$(chezmoi --config "$newcfg" status "${common[@]}" --path-style=absolute </dev/null 2>/dev/null | grep -E '^[ ADMR]{2} /' || true)
        changes=$(awk '
          NR == FNR { if ($0 != "") cur[substr($0, 4)] = substr($0, 1, 2); next }
          $0 != "" { p = substr($0, 4); nw[p] = substr($0, 1, 2) }
          END {
            for (p in cur) if (!(p in nw)) print p "\t" cur[p] "\t--"
            for (p in nw) if (!(p in cur) || cur[p] != nw[p]) print p "\t" ((p in cur) ? cur[p] : "--") "\t" nw[p]
          }' <(printf '%s\n' "$entries") <(printf '%s\n' "$new") | sort)
        if [ -n "$changes" ]; then
          section "config: effect of regenerating on managed entries"
          echo "status now -> after regeneration ('--' = no drift):"
          n=0
          while IFS=$'\t' read -r path was now; do
            printf '%s: %s -> %s\n' "$path" "$was" "$now"
          done <<<"$changes"
          while IFS=$'\t' read -r path was now; do
            case $now in *R) continue ;; esac
            [ "$now" = "--" ] && continue
            n=$((n + 1))
            if [ "$n" -gt "$MAX_ENTRIES" ]; then
              echo "(more entries not shown)"
              break
            fi
            s=$(chezmoi source-path "$path" 2>/dev/null)
            case $s in
              *encrypted_* | */private_* | private_*)
                echo "== $path :: rendering differs :: contents not shown (private or encrypted source) =="
                continue
                ;;
            esac
            d=$(diff -u <(chezmoi cat "${common[@]}" -- "$path" 2>/dev/null) \
              <(chezmoi --config "$newcfg" cat "${common[@]}" -- "$path" 2>/dev/null) | tail -n +3)
            [ -n "$d" ] || continue
            echo "== $path :: rendering under the current config (-) vs a regenerated one (+) =="
            printf '%s\n' "$d" | capped
          done <<<"$changes"
        fi
      fi
    fi
  fi
fi

if [ "$printed" = 0 ]; then
  echo "(in sync: no destination drift, source tree clean, nothing to push or pull, config current)"
fi
exit 0
