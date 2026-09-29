#!/usr/bin/env bash
# Usage: example.sh <name-or-pattern> [--list]
#   Prints the path of each matching example (a file, or a directory for multi-file crates).
#   --list: prints matches as TSV (name, category, path, summary).
# Pattern is a case-insensitive substring of name, category or summary.

# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

SYNOPSIS="$PROG example <name-or-pattern> [--list]"

list_only=0
pat=""
while (( $# )); do
    case "$1" in
        --list)    list_only=1; shift ;;
        -h|--help) printf 'Usage: %s\n' "$SYNOPSIS"; exit 0 ;;
        --)        shift; pat+="${pat:+ }$*"; break ;;
        -*)        usage_error "unknown flag: $1 (usage: $SYNOPSIS)" ;;
        *)         pat+="${pat:+ }$1"; shift ;;
    esac
done

! is_blank "$pat" || usage_error "usage: $SYNOPSIS"

ensure_bootstrapped

matches=$(P="$pat" awk -F'\t' '
    BEGIN { p = tolower(ENVIRON["P"]) }
    index(tolower($1), p) || index(tolower($2), p) || index(tolower($4), p)
' "$INDEX/examples.tsv")

if [[ -z "$matches" ]]; then
    log "[example] no matches for: $pat"
    exit 1
fi

if (( list_only )); then
    printf '%s\n' "$matches"
else
    printf '%s\n' "$matches" | awk -F'\t' '{ print $3 }'
fi
