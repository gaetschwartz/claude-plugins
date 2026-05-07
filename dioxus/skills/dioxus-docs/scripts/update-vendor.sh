#!/usr/bin/env bash
# Bootstrap or refresh the vendored Dioxus + docsite clones, then rebuild the index.
#
# Idempotent: clones from scratch if a vendor dir is missing (first run after
# cloning the marketplace from GitHub), otherwise `git pull --ff-only`.
#
# Usage: update-vendor.sh

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

mkdir -p "$VENDOR"

if [[ ! -d "$DIOXUS/.git" ]]; then
    log "[update] dioxus: missing — cloning v0.7 (shallow)"
    git clone --depth=1 --branch v0.7 https://github.com/DioxusLabs/dioxus.git "$DIOXUS" 2>&1 | tail -5 >&2 || \
        git clone --depth=1 https://github.com/DioxusLabs/dioxus.git "$DIOXUS" 2>&1 | tail -5 >&2
else
    log "[update] dioxus: pulling…"
    git -C "$DIOXUS" pull --ff-only 2>&1 | tail -5 >&2
fi

if [[ ! -d "$DOCSITE/.git" ]]; then
    log "[update] docsite: missing — cloning (shallow)"
    git clone --depth=1 https://github.com/DioxusLabs/docsite.git "$DOCSITE" 2>&1 | tail -5 >&2
else
    log "[update] docsite: pulling…"
    git -C "$DOCSITE" pull --ff-only 2>&1 | tail -5 >&2
fi

log "[update] pruning docsite asset directories (binary, not used for text Q&A)"
find "$DOCSITE/packages" -maxdepth 2 -type d -name assets -exec rm -rf {} + 2>/dev/null || true

# Pre-create the .serena project config if it's missing (e.g. fresh clone).
if [[ ! -f "$DIOXUS/.serena/project.yml" ]]; then
    log "[update] writing default .serena/project.yml for Serena MCP"
    mkdir -p "$DIOXUS/.serena"
    cat > "$DIOXUS/.serena/project.yml" <<'YAML'
project_name: dioxus
language: rust
read_only: true
ignored_paths:
  - target
  - .git
  - .github
  - flake.lock
  - Cargo.lock
  - notes
  - playwright-tests
languages:
  - rust
YAML
fi

log "[update] recording refs"
{
    printf 'DIOXUS_REF=%s@%s\n' \
        "$(git -C "$DIOXUS" rev-parse --abbrev-ref HEAD)" \
        "$(git -C "$DIOXUS" rev-parse --short HEAD)"
    printf 'DOCSITE_REF=%s@%s\n' \
        "$(git -C "$DOCSITE" rev-parse --abbrev-ref HEAD)" \
        "$(git -C "$DOCSITE" rev-parse --short HEAD)"
} > "$VENDOR/.ref"

log "[update] rebuilding index"
bash "$PLUGIN_ROOT/skills/dioxus-docs/scripts/build-index.sh"

#
# Ensure rust-analyzer is available for Serena MCP.
# Try brew first (works cleanly on this host), fall back to rustup.
#
log "[update] checking rust-analyzer"
if ! command -v rust-analyzer >/dev/null 2>&1 \
   || ! rust-analyzer --version >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        log "[update] installing rust-analyzer via brew"
        brew install rust-analyzer 2>&1 | tail -3 >&2 || \
            log "[update] WARN: brew install rust-analyzer failed"
    elif command -v rustup >/dev/null 2>&1; then
        log "[update] installing rust-analyzer via rustup"
        rustup component add rust-analyzer 2>&1 | tail -3 >&2 || \
            log "[update] WARN: rustup component add rust-analyzer failed"
    else
        log "[update] WARN: no brew or rustup found — install rust-analyzer manually for Serena MCP"
    fi
fi

#
# Warm rust-analyzer's understanding of the workspace by running cargo metadata.
# This populates Cargo.lock and target/.rustc_info.json so the first MCP query is fast.
#
if command -v cargo >/dev/null 2>&1; then
    log "[update] warming workspace metadata (cargo metadata, ~30s)"
    (cd "$DIOXUS" && cargo metadata --format-version 1 --offline >/dev/null 2>&1) \
      || (cd "$DIOXUS" && cargo metadata --format-version 1 >/dev/null 2>&1) \
      || log "[update] WARN: cargo metadata failed; serena's first query will be slower"
fi

log "[update] done. To re-trigger MCP setup, restart Claude Code."
