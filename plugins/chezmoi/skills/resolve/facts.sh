#!/usr/bin/env bash
# Print the status facts, then what resolving needs on top of them: per drifted file its class,
# recent source history and, where the last-written content can be recovered, a three-way merge preview.
# Always exits 0: a non-zero exit from an injected command aborts the skill.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
out=$("$here/../status/report.sh")
printf '%s\n' "$out"
[ "$out" = "✅ in sync" ] && exit 0

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
srcdir=$(chezmoi source-path 2>/dev/null || true)

echo
echo "### RESOLVE-FACTS"
auto=$(chezmoi dump-config --format=json 2>/dev/null | jq -r '(.git // {}) | "autoCommit=\(.autoCommit // .autocommit // false) autoPush=\(.autoPush // .autopush // false)"' 2>/dev/null || true)
[ -n "$auto" ] && echo "git: $auto"

# one line per FILE block of the status facts: target|kind|disk_edited|source_ahead|hidden
printf '%s\n' "$out" | awk '
  /^### FILE / { if (path != "") print path "|" kind "|" de "|" sa "|" hid; path = substr($0, 10); kind = ""; de = ""; sa = ""; hid = "" }
  /^kind: /          { kind = $2 }
  /^disk_edited: /   { de = $2 }
  /^source_ahead: /  { sa = $2 }
  /^contents: hidden/ { hid = "hidden" }
  END { if (path != "") print path "|" kind "|" de "|" sa "|" hid }
' >"$tmp/files"

while IFS='|' read -r shown kind de sa hid; do
  [ -n "$shown" ] || continue
  p=${shown/#\~/$HOME}
  src=$(chezmoi source-path "$p" 2>/dev/null)
  rel=${src#"$srcdir"/}
  if [ ! -e "$p" ]; then class=deleted-on-disk
  elif [ "$de" = yes ] && [ "$sa" = yes ]; then class=both-changed
  elif [ "$de" = yes ]; then class=disk-only
  elif [ "$sa" = yes ]; then class=source-only
  else class=rendering-changes-on-re-init-only; fi

  echo
  echo "#### $shown"
  echo "class: $class"
  echo "source: $rel"
  echo "recent source history:"
  chezmoi git -- log -3 --date=short --format='- %h %ad %s' -- "$rel" 2>/dev/null | sed 's/^$//' | grep . || echo "- (none)"

  if [ "$hid" = hidden ]; then
    echo "contents: hidden (private or encrypted); no merge preview"
  elif [ "$class" = both-changed ] || [ "$class" = disk-only ]; then
    "$here/base.sh" "$p" >"$tmp/base" 2>/dev/null
    if head -n 1 "$tmp/base" | grep -q '^(base not found'; then
      echo "base: not found, so no three-way view"
    else
      chezmoi cat --no-pager --color=false --no-tty -- "$p" >"$tmp/theirs" 2>/dev/null
      cp "$p" "$tmp/ours"
      git merge-file -p -L disk -L base -L source "$tmp/ours" "$tmp/base" "$tmp/theirs" >"$tmp/merged" 2>/dev/null
      n=$?
      if [ "$n" -eq 0 ]; then
        echo "three-way merge of disk and source over the last-written base: clean (disk edits and source changes touch different hunks)"
      else
        echo "three-way merge of disk and source over the last-written base: $n conflicting hunk(s)"
      fi
      [ "$kind" = template ] && echo "(template: this merges the rendered text; port the disk edits into the template itself)"
      echo "merged result (conflicts, if any, marked with <<<<<<< disk / ======= / >>>>>>> source):"
      sed 's/^/    /' "$tmp/merged"
    fi
  fi
done <"$tmp/files"
exit 0
