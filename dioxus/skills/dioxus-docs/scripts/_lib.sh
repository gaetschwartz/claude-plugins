#!/usr/bin/env bash
# Shared helpers for dioxus-docs skill scripts.
# Sourced by every script in this dir.

set -euo pipefail

# Resolve the plugin root, regardless of cwd.
# Prefer the env var Claude Code injects; fall back to walking up from $0.
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
    PLUGIN_ROOT="$CLAUDE_PLUGIN_ROOT"
else
    _self="${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}"
    _dir="$(cd "$(dirname "$_self")" && pwd)"
    # scripts/ -> skills/dioxus-docs/ -> skills/ -> plugin root
    PLUGIN_ROOT="$(cd "$_dir/../../.." && pwd)"
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

# Auto-run bootstrap.sh if the vendored content or index is missing.
# User-facing scripts (doc.sh, search.sh, show-example.sh) call this on entry.
# bootstrap.sh and build-index.sh do NOT call it (they're the bootstrap itself).
ensure_bootstrapped() {
    if [[ ! -d "$DIOXUS/.git" ]] \
       || [[ ! -d "$DOCSITE/.git" ]] \
       || [[ ! -f "$INDEX/docs.tsv" ]]; then
        log "[init] vendor or index missing — running one-time bootstrap (clones + index, ~30-60s)"
        bash "$PLUGIN_ROOT/skills/dioxus-docs/scripts/bootstrap.sh" >&2 \
            || die "bootstrap failed; run scripts/bootstrap.sh manually to see full output"
    fi
}
