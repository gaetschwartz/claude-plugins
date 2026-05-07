# gaetans-claude-plugins — a Claude Code plugin marketplace

A small, locally-authored marketplace for Claude Code plugins.

## Plugins

| Name      | What it does |
|-----------|--------------|
| `dioxus`  | Dioxus 0.7 SME: bundled `DioxusLabs/dioxus` + `DioxusLabs/docsite` clones, prebuilt doc/example index, [Serena](https://github.com/oraios/serena) MCP for symbol intelligence, and a `dioxus-expert` subagent that cites `vendor/<path>:<line>` for Q&A, codegen, and code review. |

## Install

In a Claude Code session:

```text
/plugin marketplace add https://github.com/<owner>/<repo>
/plugin install <plugin-name>@gaetans-claude-plugins
/reload-plugins
```

That's it. Plugins in this marketplace **self-bootstrap on first use** —
the first time you invoke a skill or agent from a plugin, its scripts
auto-clone the vendored upstream content and build any required indexes.
No manual `bootstrap.sh` invocation is needed.

> **Note**: on a brand-new install, MCP servers (e.g. Serena) that depend on
> the vendored content may fail to start at session boot. After the first
> agent/skill call triggers the bootstrap, run `/reload-plugins` once to bring
> them up. Subsequent sessions are seamless.

## What's not in this repo

- Each plugin's `vendor/` (cloned upstream content — large, derivable).
- Each plugin's `index/` (regenerated from `vendor/`).
- `target/` directories created by rust-analyzer when MCP runs.

See `.gitignore`.

## Authoring conventions

- Plugins live in their own top-level dir (e.g. `dioxus/`).
- Each plugin contains a `.claude-plugin/plugin.json`, optional `.mcp.json`,
  `agents/`, `skills/`, and any vendored data under `vendor/` (gitignored).
- The marketplace manifest at `.claude-plugin/marketplace.json` lists each
  plugin with `"source": "./<plugin-dir>"`.
