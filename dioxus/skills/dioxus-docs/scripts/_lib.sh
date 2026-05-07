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
    [[ -f "$1" ]] || die "missing $1 (run build-index.sh first)"
}
require_dir() {
    [[ -d "$1" ]] || die "missing dir $1 (run update-vendor.sh first)"
}
