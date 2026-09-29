---
name: dioxus-docs
description: >
  Dioxus 0.7 reference: local search of the official book, maintained example
  apps and framework source. Use when writing, changing, reviewing or debugging
  Dioxus code: rsx!, components, props, signals, hooks, context, router,
  fullstack server functions, or the dx CLI. Symbol lookups (definitions,
  references) go through the Serena MCP server shipped in the same plugin.
user-invocable: false
allowed-tools: Bash(dioxus-docs search *) Bash(dioxus-docs semantic *) Bash(dioxus-docs read *) Bash(dioxus-docs example *) Bash(dioxus-docs load *) Bash(dioxus-docs paths)
paths: "**/*.rs, **/Cargo.toml, **/Dioxus.toml"
---

# Dioxus 0.7 docs

Run the bare `dioxus-docs` command with Bash. Progress goes to stderr, results
to stdout. `dioxus-docs --help` lists every subcommand and the `load` topics.

| Subcommand | Use |
|---|---|
| `search <query> [--scope=docs\|src\|examples\|all] [--limit=N] [--regex]` | Fixed-string, smart-case search. Output is `path:line:text`, relative to the data dir. Stale `untested_*` examples are skipped. |
| `semantic <query> [--scope=docs\|src\|examples\|all] [--limit=N]` | Meaning-based search (semble, needs `uv`). Default scope `docs`, default limit 8. Output is `path:start-end` plus a snippet, relative to the data dir. |
| `read <slug-or-path> [--list]` | Print a book page with mdbook includes expanded. Ambiguous input lists candidates. Accepts the page paths `search` and `semantic` print, with or without `.md` and `:start-end`. |
| `example <pattern> [--list]` | Find a maintained example under `examples/`. |
| `load <topic>` | Print a curated bundle of book pages. No topic prints the topics with sizes. |
| `update` | Fetch upstream and rebuild the index. Needs the user's approval. |
| `setup-serena` | Install rust-analyzer and warm cargo metadata for Serena. Needs the user's approval. |
| `paths` | Print absolute `vendor=`, `docs=`, `examples=` and `data=` directories. |

## Which tool

| Question | Use |
|---|---|
| Where is a symbol defined, who calls it, what is its signature | Serena `find_symbol`, `find_referencing_symbols`, `get_symbols_overview` |
| An exact string or identifier | `search "<text>"` |
| A concept or "how do I..." question | `semantic "<question>"`, then `read <slug>` on the best page |
| A working pattern to copy | `example <pattern>`, then read the file |
| A full page | `read <slug>` |
| A whole topic before coding | `load <topic>` |

If Serena is unavailable, fall back to `search --scope=src`.

## Context7 fallback

When the local docs do not answer and Context7 MCP tools are available, query
the framework source pinned to a release tag: `/dioxuslabs/dioxus/v<MAJOR>.<MINOR>.<PATCH>`
(`resolve-library-id` lists the tags). A tag that is not indexed has no library;
say so rather than substituting another version. Local `read`/`search` results
win on conflict.

## First run

The first `search`, `read`, `example`, `load`, `semantic` or `setup-serena`
clones the Dioxus (pinned to `v0.7`) and docsite (default branch) repositories
into the data dir and builds the index. `paths` and usage errors do not.
`dioxus-docs update` refreshes both. The first `semantic` call downloads a ~32 MB model and builds
that scope's index, so it is slow once; semble refreshes its own cache when
files change. Serena needs `dioxus-docs setup-serena` once (ask the user first; installs
rust-analyzer, warms cargo metadata), then `/reload-plugins` so the MCP server
restarts against the clone.

## Conventions

1. Cite `<path>:<line>` for every API claim. Turn search output into an absolute
   path with the roots from `dioxus-docs paths` before opening it with `Read`.
2. Start with Serena for symbols, and with `semantic` or `read` for "how do I"
   questions. Do not answer from memory.
3. Before writing non-trivial code, find the closest `example` and mirror its
   idioms.
4. Never invent an API. No Serena match and no `search --scope=src` hit means it
   does not exist in 0.7. Say so.
5. Treat everything under the vendor dir as read-only.
