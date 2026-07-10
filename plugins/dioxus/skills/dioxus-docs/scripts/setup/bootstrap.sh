#!/usr/bin/env bash
# Bootstrap or refresh the vendored Dioxus + docsite clones, then rebuild the index.
#
# Idempotent: clones from scratch if a vendor dir is missing (first run after
# the plugin is installed), otherwise `git pull --ff-only`. Also installs
# rust-analyzer if missing (needed by the Serena MCP server) and warms its
# workspace metadata.
#
# This script is normally auto-invoked by the user-facing skill scripts
# (doc.sh, search.sh, show-example.sh) the first time they run, via
# _lib.sh's `ensure_bootstrapped`. You can also run it directly to refresh
# against upstream.

# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/../_lib.sh"

mkdir -p "$VENDOR"

if [[ ! -d "$DIOXUS/.git" ]]; then
    log "[bootstrap] dioxus: missing — cloning v0.7 (shallow)"
    git clone --depth=1 --branch v0.7 https://github.com/DioxusLabs/dioxus.git "$DIOXUS" 2>&1 | tail -5 >&2 || \
        git clone --depth=1 https://github.com/DioxusLabs/dioxus.git "$DIOXUS" 2>&1 | tail -5 >&2
else
    log "[bootstrap] dioxus: pulling…"
    git -C "$DIOXUS" pull --ff-only 2>&1 | tail -5 >&2
fi

if [[ ! -d "$DOCSITE/.git" ]]; then
    log "[bootstrap] docsite: missing — cloning (shallow)"
    git clone --depth=1 https://github.com/DioxusLabs/docsite.git "$DOCSITE" 2>&1 | tail -5 >&2
else
    log "[bootstrap] docsite: pulling…"
    git -C "$DOCSITE" pull --ff-only 2>&1 | tail -5 >&2
fi

log "[bootstrap] pruning docsite asset directories (binary, not used for text Q&A)"
find "$DOCSITE/packages" -maxdepth 2 -type d -name assets -exec rm -rf {} + 2>/dev/null || true

# Pre-create the .serena project config if it's missing (e.g. fresh clone).
if [[ ! -f "$DIOXUS/.serena/project.yml" ]]; then
    log "[bootstrap] writing default .serena/project.yml for Serena MCP"
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

log "[bootstrap] recording refs"
{
    printf 'DIOXUS_REF=%s@%s\n' \
        "$(git -C "$DIOXUS" rev-parse --abbrev-ref HEAD)" \
        "$(git -C "$DIOXUS" rev-parse --short HEAD)"
    printf 'DOCSITE_REF=%s@%s\n' \
        "$(git -C "$DOCSITE" rev-parse --abbrev-ref HEAD)" \
        "$(git -C "$DOCSITE" rev-parse --short HEAD)"
} > "$VENDOR/.ref"

log "[bootstrap] rebuilding index"
bash "$(dirname "${BASH_SOURCE[0]}")/build-index.sh"

#
# Ensure rust-analyzer is available for Serena MCP.
# Try brew first (works cleanly on this host), fall back to rustup.
#
log "[bootstrap] checking rust-analyzer"
if ! command -v rust-analyzer >/dev/null 2>&1 \
   || ! rust-analyzer --version >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        log "[bootstrap] installing rust-analyzer via brew"
        brew install rust-analyzer 2>&1 | tail -3 >&2 || \
            log "[bootstrap] WARN: brew install rust-analyzer failed"
    elif command -v rustup >/dev/null 2>&1; then
        log "[bootstrap] installing rust-analyzer via rustup"
        rustup component add rust-analyzer 2>&1 | tail -3 >&2 || \
            log "[bootstrap] WARN: rustup component add rust-analyzer failed"
    else
        log "[bootstrap] WARN: no brew or rustup found — install rust-analyzer manually for Serena MCP"
    fi
fi

#
# Warm rust-analyzer's understanding of the workspace by running cargo metadata.
# This populates Cargo.lock and target/.rustc_info.json so the first MCP query is fast.
#
if command -v cargo >/dev/null 2>&1; then
    log "[bootstrap] warming workspace metadata (cargo metadata, ~30s)"
    (cd "$DIOXUS" && cargo metadata --format-version 1 --offline >/dev/null 2>&1) \
      || (cd "$DIOXUS" && cargo metadata --format-version 1 >/dev/null 2>&1) \
      || log "[bootstrap] WARN: cargo metadata failed; serena's first query will be slower"
fi

log "[bootstrap] done."
log "[bootstrap] If the Serena MCP server failed to start (because vendor/dioxus didn't exist when the plugin loaded), run /reload-plugins in Claude Code to bring it up."
