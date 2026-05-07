# gaetans — a Claude Code plugin marketplace

A small, locally-authored marketplace for Claude Code plugins.

## Plugins

| Name      | What it does |
|-----------|--------------|
| `dioxus`  | Dioxus 0.7 SME: bundled `DioxusLabs/dioxus` + `DioxusLabs/docsite` clones, prebuilt doc/example index, [Serena](https://github.com/oraios/serena) MCP for symbol intelligence, and a `dioxus-expert` subagent that cites `vendor/<path>:<line>` for Q&A, codegen, and code review. |

## Install

In a Claude Code session:

```text
/plugin marketplace add https://github.com/<owner>/<repo>
/plugin install <plugin-name>@gaetans
/reload-plugins
```

After install, run the plugin's first-time bootstrap script (it clones the
upstream repos that are intentionally **not** committed to this marketplace):

```bash
bash ~/.claude/plugins/cache/gaetans/<plugin-name>/<latest-version>/skills/<skill-name>/scripts/update-vendor.sh
```

For the `dioxus` plugin specifically:

```bash
bash ~/.claude/plugins/cache/gaetans/dioxus/*/skills/dioxus-docs/scripts/update-vendor.sh
```

This pulls `DioxusLabs/dioxus` (v0.7) and `DioxusLabs/docsite` into the
plugin's `vendor/` dir, prunes binary asset dirs from the docsite, and
rebuilds the index.

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
