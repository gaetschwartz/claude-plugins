---
name: rules
description: Use when the user explicitly asks to add, write, change, disable, remove or list guardrails rules or modes, e.g. "block X for agents", "write a rule that warns on Y", "make the strings rule a warning", "turn off the pkill rule in this repo", "what guardrails are active?". Never use it to get past a guardrails denial. A denial means follow its message, re-run the exact command if it says so, or ask the user.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/../../hooks/guard.py *)
---

# guardrails rules

Rules are data, checked against every Bash command by a PreToolUse hook. Change them only through
`python3 "${CLAUDE_SKILL_DIR}/../../hooks/guard.py" <verb>` (written `guard.py` below), never by editing state files.

!`python3 "${CLAUDE_SKILL_DIR}/../../hooks/guard.py" status 2>&1 || echo "(could not read guardrails state)"`

## Ground rules

- Only make changes the user asked for in this conversation, never because a rule just blocked you.
- Pass `--as-user` on every change and put the user's own words in `--reason`.
- Global by default; `--project` when they mean this repo. A project entry can only tighten or reword a global rule.
- `enable` / `disable` are refused for agents: the user runs them from their terminal.

## Authoring a rule

1. Pin down what to catch and what the agent should do instead.
2. Use the narrowest matcher: `program`, narrowed by `args` if needed; `regex` only when nothing else fits.
3. Write a `message` that names the alternative.
4. Default to `deny` with `retry: same-command` (the retry covers cases the rule cannot foresee); use `warn` for
   advice, `modes` for work where the rule should step aside.
5. Dry-run it with at least three commands it must catch (one wrapped: `sudo …`, `bash -c '…'`, `x | …`) and three it
   must not (`man X`, `echo X`, a heredoc mentioning X). Adjust until every line is right:
   `guard.py rule test --json '<rule>' 'cmd1' 'cmd2' …`
6. `guard.py rule add <id> --json '<rule>' --as-user --reason "…"`, then show `status`.

## Fields

| field | meaning |
|---|---|
| `match.program` | command name(s); wrappers (`sudo`, `xargs`, `timeout`, `bash -c`, `$(…)`) are looked through |
| `match.args` | regex over that command's arguments, joined by spaces |
| `match.builtin` | `grep-recursive` |
| `match.regex` | regex over the raw command text |
| `action` | `deny` (default) or `warn` (context note, once per session) |
| `retry` | `none` (default) or `same-command` |
| `modes` | modes that suspend the rule |
| `message` | required; `{which:a\|b}` becomes the first installed binary |
| `messageShort` | shown instead of `message` after its first showing in a session |
| `requires` | rule only active if one of these binaries is installed |
| `enabled` | `true` / `false` |

## Other verbs

`rule set <id> key=value…` (keys: the fields above; `program`/`args`/`builtin`/`regex` set `match`; comma lists;
empty clears), `rule rm <id>`, `rule test --id <id> 'cmd'…`, `mode declare <name> [--agent-may-enable]`,
`mode undeclare <name>`, `mode on|off <name> --scope project|global`, `preset list`, `status`.

Exit codes: 0 ok, 2 invalid input (fix what it says), 3 refused (relay to the user).
