---
name: rules
description: Use when the user explicitly asks to add, change, disable, remove or list guardrails rules or modes, e.g. "block X for agents", "warn when an agent runs Y", "make the strings rule a warning", "turn off the pkill rule in this repo", "what guardrails are active?". Never use it to get past a guardrails denial. A denial means follow its message, re-run the exact command if it says so, or ask the user.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/../../hooks/guard.py *)
---

# Managing guardrails rules

guardrails is a PreToolUse hook that checks every Bash command against rules stored as data. Rules and modes change
only through `guard.py`. Never edit the state files by hand.

## Current state

!`python3 "${CLAUDE_SKILL_DIR}/../../hooks/guard.py" status 2>&1 || echo "(could not read guardrails state)"`

## Ground rules

- Only make changes the user asked for in this conversation. Pass `--as-user` on every change: it states that the user
  explicitly requested it, and it is recorded (`setBy.by: agent`).
- Never add, loosen, disable or remove a rule because it just blocked you.
- Put the user's own words in `--reason`.
- Default to global scope; add `--project` when the user means "in this repo". A project entry for a global rule can
  only tighten it (warn→deny, drop retry, remove suspending modes, re-enable) and reword its messages; it cannot
  change what the rule matches (`match`, `requires` are ignored). A project can also switch a declared mode on for
  itself (`active`), which suspends the rules that list that mode. The CLI prints a note when an assignment has no
  effect.
- `enable` / `disable` of the whole hook are refused for agents: tell the user to run them from their terminal.

## Rule fields

| field | values |
|---|---|
| `match.program` | command name or list of names (wrappers like sudo/xargs/timeout and `bash -c` are looked through) |
| `match.args` | regex over that command's arguments joined by spaces |
| `match.builtin` | built-in predicate: `grep-recursive` |
| `match.regex` | regex over the raw command text |
| `action` | `deny` (default) or `warn` (context note, once per session) |
| `retry` | `none` (default) or `same-command` (re-running the identical command is allowed) |
| `modes` | modes that suspend the rule |
| `message` | required; say what to do instead |
| `messageShort` | optional; shown instead of `message` after the first time in a session |
| `requires` | optional; rule is only active if one of these binaries is installed |
| `enabled` | `true` / `false` |

`message` may use `{which:a|b}`, which becomes the first of those binaries that is installed.

## Commands

```bash
G="${CLAUDE_SKILL_DIR}/../../hooks/guard.py"
python3 "$G" rule add no-telnet --as-user --reason "<user's words>" \
  --json '{"match": {"program": "telnet"}, "message": "Use nc or openssl s_client instead."}'
python3 "$G" rule set no-strings action=warn --as-user --reason "<user's words>"
python3 "$G" rule set no-pkill modes= --project --as-user --reason "<user's words>"
python3 "$G" rule rm no-telnet --as-user --reason "<user's words>"
python3 "$G" mode declare incident --description "Firefighting" [--agent-may-enable] --as-user
python3 "$G" mode undeclare incident --as-user
python3 "$G" mode on reverse-engineering --scope project --as-user   # persistent for this repo
python3 "$G" preset list
python3 "$G" status
```

`rule set` keys: action retry enabled modes message messageShort description program args builtin regex requires
(`program`, `modes`, `requires` take comma lists; an empty value clears the field).

Exit codes: 0 ok, 2 invalid input or unreadable state (fix what the message says), 3 refused (relay it to the user).
Show the user `status` after any change.
