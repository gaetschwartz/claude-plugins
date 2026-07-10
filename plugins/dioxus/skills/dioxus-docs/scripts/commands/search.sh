#!/usr/bin/env bash
# Lexical search across the bundled dioxus + docsite content.
#
# Usage: search.sh <query> [--scope=docs|src|examples|all] [--limit=N]
#
# Output: <relpath>:<line>:<matched line>  (path is plugin-relative)
# Defaults: scope=all, limit=50

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"
ensure_bootstrapped

scope=all
limit=50
query=""
while (( "$#" )); do
    case "$1" in
        --scope=*) scope="${1#--scope=}"; shift ;;
        --limit=*) limit="${1#--limit=}"; shift ;;
        --) shift; break ;;
        -*) die "unknown flag: $1" ;;
        *)  query+="${query:+ }$1"; shift ;;
    esac
done

[[ -n "$query" ]] || die "usage: search.sh <query> [--scope=docs|src|examples|all] [--limit=N]"

paths=()
case "$scope" in
    docs)     paths=("vendor/docsite/docs-src/0.7/src") ;;
    src)      paths=("vendor/dioxus/packages") ;;
    examples) paths=("vendor/dioxus/examples") ;;
    all)      paths=("vendor/dioxus/packages" "vendor/dioxus/examples" "vendor/docsite/docs-src/0.7/src") ;;
    *) die "unknown --scope=$scope (docs|src|examples|all)" ;;
esac

cd "$PLUGIN_ROOT"

# -i smart-case, -n line numbers, --no-heading flat output, --color never for scripting.
# Cap with head to keep output scannable.
rg --no-heading --line-number --color=never --smart-case --max-count 5 \
   -- "$query" "${paths[@]}" 2>/dev/null \
  | head -n "$limit"
