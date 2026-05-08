#!/usr/bin/env bash
# RAG sub-dispatcher for the dioxus-docs skill.
#
# Verbs:
#   enable <book>    USER-ONLY. Set up venv, pull model, build index.
#   disable <book>   USER-ONLY. Drop the index for a book.
#   rebuild <book>   USER-ONLY. Re-index a book in place.
#   status           Print enabled books and metadata.
#   query <q>        Top-k semantic search across enabled books.
#
# enable/disable/rebuild perform heavyweight side effects (model download,
# Python venv install). They MUST only be invoked by the user; the agent's
# system prompt forbids calling them. The warning printed below is a soft gate.

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
ensure_bootstrapped

RAG_VENV="$PLUGIN_ROOT/.rag-venv"
SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]}")"

usage() {
    cat >&2 <<'EOF'
Usage: /dioxus-docs rag <verb> [args]

Verbs:
  enable <book>  [--model=qwen3-embedding:0.6b|nomic-embed-text|<other>]   USER-ONLY
                          Set up the RAG venv, pull the embedding model, and
                          build a semantic index for <book>.
                          Books: docs, src, examples
  disable <book>          USER-ONLY. Drop the index for <book>.
  rebuild <book>          USER-ONLY. Re-index <book> with the recorded model.
  status                  List enabled books, embedding model, indexed_at.
  query <text> [--book=docs|src|examples|all] [--top-k=N]
                          Semantic top-k search. Default --book=all, --top-k=8.

Default model: qwen3-embedding:0.6b (Ollama tag). With sentence-transformers
fallback when Ollama is unreachable.
EOF
}

book_path() {
    case "$1" in
        docs)     printf '%s\n' "$DOCS_ROOT" ;;
        src)      printf '%s\n' "$DIOXUS/packages" ;;
        examples) printf '%s\n' "$DIOXUS/examples" ;;
        *) die "unknown book: $1 (valid: docs, src, examples)" ;;
    esac
}

warn_user_only() {
    log
    log "[rag] '$1' is a USER-ONLY operation."
    log "[rag] If an agent triggered this, abort and ask the user."
    log "[rag] (downloads embedding model, installs Python deps, indexes content)"
    log
}

run_in_venv() {
    [[ -x "$RAG_VENV/bin/python" ]] || die "RAG venv not set up — run 'rag enable <book>' first"
    "$RAG_VENV/bin/python" "$@"
}

# Extract --model=<v> from argv, leaving the rest in OUT_ARGS. Default below.
MODEL="qwen3-embedding:0.6b"
parse_model_flag() {
    OUT_ARGS=()
    for a in "$@"; do
        case "$a" in
            --model=*) MODEL="${a#--model=}" ;;
            *) OUT_ARGS+=("$a") ;;
        esac
    done
}

if (( $# == 0 )); then usage; exit 0; fi

verb=$1; shift

case "$verb" in
    enable)
        warn_user_only enable
        parse_model_flag "$@"
        set -- "${OUT_ARGS[@]}"
        book="${1:?usage: rag enable <book> [--model=NAME]}"
        path="$(book_path "$book")"
        bash "$SCRIPT_DIR/setup-rag-venv.sh" || die "venv setup failed"
        log "[rag] indexing book='$book' from '$path' with model='$MODEL'"
        run_in_venv "$SCRIPT_DIR/rag_index.py" \
            --action index \
            --book "$book" \
            --source-dir "$path" \
            --plugin-root "$PLUGIN_ROOT" \
            --model "$MODEL"
        ;;
    disable)
        warn_user_only disable
        book="${1:?usage: rag disable <book>}"
        run_in_venv "$SCRIPT_DIR/rag_index.py" \
            --action disable \
            --book "$book" \
            --plugin-root "$PLUGIN_ROOT"
        ;;
    rebuild)
        warn_user_only rebuild
        book="${1:?usage: rag rebuild <book>}"
        path="$(book_path "$book")"
        # Reuse the model recorded in state, falling back to the default.
        recorded=$(run_in_venv -c "
import json, sys
from pathlib import Path
p = Path('$PLUGIN_ROOT/.rag-state.json')
if p.exists():
    s = json.loads(p.read_text())
    info = s.get('books', {}).get('$book', {})
    print(info.get('model', '$MODEL'))
else:
    print('$MODEL')
" 2>/dev/null) || recorded="$MODEL"
        log "[rag] rebuilding '$book' from '$path' with model='$recorded'"
        run_in_venv "$SCRIPT_DIR/rag_index.py" \
            --action rebuild \
            --book "$book" \
            --source-dir "$path" \
            --plugin-root "$PLUGIN_ROOT" \
            --model "$recorded"
        ;;
    status)
        if [[ ! -x "$RAG_VENV/bin/python" ]]; then
            log "[rag] disabled (no venv yet — run 'rag enable <book>')"
            exit 0
        fi
        run_in_venv "$SCRIPT_DIR/rag_index.py" \
            --action status \
            --plugin-root "$PLUGIN_ROOT"
        ;;
    query)
        text="${1:?usage: rag query <text> [--book=...] [--top-k=N]}"; shift
        run_in_venv "$SCRIPT_DIR/rag_query.py" \
            --plugin-root "$PLUGIN_ROOT" \
            --query "$text" \
            "$@"
        ;;
    -h|--help|help) usage; exit 0 ;;
    *) log "unknown rag verb: $verb"; usage; exit 1 ;;
esac
