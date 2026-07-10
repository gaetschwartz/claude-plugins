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
#              Every status path must finish with exit 0 and complete output —
#              the skill embeds this script's stdout, so a mid-run abort would
#              splice a truncated body into the agent's context.
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

# A literal ${...} surviving into KR means the launcher didn't expand
# CLAUDE_PLUGIN_ROOT; fail loud rather than create a corrupt data tree.
case "$KR" in *'${'*)
  echo "[toolkit] ERROR: KNOWLEDGE_RAG_DIR has an unexpanded variable: $KR" >&2
  exit 1 ;;
esac

# knowledge-rag resolves models_cache_dir relative to KNOWLEDGE_RAG_DIR with no
# ~ expansion, so redirecting the embedder cache to a shared XDG location (one
# download across every corpus) has to be a symlink, established before the
# server lazy-loads the model. Non-destructive: a pre-existing real directory is
# left untouched (its models are simply not shared) rather than deleted.
link_cache() {
  local link="$KR/models_cache"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "[toolkit] note: $link is a real directory; leaving it in place (model cache not shared). Remove it to enable the shared cache at $CACHE." >&2
    return 0
  fi
  mkdir -p "$CACHE"
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
  if [ ! -x "$VENV_PY" ]; then
    echo "[toolkit] ERROR: knowledge-rag venv python not found at $VENV_PY after install." >&2
    exit 1
  fi
  echo "[toolkit] building index from $KR/documents …" >&2
  KNOWLEDGE_RAG_DIR="$KR" "$VENV_PY" - >&2 <<'PY' || { echo "[toolkit] ERROR: index build failed — marker not written." >&2; exit 1; }
import sys
from mcp_server.server import reindex_documents
result = reindex_documents(full_rebuild=True)
if result:
    print(result)
sys.exit(0 if result else 1)
PY
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
    if [ -f "$KNOWLEDGE_MD" ]; then
      cat "$KNOWLEDGE_MD"
    else
      printf '> WARNING: knowledge manual missing at %s — reinstall the plugin.\n' "$KNOWLEDGE_MD"
    fi
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

sub="${1:-status}"; [ $# -gt 0 ] && shift
case "$sub" in
  serve)     cmd_serve "$@" ;;
  bootstrap) cmd_bootstrap "$@" ;;
  status)    cmd_status "$@" ;;
  *) echo "usage: toolkit.sh {serve | bootstrap | status [--skill] [bootstrap]}" >&2; exit 2 ;;
esac
