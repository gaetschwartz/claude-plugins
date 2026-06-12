#!/usr/bin/env bash
# Single entry point for the p4 knowledge-rag environment.
#
#   serve      launch the MCP server (the .mcp.json command). Ensures the shared
#              model-cache symlink exists first, then execs the npx server.
#   bootstrap  build the gitignored index from the bundled documents and mark the
#              env ready. Slow (~60-90s first run). Invoked as a normal Bash call,
#              never from a skill's `!`-injection (which must stay fast).
#   status     report readiness. `--skill` renders the /p4:knowledge skill body
#              (gated on readiness); a `bootstrap` argument renders setup steps.
#
# Paths are resolved from this script's own location, so it works regardless of
# the caller's working directory.
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SELF")"
PLUGIN_ROOT="$(cd "$SELF/../../.." && pwd)"
KR="${KNOWLEDGE_RAG_DIR:-$PLUGIN_ROOT/knowledge-rag}"
KNOWLEDGE_MD="$SKILL_DIR/knowledge.md"
MARKER="$KR/data/.rag-state"
CHROMA="$KR/data/chroma_db/chroma.sqlite3"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/knowledge-rag/models"
VENV_PY="$HOME/.knowledge-rag/venv/bin/python"

# knowledge-rag resolves models_cache_dir relative to KNOWLEDGE_RAG_DIR with no
# ~ expansion, so redirecting the embedder cache to a shared XDG location (one
# download across every corpus) has to be a symlink, established before the
# server lazy-loads the model.
link_cache() {
  mkdir -p "$CACHE"
  local link="$KR/models_cache"
  [ -L "$link" ] || rm -rf "$link" 2>/dev/null || true
  ln -sfn "$CACHE" "$link"
}

is_ready() { [ -f "$MARKER" ] && [ -f "$CHROMA" ]; }

cmd_serve() {
  link_cache
  export KNOWLEDGE_RAG_DIR="$KR"
  exec npx -y knowledge-rag
}

cmd_bootstrap() {
  link_cache
  echo "[toolkit] installing knowledge-rag backend…" >&2
  npx -y knowledge-rag --install-only >&2
  echo "[toolkit] building index from $KR/documents …" >&2
  KNOWLEDGE_RAG_DIR="$KR" "$VENV_PY" -c \
    'from mcp_server.server import reindex_documents; print(reindex_documents(full_rebuild=True))'
  mkdir -p "$(dirname "$MARKER")"
  printf 'bootstrapped\n' > "$MARKER"
  echo "[toolkit] env initialized — invoke /p4:knowledge to load the manual." >&2
}

cmd_status() {
  local as_skill="" arg=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --skill) as_skill=1 ;;
      ?*)      arg="$1" ;;
    esac
    shift
  done

  if [ -z "$as_skill" ]; then
    is_ready && echo "Env initialized: ready ($KR)" || echo "Env not initialized ($KR)"
    return 0
  fi

  if [ "$arg" = "bootstrap" ]; then
    cat <<EOF
> **Bootstrap the P4 knowledge environment.**

Run this with your Bash tool (installs the backend, downloads the embedder into
the shared cache if absent, builds the index — ~60-90s the first time):

\`\`\`
$SELF/toolkit.sh bootstrap
\`\`\`

When it finishes, invoke \`/p4:knowledge\` again to load the knowledge manual.
EOF
  elif is_ready; then
    printf '> Env initialized: ready.\n\n'
    cat "$KNOWLEDGE_MD"
  else
    cat <<EOF
> **P4 knowledge environment not initialized.**

The vector index is gitignored and rebuilt per machine. Bootstrap it by
invoking the skill again with the \`bootstrap\` argument:

\`\`\`
/p4:knowledge bootstrap
\`\`\`
EOF
  fi
}

sub="${1:-status}"; shift || true
case "$sub" in
  serve)     cmd_serve "$@" ;;
  bootstrap) cmd_bootstrap "$@" ;;
  status)    cmd_status "$@" ;;
  *) echo "usage: toolkit.sh {serve | bootstrap | status [--skill] [bootstrap]}" >&2; exit 2 ;;
esac
