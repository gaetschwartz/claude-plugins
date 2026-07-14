# gaetans-claude-plugins

A small Claude Code plugin marketplace.

| Plugin   | What it does |
|----------|--------------|
| `dioxus` | Dioxus 0.7 SME — bundled source + docsite, Serena MCP for symbols, `dioxus-expert` subagent for Q&A / codegen / review. |
| `p4` | P4 / Tofino SDE SME — bundled source-grounded corpus (P4_16 spec, TNA, BF-Runtime, SDE guides) via a knowledge-rag MCP, `knowledge` skill, and a `p4-expert` subagent that cites every answer. |
| `language-tutor` | Personal language tutor for any language pair — curriculum, lessons, and spaced-repetition vocabulary, with all progress stored in your Notion workspace. |
| `swiss-transport` | Accurate Swiss public-transport lookups — connections, live departure/arrival boards, and station search — via the keyless public search.ch timetable API, with a bundled `sbb.py` helper. |

```text
/plugin marketplace add https://github.com/gaetschwartz/claude-plugins
/plugin install <name>@gaetans-claude-plugins
/reload-plugins
```
