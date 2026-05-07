# dioxus

A self-contained Claude Code plugin that turns a fresh session into a
Dioxus 0.7 subject-matter expert.

## What's bundled

- `.mcp.json`       — auto-registers a [Serena](https://github.com/oraios/serena) MCP server scoped to `vendor/dioxus`, providing rust-analyzer-backed symbol intelligence (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, …).
- `skills/dioxus-docs/` — skill exposing query scripts (ripgrep over vendored content + prebuilt doc/example index). Complements Serena by handling docs, examples, and free-text.
- `agents/dioxus-expert.md` — subagent that uses Serena + the dioxus-docs skill to answer Q&A, write Dioxus code, and review Dioxus code with `vendor/<path>:<line>` citations.

The vendored upstream content (`vendor/dioxus/` and `vendor/docsite/`) and the
prebuilt indexes (`index/`) are **not** committed here — they are populated by
`bootstrap.sh` automatically on first use.

## Dependencies

- `uvx` (from [uv](https://github.com/astral-sh/uv)) — used by `.mcp.json` to launch Serena.
- `rust-analyzer` — Serena's Rust language server. `bootstrap.sh` installs it via brew or rustup if missing.
- ~1GB free disk inside `vendor/dioxus/target/` for rust-analyzer's analysis cache.

## Install

```text
/plugin marketplace add https://github.com/gaetschwartz/claude-plugins
/plugin install dioxus@gaetans-claude-plugins
/reload-plugins
```

That's it — no manual bootstrap step. The first time the `dioxus-expert`
agent or the `dioxus-docs` skill scripts run, they auto-invoke
`bootstrap.sh`, which clones the repos and builds the index.

> **Note**: on a brand-new install, the Serena MCP server can't start at
> session boot because `vendor/dioxus` doesn't exist yet. The first agent/skill
> call will trigger the bootstrap; after that completes, run `/reload-plugins`
> to bring up Serena. Subsequent sessions are seamless.

## Refresh against upstream

The `bootstrap.sh` script is idempotent — re-running it `git pull`s both
clones and rebuilds the index. The `dioxus-expert` agent will also offer
to run it for you when relevant.

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
