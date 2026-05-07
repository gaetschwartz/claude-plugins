---
name: dioxus-docs
description: Look up Dioxus 0.7 documentation pages and maintained examples from a bundled clone of DioxusLabs/dioxus and DioxusLabs/docsite via prebuilt slug indexes. Use when asked anything about Dioxus, RSX, dioxus-router, dioxus-fullstack, dioxus-desktop, dioxus-web, dioxus-mobile, signals, use_signal, use_resource, use_memo, dx CLI, manganis, or any dioxus-* crate. For *symbol* intelligence (definitions, references, signatures), use the Serena MCP server that ships in the same plugin instead.
---

# Dioxus Docs Skill

Local, lexical doc + example index for **Dioxus 0.7**. Bundles shallow clones of:

- `vendor/dioxus/` — the Dioxus framework (v0.7 branch), including the maintained `examples/` tree
- `vendor/docsite/` — official docs (`docs-src/0.7/src/`)

…plus a prebuilt index in `index/*.tsv` (files, examples, docs) for fast lookups.

**For Rust symbol/API intelligence (definitions, references, signatures), prefer
the Serena MCP server** (rust-analyzer-backed) that ships in this same plugin.
This skill complements Serena by covering documentation pages, curated example
apps, and free-text search — things Serena doesn't do.

## Symbol lookup → Serena MCP (preferred)

This plugin auto-registers a **Serena** MCP server scoped to
`${CLAUDE_PLUGIN_ROOT}/vendor/dioxus`. Serena wraps `rust-analyzer`, so you get
real symbol resolution — not regex. Tools available (names prefixed by Claude
Code, exact spelling depends on the runtime):

| Question                                | Serena tool                 |
|-----------------------------------------|-----------------------------|
| "Where is `use_signal` defined?"        | `find_symbol`               |
| "Who calls `use_signal`?"               | `find_referencing_symbols`  |
| "What's in `packages/hooks/src/lib.rs`?"| `get_symbols_overview`      |
| "Show me the body of `Signal::write`"   | `find_symbol` + `read_file` |

Fall back to `search.sh --scope=src` only if Serena is unavailable.

## When to use which tool

| Question shape                                  | Approach                            |
|-------------------------------------------------|-------------------------------------|
| Symbol / API definition / references            | Serena (`find_symbol`, `find_referencing_symbols`) |
| "How do server functions work?" (concept)       | `doc.sh server_functions` or `search.sh "server functions" --scope=docs` |
| "Show me a minimal router example"              | `show-example.sh router`            |
| Free-text search across the world               | `search.sh "<phrase>"`              |
| Refresh / first-run setup                       | `bootstrap.sh` (auto-runs on first script invocation) |

All scripts write **info to stderr** and **results to stdout**, so they pipe cleanly.

## First-run / refresh — auto-bootstrap

`doc.sh`, `search.sh`, and `show-example.sh` all auto-invoke `bootstrap.sh` the
first time they run on a fresh install (or after `vendor/` is missing). You
should never have to run anything manually before using the skill.

> **Caveat for Serena MCP**: if `vendor/dioxus` didn't exist when Claude Code
> started the plugin, the Serena MCP server failed to start at session boot.
> After the first script call auto-bootstraps the workspace, run
> `/reload-plugins` to bring Serena up. Subsequent sessions are fine because
> `vendor/dioxus` is already there.

## Scripts

### `doc.sh <slug-or-fragment> [--list]`
Looks up a doc page in `index/docs.tsv` (the Dioxus 0.7 book, ~179 pages).
- Single match → cats the file to stdout, citation header to stderr.
- Multiple matches → lists them; pick a more specific slug.
- `--list` always lists.

```bash
$ doc.sh use_signal --list
essentials/state/use_signal	use_signal	vendor/docsite/docs-src/0.7/src/essentials/state/use_signal.md
```

### `search.sh <query> [--scope=docs|src|examples|all] [--limit=N]`
Smart-case ripgrep across the chosen subtree. Default scope is `all`, default limit is 50, max 5 hits per file (so one chatty file doesn't drown signal).

```bash
$ search.sh "use_resource" --scope=docs
vendor/docsite/docs-src/0.7/src/essentials/lifecycle/index.md:42:...
```

### `show-example.sh <name-or-pattern> [--list]`
Searches `index/examples.tsv` (~145 reference apps under `vendor/dioxus/examples/`,
organized into categories `01-app-demos`, `02-building-ui`, `03-assets-styling`,
`04-managing-state`, `05-using-async`, `06-routing`, `07-fullstack`, `08-apis`,
`09-reference`, `10-integrations`).

Default prints matching paths only (one per line). `--list` prints full TSV with
name / category / path / summary.

```bash
$ show-example.sh router
vendor/dioxus/examples/06-routing/router.rs
```

### `bootstrap.sh`
Idempotent: clones `vendor/dioxus` (v0.7) and `vendor/docsite` if missing,
otherwise `git pull --ff-only`. Prunes docsite binary asset directories,
ensures `rust-analyzer` is installed (brew or rustup), warms `cargo metadata`,
and rebuilds the index. **Auto-invoked by `doc.sh`/`search.sh`/`show-example.sh`
on first run.** You can also run it directly to refresh against upstream.

### `build-index.sh`
Sub-step of `bootstrap.sh` that rebuilds `index/{files,examples,docs}.tsv`.
Not normally invoked directly.

## Conventions for the agent

When the `dioxus-expert` subagent calls into this skill:

1. **Always cite `vendor/<path>:<line>`** when making API claims. The user can
   open these files locally because the plugin bundles them.
2. **For API/symbol questions**, use Serena's `find_symbol` /
   `find_referencing_symbols` first. Fall back to `search.sh --scope=src` or
   `Grep` only if Serena is unavailable. Do not guess from memory.
3. **For "how do I…?" questions**, start in `--scope=docs` (the official 0.7
   book) before falling back to source.
4. **For "show me a working example"**, use `show-example.sh` and read the
   matched `.rs` file directly with `Read`. The examples in `vendor/dioxus/examples/`
   are the maintained, idiomatic patterns — if the example exists, prefer it
   over reinventing.
5. **Never invent an API.** If Serena's `find_symbol` returns no match (or the
   ripgrep fallback finds zero hits), the symbol doesn't exist in 0.7. Tell
   the user; don't fabricate.

## Versioning

Pinned to Dioxus `v0.7` branch and docsite `main`. Refs are recorded in
`vendor/.ref` after every clone or update.
