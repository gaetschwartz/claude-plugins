# RAG (optional semantic search)

Off by default. It complements lexical `search` when a question paraphrases the
docs ("tear down an effect" vs the heading "cleanup functions"). Exact-term
lookups do not need it.

## Rules for the agent

- `rag query`, `rag status` and `rag config show` are read-only. Use them freely.
- `rag enable`, `rag enable --force`, `rag disable` and every `rag config set-*`
  change persistent state. Get the user's consent first. Enabling creates a
  Python venv, downloads an embedding model and indexes a book.
- Run `rag config show` before any setup conversation. Its "Agent instructions"
  block has the verbatim question to ask and the commands for each answer.
- Run the commands yourself after the user answers. State each one as you run it.
- Never run `set-*` with values the user did not give you: no invented keys,
  model names or URLs.
- Never put an API key on a command line or in the chat. Ask the user to export
  `OPENAI_API_KEY`, or to run `dioxus-docs rag config set-openai-key` in their
  own terminal (it prompts).
- If `rag query` reports that RAG is not enabled, tell the user and stop. Do not
  enable it on your own.

## Verbs

| Verb | Type | Effect |
|---|---|---|
| `rag enable <book> [--backend=...] [--model=...] [--force]` | side effects | Set up the venv, pull the model, index `<book>` (`docs`, `src`, `examples`). An already indexed book is left alone unless `--force`, which re-indexes with the recorded backend and model. |
| `rag disable <book>` | destructive | Drop the index for `<book>`. |
| `rag status` | read-only | Indexed books with backend, model and time. |
| `rag query <text> [--book=docs\|src\|examples\|all] [--top-k=N]` | read-only | Output is `path:line<TAB>distance<TAB>snippet`. Default `--book=all --top-k=8`. |
| `rag config show` | read-only | Config, backend readiness, indexed books, agent instructions. |
| `rag config set-backend <name>` | writes config | `ollama`, `openai` or `sentence-transformers`. Resets the model to that backend's default. |
| `rag config set-model <name>` | writes config | Free-form model id. |
| `rag config set-openai-base <url>` | writes config | OpenAI-compatible endpoint (Azure, OpenRouter, vLLM, llama.cpp). |
| `rag config set-openai-key` | writes config | Key from stdin, a terminal prompt or `$OPENAI_API_KEY`. A key argument is refused. Stored in `.rag-config-secrets` (mode 0600) in the data dir. The env var takes precedence over the file. |
| `rag config set-trust-remote-code on\|off` | writes config | Let sentence-transformers run code from the model repo. Off by default. |
| `rag config reset` | writes config | Restore defaults. |

Paths in `rag query` output are relative to the data dir; resolve them with
`dioxus-docs paths` before opening.

## Backends

| Backend | Default model | Notes |
|---|---|---|
| `ollama` | `qwen3-embedding:0.6b` | Local. Needs `ollama serve`. If Ollama is unreachable while indexing and sentence-transformers is installed, indexing falls back to it and records what it actually used. Queries never fall back. |
| `openai` | `text-embedding-3-small` | Any OpenAI-compatible endpoint via `set-openai-base`. Needs a key. |
| `sentence-transformers` | `Qwen/Qwen3-Embedding-0.6B` | Local, runs in the plugin venv. Pulls in torch, so the install is large. Installed only when this backend is selected. |

## Indexes

Each book records the `{backend, model}` it was built with, and queries use the
recorded pair. Changing the config therefore does not break existing indexes.
To move a book to a new backend, `rag disable <book>` then `rag enable <book>`.
After `dioxus-docs update`, refresh a book with `rag enable <book> --force`.

An index is built into a temporary collection and swapped in only when it
completes, so a failed run leaves the previous index in place.

`rag query --book=all` across books with different backends or models warns on
stderr and lists results per book, because distances are not comparable.

## Storage

Everything lives in the data dir (`dioxus-docs paths`): `.rag-venv/`,
`.rag-index/`, `.rag-state.json` (config and per-book records) and
`.rag-config-secrets`. State and secrets are written atomically.
