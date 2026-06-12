#!/usr/bin/env bash
# First-run setup for the p4 knowledge-rag corpus.
#
# Idempotent. Run after a fresh checkout (the vector index and model cache are
# gitignored, so they must be rebuilt locally). Safe to re-run to refresh.
#
#   1. Point models_cache at the shared XDG cache (same redirection as
#      mcp-launch.sh, so the embedder is downloaded once across corpora).
#   2. Ensure the python backend is installed into ~/.knowledge-rag/venv.
#   3. Build the vector index from the bundled documents/ (open docs always;
#      local/ proprietary docs only if present on this machine).
set -euo pipefail

KR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/knowledge-rag"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/knowledge-rag/models"
VENV_PY="$HOME/.knowledge-rag/venv/bin/python"

mkdir -p "$CACHE"
link="$KR/models_cache"
[ -L "$link" ] || rm -rf "$link" 2>/dev/null || true
ln -sfn "$CACHE" "$link"

echo "[bootstrap] ensuring knowledge-rag python backend is installed…" >&2
npx -y knowledge-rag --install-only

echo "[bootstrap] building index from $KR/documents …" >&2
KNOWLEDGE_RAG_DIR="$KR" "$VENV_PY" -c \
  'from mcp_server.server import reindex_documents; print(reindex_documents(full_rebuild=True))'
