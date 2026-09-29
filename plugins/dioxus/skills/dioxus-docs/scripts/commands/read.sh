#!/usr/bin/env bash
# Usage: read.sh <slug-or-fragment> [--list]
#   Prints the matched doc page (mdbook includes expanded) to stdout.
#   --list, or more than one match: prints candidates as TSV (slug, title, path).
# Match order: exact slug, exact basename, then substring of slug or title.
# Also accepts the page paths that search, semantic and example print (optional :start-end).

# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

SYNOPSIS="$PROG read <slug-or-fragment> [--list]"

list_only=0
q=""
while (( $# )); do
    case "$1" in
        --list)    list_only=1; shift ;;
        -h|--help) printf 'Usage: %s\n' "$SYNOPSIS"; exit 0 ;;
        --)        shift; q+="${q:+ }$*"; break ;;
        -*)        die "unknown flag: $1 (usage: $SYNOPSIS)" ;;
        *)         q+="${q:+ }$1"; shift ;;
    esac
done

[[ -n "$q" ]] || die "usage: $SYNOPSIS"
if [[ "$q" =~ ^(.*):[0-9]+(-[0-9]+)?$ ]]; then q="${BASH_REMATCH[1]}"; fi
q="${q#"$DOCS_ROOT"/}"
q="${q#"${DOCS_ROOT#"$DATA"/}"/}"
q="${q%.md}"

ensure_bootstrapped

matches=$(awk -F'\t' -v q="$q" '
    BEGIN { q = tolower(q) }
    {
        slug = tolower($1)
        base = slug
        sub(/.*\//, "", base)
        if (slug == q) tier = 1
        else if (base == q) tier = 2
        else if (index(slug, q) || index(tolower($2), q)) tier = 3
        else next
        rows[tier] = rows[tier] $0 "\n"
        if (!best || tier < best) best = tier
    }
    END { if (best) printf "%s", rows[best] }
' "$INDEX/docs.tsv")

if [[ -z "$matches" ]]; then
    log "[read] no matches for: $q"
    exit 1
fi

n=$(printf '%s\n' "$matches" | awk 'END { print NR }')

if (( list_only || n > 1 )); then
    if (( ! list_only )); then
        log "[read] $n candidates; listing instead of printing. Re-run with a more specific slug."
    fi
    printf '%s\n' "$matches"
    exit 0
fi

path=$(printf '%s\n' "$matches" | awk -F'\t' '{ print $3 }')
log "[read] $path"
print_page "$DATA/$path"
