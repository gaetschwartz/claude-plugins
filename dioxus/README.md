# dioxus

Claude Code plugin that turns a session into a Dioxus 0.7 expert. Bundles the upstream `DioxusLabs/dioxus` source + docsite, a Serena MCP server (rust-analyzer-backed) for symbol intelligence, and a `dioxus-expert` subagent that answers questions, writes Dioxus code, and reviews Dioxus code.

```text
/plugin marketplace add https://github.com/gaetschwartz/claude-plugins
/plugin install dioxus@gaetans-claude-plugins
/reload-plugins
```
