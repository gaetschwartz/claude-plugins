# guardrails

A PreToolUse hook for the Bash tool whose rules are data. Each command is tokenised with a real shell lexer
(wrappers like `sudo`/`xargs`/`timeout` are looked through, `bash -c` strings and `$(…)` are descended into, heredoc
bodies and redirect targets are ignored), then checked against the rules in state.

**Nothing is active after install.** Run the `guardrails:setup` skill to pick presets.

## Rules

A rule matches a command (`program`, `args`, `builtin`, raw `regex`) and says what happens:

- `action`: `deny` (the agent gets the message and the call is blocked) or `warn` (the agent gets the message as
  context, once per session).
- `retry: same-command`: the identical command, re-issued in the same session, is allowed. That is the escape hatch
  for the cases a rule cannot anticipate.
- `modes`: session modes that suspend the rule, e.g. `reverse-engineering` for `strings`.
- `requires`: only active when one of these binaries is installed.
- `messageShort`: shown instead of `message` once the full message has been seen in the session.

## Modes

Modes are declared with `agentMayEnable` (may an agent switch it on for a session when the user says so?) and can be
on per session, for every session in a project, or globally. Agents can switch *session* modes on by themselves, but
only for modes that allow it, and only by quoting the user's own words; you get a notice when such a mode first
suspends a rule. Activating a mode for a whole project or globally is a configuration change and needs the user's
explicit request (`--as-user`).

## State

| scope | file |
|---|---|
| global | `~/.claude/plugins/data/guardrails-gaetans-claude-plugins/state.json` (`${CLAUDE_PLUGIN_DATA}`) |
| project | `<project>/.claude/plugins/data/guardrails-gaetans-claude-plugins/state.json` |

Project entries can add rules, and for a global rule id can only: tighten `action`/`retry`, re-enable it, remove
suspending modes, and reword `message`/`messageShort`/`description`. What a global rule matches (`match`,
`requires`) cannot be changed by a project entry; an override that does not validate falls back to the global rule
unchanged. A project can also switch a declared mode on for itself (`active`), which suspends the rules that list
that mode. Session state (retry acknowledgements, modes, warnings shown) lives in the global file and is pruned
after 7 days.

## Presets

| preset | rules |
|---|---|
| `docs-first` | `no-strings` (deny, retry), `binary-spelunking` (otool/nm/objdump, warn); mode `reverse-engineering` |
| `process-safety` | `no-pkill` (pkill/killall, deny, retry), `kill-9` (warn); mode `incident` |
| `modern-cli` | `find-fd`, `grep-rg` (deny, retry, fd/rg cheat sheet, only when installed) |

## CLI

`hooks/guard.py` without arguments is the hook; with arguments it is the CLI (`python3 hooks/guard.py --help`):
`status`, `rule add|set|rm`, `mode declare|undeclare|on|off`, `preset list|show|install`, `enable|disable`. When run
by an agent (`CLAUDECODE` set), configuration changes need `--as-user`, and `enable`/`disable` are refused.

## Migrating from shell-guard

shell-guard is gone; its Bash rules live here as data instead of hard-coded Python. Uninstall shell-guard, install
guardrails, then run the `guardrails:setup` skill — nothing is active until you do, exactly as after a fresh install.
There is no `FIND_OK=1 find …` or `GREP_OK=1 grep -r …` escape hatch any more: for a rule with `retry: same-command`
(the `find-fd` / `grep-rg` rules included), re-run the exact command unchanged instead.

## Skills

- `guardrails:setup`: interview, then install presets.
- `guardrails:rules`: change rules and modes when you ask for it.
- `guardrails:mode`: switch a session mode on or off when you say the session is that kind of work.

`just test` runs the suite, `just check` lints and type-checks, `just validate` runs `claude plugin validate`.
