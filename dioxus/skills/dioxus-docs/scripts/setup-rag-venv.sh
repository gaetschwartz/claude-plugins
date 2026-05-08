#!/usr/bin/env bash
# Idempotent: create / upgrade the plugin's RAG Python venv.
#
# Installs:
#   - chromadb        — persistent vector store
#   - requests        — Ollama HTTP client
#   - sentence-transformers — fallback embedding backend (when Ollama is unreachable)
#
# Skips installation if $RAG_VENV/.deps-installed already exists.
# To force a clean reinstall: rm -rf .rag-venv

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

RAG_VENV="$PLUGIN_ROOT/.rag-venv"
MARKER="$RAG_VENV/.deps-installed"

# Pick the most ML-wheel-friendly Python on PATH (3.13/3.12 first, latest last).
python_bin=""
for cand in python3.13 python3.12 python3.11 python3.10 python3.14 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        python_bin="$(command -v "$cand")"
        break
    fi
done
[[ -n "$python_bin" ]] || die "no python3 found in PATH (need >=3.10)"

ver_ok=$("$python_bin" -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')
[[ "$ver_ok" == "1" ]] || die "$python_bin too old (need >=3.10): $($python_bin --version)"

if [[ -f "$MARKER" ]]; then
    log "[rag-venv] already set up at $RAG_VENV (delete .deps-installed to force reinstall)"
    exit 0
fi

if [[ ! -d "$RAG_VENV" ]]; then
    log "[rag-venv] creating venv at $RAG_VENV with $python_bin"
    "$python_bin" -m venv "$RAG_VENV"
fi

log "[rag-venv] upgrading pip"
"$RAG_VENV/bin/pip" install --quiet --upgrade pip wheel

log "[rag-venv] installing core deps (chromadb, requests)"
"$RAG_VENV/bin/pip" install --quiet \
    'chromadb>=0.5,<0.6' \
    'requests>=2.31'

# sentence-transformers is the fallback when Ollama isn't reachable. It pulls in
# torch — large (~1 GB), but it's the price of self-contained operation. Eager
# install keeps `rag query` fast on the fallback path (no first-call delay).
log "[rag-venv] installing fallback embedding backend (sentence-transformers; large, ~1 GB)"
if ! "$RAG_VENV/bin/pip" install --quiet 'sentence-transformers>=2.7'; then
    log "[rag-venv] WARN: sentence-transformers install failed."
    log "[rag-venv] You'll be locked into the Ollama backend. Install Ollama from https://ollama.com"
    log "[rag-venv] and run: ollama serve"
fi

touch "$MARKER"
log "[rag-venv] done. venv at $RAG_VENV"
