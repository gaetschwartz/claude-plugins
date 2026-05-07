#!/usr/bin/env bash
# Look up and print a Dioxus 0.7 doc page by slug or title fragment.
#
# Usage: doc.sh <slug-or-fragment> [--list]
#   Default behavior: cat the matched doc page to stdout.
#   --list: list candidate matches as TSV (slug \t title \t relpath).

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
ensure_bootstrapped

list_only=0
q=""
while (( "$#" )); do
    case "$1" in
        --list) list_only=1; shift ;;
        *)      q+="${q:+ }$1"; shift ;;
    esac
done

[[ -n "$q" ]] || die "usage: doc.sh <slug-or-fragment> [--list]"
require_file "$INDEX/docs.tsv"

# Try exact slug match first.
exact=$(awk -F'\t' -v q="$q" '$1==q' "$INDEX/docs.tsv")

if [[ -n "$exact" ]]; then
    matches="$exact"
else
    matches=$(awk -F'\t' -v q="$q" '
        BEGIN { IGNORECASE=1 }
        index(tolower($1), tolower(q)) || index(tolower($2), tolower(q))
    ' "$INDEX/docs.tsv")
fi

if [[ -z "$matches" ]]; then
    log "[doc] no matches for: $q"
    exit 1
fi

n=$(printf '%s\n' "$matches" | wc -l)

if (( list_only )) || (( n > 1 )); then
    if (( ! list_only )) && (( n > 1 )); then
        log "[doc] $n candidates — listing instead of catting. Re-run with a more specific slug."
    fi
    printf '%s\n' "$matches"
    exit 0
fi

# Single match: emit a citation header to stderr, content to stdout.
path=$(printf '%s\n' "$matches" | awk -F'\t' '{print $3}')
log "[doc] $path"
cat "$PLUGIN_ROOT/$path"
