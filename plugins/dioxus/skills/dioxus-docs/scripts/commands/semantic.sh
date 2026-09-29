#!/usr/bin/env bash
# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

SNIPPET_LINES=12
MAX_LIMIT=50
OVERFETCH=4
MAX_FETCH=100
SYNOPSIS="$PROG semantic <query> [--scope=docs|src|examples|all] [--limit=N]"

scope=docs
limit=8
query=""
while (( $# )); do
    case "$1" in
        --scope=*) scope="${1#--scope=}"; shift ;;
        --limit=*) limit="${1#--limit=}"; shift ;;
        --scope|--limit)
            (( $# >= 2 )) || usage_error "$1 needs a value (usage: $SYNOPSIS)"
            if [[ "$1" == --scope ]]; then scope=$2; else limit=$2; fi
            shift 2 ;;
        -h|--help) printf 'Usage: %s\n' "$SYNOPSIS"; exit 0 ;;
        --)        shift; query+="${query:+ }$*"; break ;;
        -*)        usage_error "unknown flag: $1 (usage: $SYNOPSIS)" ;;
        *)         query+="${query:+ }$1"; shift ;;
    esac
done

! is_blank "$query" || usage_error "missing query (usage: $SYNOPSIS)"
[[ "$limit" =~ ^[1-9][0-9]*$ ]] || usage_error "--limit must be a positive integer, got: $limit (usage: $SYNOPSIS)"
(( ${#limit} <= 3 && limit <= MAX_LIMIT )) || usage_error "--limit is capped at $MAX_LIMIT, got: $limit"

docs_paths=("$DOCS_ROOT" "$DOCSITE/packages/docs-router/src/doc_examples")
case "$scope" in
    docs)     candidates=("${docs_paths[@]}") ;;
    src)      candidates=("$DIOXUS/packages") ;;
    examples) candidates=("$DIOXUS/examples") ;;
    all)      candidates=("$DIOXUS/packages" "$DIOXUS/examples" "${docs_paths[@]}") ;;
    *) usage_error "unknown --scope=$scope (docs|src|examples|all) (usage: $SYNOPSIS)" ;;
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

fetch=$(( limit * OVERFETCH ))
(( fetch <= MAX_FETCH )) || fetch=$MAX_FETCH
raw=$(mktemp "${TMPDIR:-/tmp}/dioxus-semantic.XXXXXX")
serr=$(mktemp "${TMPDIR:-/tmp}/dioxus-semantic.XXXXXX")
trap 'rm -f "$raw" "$serr"' EXIT

rc=0
uvx --from semble semble search -k "$fetch" --content all \
    --max-snippet-lines "$SNIPPET_LINES" --format json -- "$query" "${paths[@]}" \
    >"$raw" 2>"$serr" || rc=$?
grep -v '^WARNING: Language ' "$serr" >&2 || true
(( rc == 0 )) || die "semble search failed (exit $rc); check that uv works: uvx --from 'semble[mcp]' semble --help"

python3 "$_LIB_DIR/lib/render_semantic.py" --data="$DATA" --query="$query" --limit="$limit" \
    --stale-prefix="$STALE_PREFIX" "${paths[@]}" <"$raw"
