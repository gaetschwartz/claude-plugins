#!/usr/bin/env bash
# Shared helpers for dioxus-docs scripts. Sourced by every command and setup script.

set -euo pipefail

PROG="dioxus-docs"

_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# CLAUDE_PLUGIN_DATA is not exported to Bash tool calls, so the default path is derived here.
DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/dioxus-gaetans-claude-plugins}"
VENDOR="$DATA/vendor"
INDEX="$DATA/index"
DIOXUS="$VENDOR/dioxus"
DOCSITE="$VENDOR/docsite"
export DOCS_ROOT="$DOCSITE/docs-src/0.7/src"
export STALE_PREFIX="untested_"

DIOXUS_REF="${DIOXUS_REF:-v0.7}"
DIOXUS_REPO_URL="${DIOXUS_REPO_URL:-https://github.com/DioxusLabs/dioxus.git}"
DOCSITE_REPO_URL="${DOCSITE_REPO_URL:-https://github.com/DioxusLabs/docsite.git}"

log() { printf '%s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_file() {
    [[ -f "$1" ]] || die "missing $1 (run: $PROG update)"
}
require_dir() {
    [[ -d "$1" ]] || die "missing dir $1 (run: $PROG update)"
}

needs_bootstrap() {
    [[ ! -d "$DIOXUS/.git" || ! -d "$DOCSITE/.git" || ! -s "$INDEX/docs.tsv" ]]
}

ensure_bootstrapped() {
    needs_bootstrap || return 0
    log "[init] first run: cloning Dioxus + docsite and building the index"
    bash "$_LIB_DIR/setup/bootstrap.sh" first-run >&2 \
        || die "bootstrap failed (see messages above)"
}

print_page() {
    if command -v python3 >/dev/null 2>&1; then
        python3 "$_LIB_DIR/lib/expand_includes.py" --docsite "$DOCSITE" "$1"
    else
        log "WARN: python3 not found; printing without expanding {{#include}} directives"
        cat "$1"
    fi
}
