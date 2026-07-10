#!/usr/bin/env bash
# Shared helpers for dioxus-docs skill scripts.
# Sourced (via `source ../_lib.sh` etc.) by every command and setup script.

set -euo pipefail

# Capture this file's directory once, so siblings can reference each other
# regardless of which script sourced us or what cwd was when the chain started.
# (Symlink-safe: `cd` resolves to the physical path.)
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the plugin root. Prefer the env var Claude Code injects at plugin
# load. Fall back to walking up from _lib.sh until we find the `.claude-plugin`
# marker dir — robust against future re-organizations (depth-independent).
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
    PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT"
else
    _walk="$_LIB_DIR"
    while [[ "$_walk" != "/" && ! -d "$_walk/.claude-plugin" ]]; do
        _walk="$(dirname "$_walk")"
    done
    [[ "$_walk" != "/" ]] || {
        printf 'ERROR: could not find plugin root (no .claude-plugin in any ancestor of %s)\n' "$_LIB_DIR" >&2
        exit 1
    }
    PLUGIN_ROOT="$_walk"
fi

VENDOR="$PLUGIN_ROOT/vendor"
INDEX="$PLUGIN_ROOT/index"
DIOXUS="$VENDOR/dioxus"
DOCSITE="$VENDOR/docsite"
DOCS_ROOT="$DOCSITE/docs-src/0.7/src"

log() { printf '%s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_file() {
    [[ -f "$1" ]] || die "missing $1 (the bootstrap may have failed)"
}
require_dir() {
    [[ -d "$1" ]] || die "missing dir $1 (the bootstrap may have failed)"
}

# Auto-run setup/bootstrap.sh if the vendored content or index is missing.
# Called by every command script on entry. The bootstrap and build-index
# scripts themselves do NOT call this — they ARE the bootstrap.
ensure_bootstrapped() {
    if [[ ! -d "$DIOXUS/.git" ]] \
       || [[ ! -d "$DOCSITE/.git" ]] \
       || [[ ! -f "$INDEX/docs.tsv" ]]; then
        log "[init] vendor or index missing — running one-time bootstrap (clones + index, ~30-60s)"
        bash "$_LIB_DIR/setup/bootstrap.sh" >&2 \
            || die "bootstrap failed; run scripts/setup/bootstrap.sh manually to see full output"
    fi
}
