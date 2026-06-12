# p4

A P4 / Tofino-SDE subject-matter expert for Claude Code: a [`knowledge-rag`](https://www.npmjs.com/package/knowledge-rag)
MCP server over a bundled, source-grounded P4 corpus, plus a `p4-expert`
subagent that answers, writes, and reviews P4-stack code with citations.

## What's inside

| Component | Path | Purpose |
|---|---|---|
| Subagent | `agents/p4-expert.md` | P4_14/16, TNA/PSA/PNA/V1Model, p4c/bf-p4c, P4Runtime/BF-RT, open-p4studio, bmv2 |
| MCP server | `.mcp.json` → `scripts/mcp-launch.sh` | `knowledge-rag` over the bundled corpus |
| Corpus | `knowledge-rag/documents/` | P4_16 spec, P4 cheat-sheet, TNA app-note, P4 tutorial (committed) |

## Corpus layout

`knowledge-rag/documents/<category>/` — category is derived by substring match
on the path, so files nest freely. Two zones:

- **committed** — `language/`, `architecture/`, `examples/`: openly
  redistributable sources (p4.org / p4lang / Intel `PUBLIC_*`).
- **`local/`** (gitignored) — non-redistributable vendor docs (Intel/Barefoot
  `CAP-UG*`, SDE guides). Kept out of this public repo; re-added per machine via
  the agent's ingest workflow. `local/<category>/` keeps the right category.

## Setup

The model cache (embedder + reranker) and the vector index are gitignored —
regenerated locally. On a fresh checkout:

```sh
./scripts/bootstrap.sh
```

This redirects `knowledge-rag/models_cache` → `${XDG_CACHE_HOME:-~/.cache}/knowledge-rag/models`
(shared across every knowledge-rag corpus), installs the python backend, and
builds the index. The MCP server applies the same cache redirection on every
launch, so a plain server start works too — `bootstrap.sh` is just the explicit
"build the index now" entry point.

## Adding documents

Ask the `p4-expert` to ingest a doc. Non-redistributable docs land in
`documents/local/<category>/` (gitignored); openly redistributable canonical
sources may be committed under `documents/<category>/`.
