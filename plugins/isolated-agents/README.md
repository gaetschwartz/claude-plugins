# isolated-agents

A mod (function-hooks plugin) that adds one tool, `isolated_agent`, which runs a task in **context isolation**: the worker sees none of the session's CLAUDE.md files, rules, auto-memory, settings listing or codebase, only the brief it is handed.

## Modes

- **`complete`** (default) — one `$.model.complete` call: no tools, no history, no CLAUDE.md. Files are inlined into the prompt.
- **`agent`** — a subagent started in a fresh `mktemp -d` directory with `omitClaudeMd`, its own system prompt, and a tool allowlist. `Read`, `Write`, `Edit` and `NotebookEdit` are confined to that directory by `tool.call` guards that fail closed. The directory is deleted afterwards unless `keep` is true. MCP-server instructions and the skill listing are dropped from the worker's request.

## Input

| Parameter | Meaning |
|-----------|---------|
| `prompt`, `promptFile` | The brief: inline text, a text file, or both (file first). Write long briefs to a file, tweak them, then pass the path. |
| `@/abs`, `@./rel`, `@~/path` tokens | Inside the brief, tokens naming an existing text file are expanded (inlined in `complete`, copied to `inputs/` in `agent`). A token naming no file stays literal. |
| `files` | Extra files to attach. Text only, 1 MiB each, 4 MiB total. |
| `model` | Defaults to the session's model (`inherit` for the worker). |
| `tools`, `skills` | `agent` only. Tool names the worker may call (default `Read`, `Write`, `Edit`; `Agent`, `Task` and `Workflow` are refused) and skills to preload. |
| `system`, `effort`, `maxTokens`, `timeoutMs` | Completion options; `timeoutMs` also bounds an agent run (default 10 min). |

## Limits

- Only the four file tools are path-confined. Any other tool you grant (`Bash`, `WebFetch`, MCP tools) is **not**: `Bash` can read any file the user can. The working directory is a fresh temp dir, which is not a sandbox.
- The worker still sees the environment block: its working directory, the OS and the date.
- A timed-out agent run returns an error but the worker keeps running; there is no kill API.
- The calling model picks the files it hands over, so it can pass any file, including a CLAUDE.md.
- Early-access API: the mods surface may change between Claude Code releases.

## Install

```text
/plugin marketplace add https://github.com/gaetschwartz/claude-plugins
/plugin install isolated-agents@gaetans-claude-plugins
```

Verify locally with `claude plugin validate plugins/isolated-agents` and `claude plugin test plugins/isolated-agents`.
