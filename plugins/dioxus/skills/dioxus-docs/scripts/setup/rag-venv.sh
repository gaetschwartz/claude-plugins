#!/usr/bin/env bash
# Idempotent: create the RAG venv and install its dependencies.
#
# Usage: rag-venv.sh [--backend=<name>]
#   Core deps (chromadb, requests) are always installed. sentence-transformers
#   (pulls in torch) is installed only when --backend=sentence-transformers.
# Prefers `uv` when available, falls back to python -m venv + pip.
# To force a clean reinstall: rm -rf "$DATA/.rag-venv"

# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

RAG_VENV="$DATA/.rag-venv"
CORE_MARKER="$RAG_VENV/.deps-installed"
ST_MARKER="$RAG_VENV/.st-installed"

WITH_ST=0
for a in "$@"; do
    case "$a" in
        --backend=sentence-transformers) WITH_ST=1 ;;
        --backend=*) ;;
        *) die "usage: rag-venv.sh [--backend=<name>]" ;;
    esac
done

USE_UV=0
command -v uv >/dev/null 2>&1 && USE_UV=1

pip_install() {
    if (( USE_UV )); then
        uv pip install --quiet --python "$RAG_VENV/bin/python" "$@"
    else
        "$RAG_VENV/bin/pip" install --quiet "$@"
    fi
}

create_venv() {
    if (( USE_UV )); then
        log "[rag-venv] creating venv at $RAG_VENV with uv"
        uv venv --quiet --python ">=3.10,<3.14" "$RAG_VENV"
        return
    fi
    local python_bin="" cand
    for cand in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
            python_bin="$(command -v "$cand")"
            break
        fi
    done
    [[ -n "$python_bin" ]] || die "no python3 found in PATH (need >=3.10)"
    "$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "$python_bin too old (need >=3.10): $("$python_bin" --version)"
    log "[rag-venv] creating venv at $RAG_VENV with $python_bin"
    "$python_bin" -m venv "$RAG_VENV"
    "$RAG_VENV/bin/pip" install --quiet --upgrade pip wheel
}

if [[ ! -f "$CORE_MARKER" ]]; then
    [[ -x "$RAG_VENV/bin/python" ]] || create_venv
    log "[rag-venv] installing core deps (chromadb, requests)"
    pip_install 'chromadb>=0.5,<0.6' 'requests>=2.31'
    touch "$CORE_MARKER"
fi

if (( WITH_ST )) && [[ ! -f "$ST_MARKER" ]]; then
    log "[rag-venv] installing sentence-transformers (pulls in torch; large, ~1 GB)"
    pip_install 'sentence-transformers>=2.7' || die "sentence-transformers install failed"
    touch "$ST_MARKER"
fi

log "[rag-venv] ready at $RAG_VENV"
