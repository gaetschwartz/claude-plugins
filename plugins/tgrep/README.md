# tgrep

Teaches Claude Code to search with [tgrep](https://github.com/microsoft/tgrep) — Microsoft's
trigram-indexed, ripgrep-compatible grep — instead of the builtin Grep tool, and keeps a tgrep
server warm for every repository a session runs in. Adapted from upstream `AGENTS.md`, plus
behaviour verified against the installed binary.

## Skills

| Skill | Use it for |
|---|---|
| `tgrep:search` | Any repository-wide search once `tgrep` is installed. Loads `tgrep status` for the git root up front so the agent knows whether it is on a server, an on-disk index, or a full scan before running anything; then the command shape, scoping, freshness and exit-code rules. |
| `tgrep:hook` | Turning the server hook off or on, excluding one repository, stopping a server, and reading its state. |

Heavy detail — `--json` records, keeping `index`/`serve`/search flags aligned, non-git trees,
troubleshooting, a tool-definition sketch — lives in `skills/search/references/reference.md`.

### Things the skill gets right that a bare agent does not

- `--` before the pattern, so `serve`, `index`, `status`… are never parsed as subcommands.
- The search path must be the **indexed root**: tgrep resolves the index at `<PATH>/.tgrep`,
  so `tgrep -- foo src/cli` from the root silently degrades to a full scan. Scope with
  `-g`/`-t` instead.
- `Indexing: complete` is not a freshness signal; `--no-index` when the latest edit must be
  visible; `tgrep index .` does not refresh a running server.
- Which flags bypass the index, and which must agree between `index`, `serve` and search.

## The server hook (on by default)

`hooks/tgrep-serve.py` runs on **`SessionStart`** and **`SessionEnd`**.

At session start, if the session's cwd is inside a git repository, it asks
`tgrep status <root>` whether a server already answers for that root — tgrep only reports
`Server status` for a server it could actually reach, so a stale `serve.json` is not mistaken
for a live one — and starts `tgrep serve <root>` detached only if none does (`serve` itself
also refuses to double-serve, as a backstop). It then adds one line of context: started or
already running, pid and port, a warning if the first index build is still in progress, and
a nudge if `.tgrep/` is not gitignored. It never serves a directory that is not a git
repository, so `$HOME` or a scratch directory is never indexed.

At session end it removes the session from the root's tracking entry and, when no tracked
session remains and the hook started that server, sends it `SIGTERM` — after re-checking that
`tgrep status` still attributes that pid to the root. `/clear` keeps the server warm.
Servers the hook did not start are used but never stopped.

State lives in `~/.local/state/tgrep-serve/state.json` (`$XDG_STATE_HOME` respected,
`TGREP_SERVE_STATE` overrides): `enabled`, `excludedRoots`, `serveArgs` (e.g. `--max-memory`
/ `--max-cpu` for a large monorepo) and the tracked `servers`. The script is also its own
CLI — `--status`, `--enable`, `--disable [reason]`, `--exclude [root]`, `--include [root]`,
`--stop [root]` — which is what the `tgrep:hook` skill drives. `TGREP_SERVE_HOOK=0` in the
environment overrides the file for one machine or session:

```json
// ~/.claude/settings.json
{
  "env": {
    "TGREP_SERVE_HOOK": "0"
  }
}
```

## Requirements

`tgrep` on `PATH` (`brew install tgrep`) and `python3` for the hook. With the hook disabled,
the search skill tells the agent how to start `tgrep serve .` by hand and to keep `.tgrep/`
out of git.

## Install

```
/plugin marketplace add gaetschwartz/claude-plugins
/plugin install tgrep@gaetans-claude-plugins
```
