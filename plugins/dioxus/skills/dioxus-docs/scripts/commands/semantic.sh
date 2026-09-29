#!/usr/bin/env bash
# Usage: semantic.sh <query> [--scope=docs|src|examples|all] [--limit=N]
#
# Output: <path>:<start>-<end> then the snippet, paths relative to the data dir.
# Backed by semble via uvx; semble caches and refreshes its own indexes.

# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

SNIPPET_LINES=12
SYNOPSIS="$PROG semantic <query> [--scope=docs|src|examples|all] [--limit=N]"

arg_error() { log "ERROR: $* (usage: $SYNOPSIS)"; exit 2; }

scope=docs
limit=8
query=""
while (( $# )); do
    case "$1" in
        --scope=*) scope="${1#--scope=}"; shift ;;
        --limit=*) limit="${1#--limit=}"; shift ;;
        --scope|--limit)
            (( $# >= 2 )) || arg_error "$1 needs a value"
            if [[ "$1" == --scope ]]; then scope=$2; else limit=$2; fi
            shift 2 ;;
        --)        shift; query+="${query:+ }$*"; break ;;
        -*)        arg_error "unknown flag: $1" ;;
        *)         query+="${query:+ }$1"; shift ;;
    esac
done

[[ -n "$query" ]] || arg_error "missing query"
[[ "$limit" =~ ^[1-9][0-9]*$ ]] || arg_error "--limit must be a positive integer, got: $limit"

docs_paths=("$DOCS_ROOT" "$DOCSITE/packages/docs-router/src/doc_examples")
case "$scope" in
    docs)     candidates=("${docs_paths[@]}") ;;
    src)      candidates=("$DIOXUS/packages") ;;
    examples) candidates=("$DIOXUS/examples") ;;
    all)      candidates=("$DIOXUS/packages" "$DIOXUS/examples" "${docs_paths[@]}") ;;
    *) arg_error "unknown --scope=$scope (docs|src|examples|all)" ;;
esac

command -v uvx >/dev/null 2>&1 \
    || die "uv is required for semantic search (uvx not found on PATH); install it from https://docs.astral.sh/uv/getting-started/installation/"
command -v python3 >/dev/null 2>&1 || die "python3 is required to format semantic search results"

ensure_bootstrapped

paths=()
for p in "${candidates[@]}"; do
    [[ -d "$p" ]] && paths+=("$p")
done
(( ${#paths[@]} )) || die "no directories to search for --scope=$scope under $DATA (run: $PROG update)"

log "[semantic] first use downloads a small model and builds an index per scope; later runs are fast"
uvx --from 'semble[mcp]' semble search -k "$limit" --content all \
    --max-snippet-lines "$SNIPPET_LINES" --format json -- "$query" "${paths[@]}" \
    | python3 "$_LIB_DIR/lib/render_semantic.py" --data "$DATA" --query "$query" "${paths[@]}"
