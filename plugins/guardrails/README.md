# shell-guard

A PreToolUse hook for the Bash tool. It tokenises the command with a real shell lexer
(wrappers like `sudo`/`xargs`/`timeout` are looked through, `bash -c` strings and `$(…)` are
descended into, heredoc bodies and redirect targets are ignored) and runs small rules against
the simple commands it finds.

| Rule    | Trigger                                   | Action                                                                 |
|---------|-------------------------------------------|------------------------------------------------------------------------|
| `pkill` | `pkill` in command position               | deny — use `pgrep` + `kill <pid>`                                       |
| `find`  | `find` in command position, `fd` installed | deny with an fd/rg cheat sheet (full once per context, then terse)     |
| `grep`  | `grep -r`/`-R`/`--recursive`, `rg` installed | same                                                                 |

Escape hatches: `FIND_OK=1 find …`, `GREP_OK=1 grep -r …` (prefix on that simple command).
Commands that run inside another host (`podman exec … find`, `ssh host find`) are not touched.

## Configure

```text
/shell-guard:configure                 # status + interactive toggle
/shell-guard:configure disable pkill
/shell-guard:configure enable find grep
/shell-guard:configure reset
```

Config lives at `${CLAUDE_PLUGIN_DATA}/config.json` (Claude Code exports `CLAUDE_PLUGIN_DATA`
to hook processes) and is re-read on every Bash call. `SHELL_GUARD_CONFIG=<path>` overrides it.

## Adding a rule

Drop a module in `hooks/rules/` exposing `RULES` (`{id: description}`), `KEYWORDS`
(substrings that must appear in the raw command for the rule to run at all) and
`check(ctx) -> ("deny" | "warn", message) | None`, then add it to `MODULES` in
`hooks/rules/__init__.py`. `ctx.cmds` is the parsed command list (`None` on unbalanced quotes),
`ctx.enabled` the set of active rule ids, `ctx.which(name)` a PATH lookup.

`just test` runs the suite, `just check` lints and type-checks, `just validate` runs
`claude plugin validate`.
