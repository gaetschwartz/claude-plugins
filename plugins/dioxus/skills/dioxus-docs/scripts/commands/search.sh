#!/usr/bin/env bash
# Usage: search.sh <query> [--scope=docs|src|examples|all] [--limit=N] [--regex]
#
# Output: <path>:<line>:<matched line>, with paths relative to the data dir.
# The query is a fixed string unless --regex is given. Smart-case.

# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

PER_FILE_CAP=5
SYNOPSIS="$PROG search <query> [--scope=docs|src|examples|all] [--limit=N] [--regex]"

scope=all
limit=50
regex=0
query=""
while (( $# )); do
    case "$1" in
        --scope=*) scope="${1#--scope=}"; shift ;;
        --limit=*) limit="${1#--limit=}"; shift ;;
        --scope|--limit)
            (( $# >= 2 )) || die "$1 needs a value (usage: $SYNOPSIS)"
            if [[ "$1" == --scope ]]; then scope=$2; else limit=$2; fi
            shift 2 ;;
        --regex)   regex=1; shift ;;
        --)        shift; query+="$*"; break ;;
        -*)        die "unknown flag: $1 (usage: $SYNOPSIS)" ;;
        *)         query+="${query:+ }$1"; shift ;;
    esac
done

[[ -n "$query" ]] || die "usage: $SYNOPSIS"
[[ "$limit" =~ ^[1-9][0-9]*$ ]] || die "--limit must be a positive integer, got: $limit"

docs_paths=(vendor/docsite/docs-src/0.7/src vendor/docsite/packages/docs-router/src/doc_examples)
case "$scope" in
    docs)     paths=("${docs_paths[@]}") ;;
    src)      paths=(vendor/dioxus/packages) ;;
    examples) paths=(vendor/dioxus/examples) ;;
    all)      paths=(vendor/dioxus/packages vendor/dioxus/examples "${docs_paths[@]}") ;;
    *) die "unknown --scope=$scope (docs|src|examples|all)" ;;
esac

ensure_bootstrapped
cd "$DATA" || exit 1

mode=()
(( regex )) || mode=(--fixed-strings)

# Ask rg for one match beyond the cap so truncated files can be detected.
rc=0
hits=$(rg --no-heading --line-number --color=never --smart-case --no-messages \
          --max-count $((PER_FILE_CAP + 1)) ${mode[@]+"${mode[@]}"} -e "$query" -- "${paths[@]}") || rc=$?

if [[ -z "$hits" ]]; then
    (( rc <= 1 )) || exit "$rc"
    log "no matches for: $query"
    exit 0
fi

printf '%s\n' "$hits" | awk -v cap="$PER_FILE_CAP" -v limit="$limit" '
    {
        p = substr($0, 1, index($0, ":") - 1)
        seen[p]++
        if (seen[p] > cap) { capped[p] = 1; next }
        total++
        if (total <= limit) print
        else over = 1
    }
    END {
        n = 0
        for (p in capped) n++
        if (n) printf "note: %d file(s) have more than %d matches; only the first %d per file are shown\n", n, cap, cap > "/dev/stderr"
        if (over) printf "note: output limited to %d lines (--limit); %d matches found\n", limit, total > "/dev/stderr"
    }
'
