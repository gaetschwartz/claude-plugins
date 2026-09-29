# dioxus

Claude Code plugin for Dioxus 0.7 work: a `dioxus-docs` command over local clones of the Dioxus source and the official docsite, a Serena MCP server for Rust symbol lookups, and a `dioxus-expert` subagent that answers questions, writes code and reviews code with citations.

```text
/plugin marketplace add https://github.com/gaetschwartz/claude-plugins
/plugin install dioxus@gaetans-claude-plugins
/reload-plugins
```

## The `dioxus-docs` command

The plugin puts `dioxus-docs` on the PATH of Claude's Bash tool. The `dioxus-docs` skill (model-invoked, not a slash command) tells Claude when to use it. Run `dioxus-docs --help` for the full list.

| Subcommand | What it does |
|---|---|
| `search <query> [--scope=docs\|src\|examples\|all] [--limit=N] [--regex]` | Fixed-string, smart-case search. |
| `read <slug-or-fragment> [--list]` | Print a book page with mdbook includes expanded. |
| `example <pattern> [--list]` | Find a maintained example. |
| `load <topic>` | Print a curated bundle of book pages. |
| `update` | Fetch upstream and rebuild the index. |
| `paths` | Print the absolute vendor, docs, examples and data directories. |
| `setup-serena` | Install rust-analyzer and warm cargo metadata for Serena. |
| `rag <verb>` | Opt-in semantic search. |

## First run

The first `search`, `read`, `example` or `load` shallow-clones `DioxusLabs/dioxus` (branch `v0.7`, override with `DIOXUS_REF`) and `DioxusLabs/docsite`, then builds a small index. Expect roughly 200 MB on disk and network time proportional to your connection. Nothing else is installed. `dioxus-docs update` refreshes both clones.

## Data directory

State lives in `$CLAUDE_PLUGIN_DATA`, or `~/.claude/plugins/data/dioxus-gaetans-claude-plugins` when that is not set in the shell (`dioxus-docs paths` prints the resolved path):

```text
vendor/dioxus, vendor/docsite   the clones
index/                          docs and example indexes
.rag-venv/, .rag-index/         RAG venv and vector store (only after opting in)
.rag-state.json                 RAG config and per-book records
.rag-config-secrets             OpenAI key, mode 0600 (only if you store one)
```

The plugin directory itself is never written to, so plugin updates do not lose the clones.

## Serena MCP server

`.mcp.json` registers Serena (`oraios/serena`, pinned to v1.7.0, context `claude-code`) scoped to `vendor/dioxus`. It is launched with `uvx`, so `uv` must be installed. It gives Claude `find_symbol`, `find_referencing_symbols` and `get_symbols_overview` backed by rust-analyzer.

The project file the plugin writes sets `read_only: true`, which removes Serena's own editing tools for that project. It does not restrict Claude's `Edit` and `Write`.

`dioxus-docs setup-serena` is a separate, explicit step because it changes your machine: it installs rust-analyzer with `brew` (or `rustup component add`) if none is usable, and runs `cargo metadata` inside the Dioxus clone, which writes a `Cargo.lock` there and may use the network. Run it once, then `/reload-plugins`. The MCP server starts at session boot, so on a fresh install it fails until the clones exist; `/reload-plugins` after the first docs call (or `setup-serena`) brings it up.

## Semantic search (opt-in)

When the local docs come up empty, the skill and agent fall back to Context7 if it is installed (not bundled): `/llmstxt/dioxuslabs_learn_<MAJOR>_<MINOR>_llms-full_txt` for the guides and `/dioxuslabs/dioxus/v<MAJOR>.<MINOR>.<PATCH>` for release-pinned source.

`dioxus-docs rag` adds embedding-based search over the book, the framework source or the examples. It is off by default; enabling it creates a Python venv in the data dir, downloads an embedding model and indexes a book. Backends: Ollama (default), OpenAI-compatible endpoints and sentence-transformers. Setup is a guided conversation with Claude, see `skills/dioxus-docs/references/rag.md`. An OpenAI key is never accepted on the command line: export `OPENAI_API_KEY` or pipe it to `dioxus-docs rag config set-openai-key`.

## Tests

`just test` runs the offline shell suite (set `DIOXUS_TEST_VENDOR` to a directory holding `dioxus/` and `docsite/` clones to avoid cloning). `just check` runs shellcheck.
