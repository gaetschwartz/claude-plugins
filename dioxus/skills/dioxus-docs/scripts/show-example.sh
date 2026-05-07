#!/usr/bin/env bash
# Find a Dioxus example matching a name or pattern.
#
# Usage: show-example.sh <name-or-pattern> [--list]
#   Default behavior: print the path of the best match (or all matches if many).
#   --list: list all matches as TSV (name \t category \t path \t summary).
#
# Pattern matches against name OR category OR summary substring.

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
ensure_bootstrapped

list_only=0
pat=""
while (( "$#" )); do
    case "$1" in
        --list) list_only=1; shift ;;
        *)      pat+="${pat:+ }$1"; shift ;;
    esac
done

[[ -n "$pat" ]] || die "usage: show-example.sh <name-or-pattern> [--list]"
require_file "$INDEX/examples.tsv"

matches=$(awk -F'\t' -v p="$pat" '
    BEGIN { IGNORECASE=1 }
    index(tolower($1), tolower(p)) || index(tolower($2), tolower(p)) || index(tolower($4), tolower(p))
' "$INDEX/examples.tsv")

if [[ -z "$matches" ]]; then
    log "[show-example] no matches for: $pat"
    exit 1
fi

if (( list_only )); then
    printf '%s\n' "$matches"
else
    printf '%s\n' "$matches" | awk -F'\t' '{print $3}'
fi
