#!/usr/bin/env bash
# Launch wrapper for the knowledge-rag MCP server.
#
# Redirects the corpus's models_cache at a shared XDG cache so the embedder +
# reranker are downloaded once across every knowledge-rag corpus, not per-plugin.
# knowledge-rag resolves models_cache_dir relative to KNOWLEDGE_RAG_DIR with no
# ~ expansion, so the redirection has to be a symlink established here — before
# the server lazy-loads (and thus downloads) the model on first query.
set -euo pipefail

KR="${KNOWLEDGE_RAG_DIR:?KNOWLEDGE_RAG_DIR must be set by .mcp.json}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/knowledge-rag/models"
mkdir -p "$CACHE"

link="$KR/models_cache"
[ -L "$link" ] || rm -rf "$link" 2>/dev/null || true
ln -sfn "$CACHE" "$link"

exec npx -y knowledge-rag
