#!/usr/bin/env bash
# RAG sub-dispatcher for the dioxus-docs skill.
#
# Verbs:
#   enable <book>    Set up venv, pull model, build index. Side effects.
#   disable <book>   Drop the index for a book. Destructive.
#   rebuild <book>   Re-index a book in place. Side effects.
#   status           Print enabled books and metadata. Read-only.
#   query <q>        Top-k semantic search across enabled books. Read-only.
#   config <verb>    Inspect or change the RAG configuration.
#                      show:         read-only, self-describing
#                      set-* / reset: writes persistent config
#
# Policy: the agent drives RAG setup conversations (asks the user, runs the
# commands itself). The agent's system prompt requires explicit user consent
# before any side-effecting / destructive verb. The note printed below is
# informational — it tells whoever's reading the stderr stream that this is
# a mutating op, not a read-only one.

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
ensure_bootstrapped

RAG_VENV="$PLUGIN_ROOT/.rag-venv"
SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]}")"
SYSTEM_PY="$(command -v python3 || true)"

usage() {
    cat >&2 <<'EOF'
Usage: /dioxus-docs rag <verb> [args]

Verbs:
  enable <book> [--backend=...] [--model=...]   side-effects
                          Set up the RAG venv, pull the embedding model, and
                          build a semantic index for <book>.
                          Books: docs, src, examples.
                          --backend and --model default to the current config
                          (see `rag config show`).
  disable <book>          destructive. Drop the index for <book>.
  rebuild <book>          side-effects. Re-index <book> with the recorded backend+model.
  status                  read-only. List enabled books and metadata.
  query <text> [--book=docs|src|examples|all] [--top-k=N]
                          read-only. Semantic top-k search. Default --book=all, --top-k=8.
  config <sub-verb> [args]
                          show                       read-only. Full config + agent guidance.
                          set-backend <name>         writes config
                          set-model <name>           writes config
                          set-openai-base <url>      writes config
                          set-openai-key <KEY>       writes config (stored in .rag-config-secrets, chmod 600)
                          reset                      writes config
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

note_side_effects() {
    log "[rag] '$1' has side effects (modifies persistent state / installs deps / indexes content)."
}

run_in_venv() {
    [[ -x "$RAG_VENV/bin/python" ]] || die "RAG venv not set up — run 'rag enable <book>' first"
    "$RAG_VENV/bin/python" "$@"
}

# Read a config field via the stdlib-only config IO script (no venv needed).
config_get() {
    [[ -n "$SYSTEM_PY" ]] || die "python3 not found in PATH"
    "$SYSTEM_PY" "$SCRIPT_DIR/rag_config_io.py" --plugin-root "$PLUGIN_ROOT" get "$1"
}

# Extract --backend=<v> --model=<v> from argv, leaving the rest in OUT_ARGS.
# Defaults come from current config so the user's `rag config` choices flow into `enable`.
MODEL=""
BACKEND=""
parse_enable_flags() {
    OUT_ARGS=()
    for a in "$@"; do
        case "$a" in
            --model=*)   MODEL="${a#--model=}" ;;
            --backend=*) BACKEND="${a#--backend=}" ;;
            *) OUT_ARGS+=("$a") ;;
        esac
    done
    [[ -n "$MODEL"   ]] || MODEL="$(config_get model)"
    [[ -n "$BACKEND" ]] || BACKEND="$(config_get backend)"
    [[ -n "$MODEL"   ]] || MODEL="qwen3-embedding:0.6b"
    [[ -n "$BACKEND" ]] || BACKEND="ollama"
}

if (( $# == 0 )); then usage; exit 0; fi

verb=$1; shift

case "$verb" in
    enable)
        note_side_effects enable
        parse_enable_flags "$@"
        set -- "${OUT_ARGS[@]}"
        book="${1:?usage: rag enable <book> [--backend=...] [--model=...]}"
        path="$(book_path "$book")"
        bash "$SCRIPT_DIR/setup-rag-venv.sh" || die "venv setup failed"
        log "[rag] indexing book='$book' from '$path' with backend='$BACKEND' model='$MODEL'"
        run_in_venv "$SCRIPT_DIR/rag_index.py" \
            --action index \
            --book "$book" \
            --source-dir "$path" \
            --plugin-root "$PLUGIN_ROOT" \
            --backend "$BACKEND" \
            --model "$MODEL"
        ;;
    disable)
        note_side_effects disable
        book="${1:?usage: rag disable <book>}"
        run_in_venv "$SCRIPT_DIR/rag_index.py" \
            --action disable \
            --book "$book" \
            --plugin-root "$PLUGIN_ROOT"
        ;;
    rebuild)
        note_side_effects rebuild
        book="${1:?usage: rag rebuild <book>}"
        path="$(book_path "$book")"
        # Reuse the backend+model recorded for this book at index time.
        # Fall back to current config defaults if the book isn't in state.
        recorded_backend=$("$SYSTEM_PY" "$SCRIPT_DIR/rag_config_io.py" --plugin-root "$PLUGIN_ROOT" get-book "$book" backend)
        recorded_model=$("$SYSTEM_PY"   "$SCRIPT_DIR/rag_config_io.py" --plugin-root "$PLUGIN_ROOT" get-book "$book" model)
        [[ -n "$recorded_backend" ]] || recorded_backend="$(config_get backend)"
        [[ -n "$recorded_model"   ]] || recorded_model="$(config_get model)"
        log "[rag] rebuilding '$book' from '$path' with backend='$recorded_backend' model='$recorded_model'"
        run_in_venv "$SCRIPT_DIR/rag_index.py" \
            --action rebuild \
            --book "$book" \
            --source-dir "$path" \
            --plugin-root "$PLUGIN_ROOT" \
            --backend "$recorded_backend" \
            --model "$recorded_model"
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
    config)
        [[ -n "$SYSTEM_PY" ]] || die "python3 not found in PATH"
        sub=${1:-show}
        [[ $# -gt 0 ]] && shift
        case "$sub" in
            show|"")
                exec "$SYSTEM_PY" "$SCRIPT_DIR/rag_config_io.py" --plugin-root "$PLUGIN_ROOT" show
                ;;
            set-backend|set-model|set-openai-base|set-openai-key|reset)
                note_side_effects "config $sub"
                exec "$SYSTEM_PY" "$SCRIPT_DIR/rag_config_io.py" --plugin-root "$PLUGIN_ROOT" "$sub" "$@"
                ;;
            -h|--help|help)
                cat >&2 <<'EOF'
Usage: rag config <sub-verb> [args]

  show                          read-only. Print current config, backend readiness,
                                indexed books, and an "Agent instructions" block tailored
                                to the state.
  set-backend <name>            writes config. ollama | openai | sentence-transformers.
                                Resets the model to the backend's default.
  set-model <name>              writes config. Free-form model identifier:
                                  - ollama tag (e.g. qwen3-embedding:0.6b)
                                  - OpenAI model (e.g. text-embedding-3-small)
                                  - HuggingFace id (e.g. Qwen/Qwen3-Embedding-0.6B)
  set-openai-base <url>         writes config. OpenAI-compatible endpoint
                                (api.openai.com / Azure / OpenRouter / vLLM / llama.cpp).
  set-openai-key <KEY>          writes config. Stored in .rag-config-secrets (gitignored, chmod 600).
                                Env var $OPENAI_API_KEY takes precedence if set.
  reset                         writes config. Restore defaults.
EOF
                ;;
            *)
                log "unknown rag config sub-verb: $sub"
                exit 1
                ;;
        esac
        ;;
    -h|--help|help) usage; exit 0 ;;
    *) log "unknown rag verb: $verb"; usage; exit 1 ;;
esac
