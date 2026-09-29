#!/usr/bin/env bash
# shellcheck source=../_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

cat >&2 <<PLAN
[setup-serena] This will:
  1. clone Dioxus and docsite into $VENDOR if they are missing
  2. install rust-analyzer (brew, else rustup) if it is not already usable
  3. run 'cargo metadata' inside $DIOXUS to warm rust-analyzer (writes Cargo.lock there, may use the network)
PLAN

ensure_bootstrapped

if command -v rust-analyzer >/dev/null 2>&1 && rust-analyzer --version >/dev/null 2>&1; then
    log "[setup-serena] rust-analyzer already available"
elif command -v brew >/dev/null 2>&1; then
    log "[setup-serena] installing rust-analyzer via brew"
    brew install rust-analyzer >&2 || die "brew install rust-analyzer failed"
elif command -v rustup >/dev/null 2>&1; then
    log "[setup-serena] installing rust-analyzer via rustup"
    rustup component add rust-analyzer >&2 || die "rustup component add rust-analyzer failed"
else
    die "neither brew nor rustup found; install rust-analyzer manually"
fi

if command -v cargo >/dev/null 2>&1; then
    log "[setup-serena] running cargo metadata"
    (cd "$DIOXUS" && { cargo metadata --format-version 1 --offline || cargo metadata --format-version 1; } >/dev/null) \
        || log "[setup-serena] WARN: cargo metadata failed; Serena's first query will be slower"
else
    log "[setup-serena] WARN: cargo not found; skipping cargo metadata"
fi

log "[setup-serena] done. Run /reload-plugins in Claude Code if the Serena MCP server did not start because $DIOXUS did not exist yet."
