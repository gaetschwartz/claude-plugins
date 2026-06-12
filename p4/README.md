# p4

A P4 / Tofino-SDE subject-matter expert for Claude Code: a [`knowledge-rag`](https://www.npmjs.com/package/knowledge-rag)
MCP server over a bundled, source-grounded P4 corpus, a `p4-expert` subagent
that answers/writes/reviews P4-stack code with citations, and a `knowledge`
skill that fronts all corpus tooling.

## What's inside

| Component | Path | Purpose |
|---|---|---|
| Subagent | `agents/p4-expert.md` | Reasoning + discipline; delegates all corpus access to the skill |
| Skill | `skills/knowledge/SKILL.md` | `/p4:knowledge` — verifies the env, renders the operational manual, gates bootstrap |
| Toolkit | `skills/knowledge/scripts/toolkit.sh` | One entry point: `serve` (MCP launch), `status`, `bootstrap` |
| Manual | `skills/knowledge/knowledge.md` | The "how to fetch" guide, printed by the toolkit when the env is ready |
| MCP server | `.mcp.json` → `toolkit.sh serve` | `knowledge-rag` over the bundled corpus |
| Corpus | `knowledge-rag/documents/` | P4_16 spec, cheat-sheet, TNA app-note, P4 tutorial (committed) |

## How it fits together

`p4-expert` invokes `/p4:knowledge` once per session. The skill body is a single
shell-injection — `` !`toolkit.sh status --skill` `` — so its rendered content is
produced fresh each time:

- **Env not initialized** (fresh checkout — index is gitignored) → the skill
  renders a one-liner pointing at `/p4:knowledge bootstrap`, which renders the
  exact `toolkit.sh bootstrap` command for the agent to run via Bash.
- **Env ready** → the skill renders the full `knowledge.md` manual, after which
  the agent uses the `mcp__p4-knowledge-rag__*` tools directly.

The slow index build never runs inside the shell-injection (which must stay
fast) — only `toolkit.sh bootstrap`, invoked as a normal Bash call, does it.

## Corpus layout

`knowledge-rag/documents/<category>/` — category is derived by substring match
on the path, so files nest freely. Two zones:

- **committed** — `language/`, `architecture/`, `examples/`: openly
  redistributable sources (p4.org / p4lang / Intel `PUBLIC_*`).
- **`local/`** (gitignored) — non-redistributable vendor docs (Intel/Barefoot
  `CAP-UG*`, SDE guides). Kept out of this public repo; re-added per machine via
  the agent's ingest workflow. `local/<category>/` keeps the right category.

## Regenerable state (gitignored)

- `knowledge-rag/data/` — vector index, rebuilt by `toolkit.sh bootstrap`.
- `knowledge-rag/models_cache` — symlink to `${XDG_CACHE_HOME:-~/.cache}/knowledge-rag/models`,
  so the embedder + reranker are downloaded once across every knowledge-rag
  corpus. The symlink is (re)established on every `serve` and `bootstrap`.

## Setup

Usually hands-off: the agent drives bootstrap via the skill on first use. To do
it manually on a fresh checkout:

```sh
./skills/knowledge/scripts/toolkit.sh bootstrap
```
