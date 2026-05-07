# dioxus

A self-contained Claude Code plugin that turns a fresh session into a
Dioxus 0.7 subject-matter expert.

## What's bundled

- `vendor/dioxus/`  — shallow clone of `DioxusLabs/dioxus` (`v0.7` branch)
- `vendor/docsite/` — shallow clone of `DioxusLabs/docsite` (`docs-src/0.7/src/` book)
- `index/`          — prebuilt TSV indexes (files, examples, docs)
- `.mcp.json`       — auto-registers a [Serena](https://github.com/oraios/serena) MCP server scoped to `vendor/dioxus`, providing rust-analyzer-backed symbol intelligence (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, …).
- `skills/dioxus-docs/` — skill exposing query scripts (ripgrep over vendored content + prebuilt doc/example index). Complements Serena by handling docs, examples, and free-text.
- `agents/dioxus-expert.md` — subagent that uses Serena + the dioxus-docs skill to answer Q&A, write Dioxus code, and review Dioxus code with `vendor/<path>:<line>` citations.

## Dependencies

- `uvx` (from [uv](https://github.com/astral-sh/uv)) — used by `.mcp.json` to launch Serena.
- `rust-analyzer` — Serena's Rust language server. `update-vendor.sh` installs it via brew or rustup if missing.
- ~1GB free disk inside `vendor/dioxus/target/` for rust-analyzer's analysis cache.

## Install

```text
/plugin marketplace add https://github.com/gaetschwartz/claude-plugins
/plugin install dioxus@gaetans-claude-plugins
/reload-plugins
```

## First-time bootstrap (clones vendor/ + builds index)

After install, run the bootstrap script from the installed cache:

```bash
bash "$(ls -d ~/.claude/plugins/cache/gaetans-claude-plugins/dioxus/*/ | tail -1)skills/dioxus-docs/scripts/update-vendor.sh"
```

The `update-vendor.sh` script is idempotent: clones from scratch on first run,
`git pull`s on subsequent runs.

## Refresh against upstream

Re-run the same script — same idempotent behavior.

This pulls both clones, re-prunes binary asset directories under
`docsite/packages/*/assets/`, ensures `rust-analyzer` is installed, and
rebuilds the index.

## Approach

- **Symbol intelligence** → Serena MCP (rust-analyzer). Real Rust semantics,
  not regex.
- **Conceptual lookups + curated examples + free-text** → ripgrep + prebuilt
  TSV indexes for the docsite book and the maintained examples in
  `vendor/dioxus/examples/`.
- **No embeddings.** The Dioxus 0.7 surface is small enough that this hybrid
  covers Q&A, code-writing, and review without runtime vector services. An
  embeddings layer could be added later as additive caching if recall on
  conceptual queries proves weak.
