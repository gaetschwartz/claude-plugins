---
name: dioxus-docs
description: >
  Dioxus 0.7 documentation reference. MUST be used when writing, modifying,
  reviewing, or debugging any Dioxus component, hook, RSX markup, or fullstack
  server function. Covers signals, hooks, effects, stores, context, collections,
  RSX, components, event handlers, hoisting patterns, routing, and server
  functions. For *symbol* intelligence (definitions, references, signatures),
  call the Serena MCP server that ships in the same plugin instead.
user-invocable: true
effort: high
paths: "**/*.rs"
argument-hint: "search <q> | read <slug> | example <pattern> | load <topic> | rag <verb>"
---

!`${CLAUDE_PLUGIN_ROOT}/skills/dioxus-docs/scripts/dispatch.sh $ARGUMENTS`

# Dioxus Docs Skill

Local, lexical doc + example index for **Dioxus 0.7**. Bundles shallow clones of:

- `vendor/dioxus/` — the Dioxus framework (v0.7 branch), including the maintained `examples/` tree
- `vendor/docsite/` — official docs (`docs-src/0.7/src/`)

…plus a prebuilt index for fast lookups.

**For Rust symbol/API intelligence (definitions, references, signatures), prefer
the Serena MCP server** (rust-analyzer-backed) that ships in this same plugin.
This skill complements Serena by covering documentation pages, curated example
apps, and free-text search — things Serena doesn't do.

## Subcommands

All access goes through one entry point — `/dioxus-docs <subcommand> [args]` for
human use, or `bash ${CLAUDE_PLUGIN_ROOT}/skills/dioxus-docs/scripts/dispatch.sh
<subcommand> [args]` from a subagent. Bootstrap is automatic on every call.

| Subcommand                        | What it does |
|-----------------------------------|---|
| `search <query> [--scope=docs\|src\|examples\|all] [--limit=N]` | Smart-case ripgrep across the chosen subtree. Default `all`, limit 50, capped at 5 hits per file. |
| `read <slug-or-fragment> [--list]` | Print a doc page from the Dioxus 0.7 book (~179 pages). Multiple matches → list; `--list` always lists. |
| `example <pattern> [--list]`      | Find a maintained reference example (~145 apps under `vendor/dioxus/examples/`). |
| `load <topic>`                    | Load a curated bundle of doc pages. Topics: `state`, `ui`, `fullstack`, `router`, `all`. |
| `rag <verb> [args]`               | Semantic search over an opt-in vector index. See **RAG (optional)** below. Disabled by default. Use `rag config show` to see what's available + get tailored setup prompts. |

Output convention: progress goes to **stderr**, results to **stdout** — pipes cleanly.

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

If Serena is unavailable, fall back to `search --scope=src`.

## When to use which tool

| Question shape                            | Approach                            |
|-------------------------------------------|-------------------------------------|
| Symbol / API definition / references      | Serena (`find_symbol`, `find_referencing_symbols`) |
| "How do server functions work?" (concept) | `read server_functions` or `search "server functions" --scope=docs` |
| "Show me a minimal router example"        | `example router`                    |
| Free-text search across the world         | `search "<phrase>"`                 |
| Front-load a curated topic before coding  | `load <topic>`                      |
| Lexical search misses (paraphrase, concept) | `rag query "<phrase>"` (if enabled) |

## RAG (optional)

Disabled by default. RAG complements lexical `search` with semantic similarity —
helpful when the user's question paraphrases the docs ("how do I tear down an
effect?" vs the doc's heading "cleanup functions"), but not necessary for
exact-term lookups.

**Setup is a user action.** Enabling RAG creates a Python venv, downloads an
embedding model (~600 MB for `qwen3-embedding:0.6b`), and indexes a book —
~30 sec for `docs`, a few minutes for `src`. The agent must NOT invoke
`rag enable | disable | rebuild`; only `rag query | status`.

| Verb                                       | Who | What |
|--------------------------------------------|-----|------|
| `rag enable <book> [--model=<tag>]`        | user | Set up venv, pull model, build index for `<book>` (`docs`, `src`, `examples`). |
| `rag disable <book>`                       | user | Drop the index for `<book>`. |
| `rag rebuild <book>`                       | user | Re-index `<book>` with the previously-recorded model. |
| `rag status`                               | both | List enabled books, model, indexed_at. |
| `rag query <text> [--book=...] [--top-k=N]` | both | Top-k similarity. Output: `path:line<TAB>distance<TAB>snippet`. |
| `rag config show`                          | both | Print current backend + model + indexed books + dynamic agent guidance. |
| `rag config set-backend <name>`            | user | `ollama` \| `openai` \| `sentence-transformers`. Resets the model to the backend's default. |
| `rag config set-model <name>`              | user | Free-form model id (Ollama tag, OpenAI model name, or HF id). |
| `rag config set-openai-base <url>`         | user | OpenAI-compatible endpoint (Azure, OpenRouter, vLLM, llama.cpp). |
| `rag config set-openai-key <KEY>`          | user | Store key in `.rag-config-secrets` (gitignored, chmod 600). `$OPENAI_API_KEY` takes precedence if set. |
| `rag config reset`                         | user | Restore defaults. |

### Backends

| Backend                | Default model                | Notes |
|------------------------|------------------------------|-------|
| `ollama`               | `qwen3-embedding:0.6b`       | Local. Auto-falls back to sentence-transformers if Ollama isn't reachable. |
| `openai`               | `text-embedding-3-small`     | Any OpenAI-compatible endpoint via `openai_base_url`. Needs API key. |
| `sentence-transformers` | `Qwen/Qwen3-Embedding-0.6B` | Pure local, runs in plugin venv (~1 GB on first model). |

### Configuration

`rag config show` is the agent's entry point: it prints the current
backend/model, probes each backend's readiness (Ollama running? OpenAI key set?
ST venv ready?), lists indexed books, and emits an **Agent instructions** block
with verbatim prompts to show the user and the exact commands to run for each
possible response. The agent should call this *before* asking the user about
RAG setup — it removes guesswork from the conversation.

Per-book metadata records `{backend, model}` at index time. Queries against an
older book use its recorded backend, not the current default. This is why
changing `set-backend` doesn't invalidate existing indexes — but it does mean
that to migrate a book to a new backend, you must `rag disable <book>` then
`rag enable <book>`.

## First-run / refresh

The dispatcher auto-bootstraps on every invocation — clones `vendor/` if
missing, otherwise `git pull --ff-only`. You should never need to run anything
manually before using the skill.

> **Caveat for Serena MCP**: if `vendor/dioxus` didn't exist when Claude Code
> started the plugin, the Serena MCP server failed to start at session boot.
> After the first dispatcher call populates `vendor/`, run `/reload-plugins`
> to bring Serena up. Subsequent sessions are fine because `vendor/dioxus` is
> already there.

## Conventions for the agent

When the `dioxus-expert` subagent calls into this skill:

1. **Always cite `vendor/<path>:<line>`** when making API claims. The user can
   open these files locally because the plugin bundles them.
2. **For API/symbol questions**, use Serena's `find_symbol` /
   `find_referencing_symbols` first. Fall back to `search --scope=src` or
   `Grep` only if Serena is unavailable. Do not guess from memory.
3. **For "how do I…?" questions**, start with `search --scope=docs` (the
   official 0.7 book) before falling back to source. For a curated bundle of
   pages, `load <topic>`.
4. **For "show me a working example"**, use `example <pattern>` and read the
   matched `.rs` file directly with `Read`. The examples in
   `vendor/dioxus/examples/` are the maintained, idiomatic patterns — if the
   example exists, prefer it over reinventing.
5. **Never invent an API.** If Serena's `find_symbol` returns no match (or
   `search --scope=src` finds zero hits), the symbol doesn't exist in 0.7.
   Tell the user; don't fabricate.

## Versioning

Pinned to Dioxus `v0.7` branch and docsite `main`. Refs are recorded in
`vendor/.ref` after every clone or update.
