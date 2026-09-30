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
- `modes`: session modes that suspend the rule, e.g. `reverse-engineering` for `strings`. Optional; a rule without
  modes is never suspended.
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
| managed | `/Library/Application Support/ClaudeCode/guardrails.json` (macOS), `/etc/claude-code/guardrails.json` (Linux); POSIX only |

`GUARDRAILS_MANAGED_PATH` adds a second managed file, for tests and odd setups. It never replaces the platform path: both are loaded, the platform file ranks higher, and the override can only add or tighten (it cannot switch on a mode the platform file declares or add suspending modes to its rules). `status` says when the platform file is absent and only an override is in use.

Layers stack managed > global > project. Project entries can add rules, and for a global rule id can only: tighten `action`/`retry`, re-enable it, remove
suspending modes, and reword `message`/`messageShort`/`description`. What a global rule matches (`match`,
`requires`) cannot be changed by a project entry; an override that does not validate falls back to the global rule
unchanged. A project can also switch a declared mode on for itself (`active`), which suspends the rules that list
that mode. Session state (retry acknowledgements, modes, warnings shown) lives in the global file and is pruned
after 7 days.

## Managed scope

The managed file has the same schema as the other state files and sits above both: it is for rules an organisation or
machine owner wants enforced no matter what a user or project configures. A missing file is an empty layer.

Lower layers are merged over managed entries with the same tightening-only rule that governs project over global.
They can add rules and modes of their own, but cannot change a managed rule's `match`/`requires`, loosen its
`action`/`retry`, disable it, add suspending `modes`, make a mode agent-enablable when managed says it is not, or
switch off a managed mode that is `active`. An override that does not validate falls back to the managed entry. A
managed rule without `modes` can never be suspended.

Writing the file is governed by OS permissions only, so an agent cannot change it without sudo. Note that `sudo` resets
the environment and drops `CLAUDECODE`, so an agent with cached or passwordless sudo can write the managed file and
the `--as-user` gating no longer applies to it. Manage it with:

```
sudo guardrails rule add <id> --scope managed --json @rule.json --reason "…"
```

`rule add|set|rm`, `mode declare|undeclare|on|off` and `preset install` accept `--scope global|project|managed`; the
default is unchanged and managed is never the default. `--scope managed` writes the override file when
`GUARDRAILS_MANAGED_PATH` is set, else the platform file. When the file (or its directory, if absent) is not writable
the CLI says so and prints the `sudo` command to re-run. Under root the parent directory is created (0755, whatever the
umask) and the file is written mode 0644 atomically with fsync. `status` warns when the managed file or its directory
is not owned by root or is writable by group or others, since that makes the layer decorative.

### `--path <file>`

`--path` names a managed-format file for one invocation, on every verb that takes `--scope` (`rule add|set|rm`,
`mode declare|undeclare|on|off`, `preset install`) and on the read verbs `status`, `rule test` and session `mode on|off`.

- With `--scope managed`, writes go to that file instead of the platform file or `GUARDRAILS_MANAGED_PATH`. On a write
  verb, `--path` without `--scope managed` is an error (exit 2).
- On `status`, `rule test --id` and `mode on|off` it is loaded as one more managed source, exactly like the
  `GUARDRAILS_MANAGED_PATH` override: ranked below the platform file (and the env override), tightening only, with the
  same validation and ownership/permission problems reported.
- The hook reads only the platform file and `GUARDRAILS_MANAGED_PATH`. A write to any other `--path` prints a note that
  the file is enforced only if `GUARDRAILS_MANAGED_PATH` points at it.
- A not-writable target prints the usual error and a `sudo …` command that includes `--path`; when the target came from
  `GUARDRAILS_MANAGED_PATH` (which `sudo` drops), the command gets an explicit `--path` for it.

Managed rules keep their own `message`/`messageShort`; lower layers cannot reword them. A modes entry on a managed
rule only counts when the managed file itself declares that mode; other entries are ignored (the rule stays always on)
and `status` names them. For a mode declared in the managed file, `active` in the project state is ignored; global
`active` and session records still apply.

`set` and `rm` on a managed rule from another scope are refused with a pointer to `--scope managed` unless that scope
holds the user's own entry for the id, which can still be changed or removed (and stays clamped by the tightening
merge). Only the managed origin is labelled in hook deny/warn output; `status` and `rule test` show the origin of
every rule.

An unreadable, invalid or wrong-shaped managed file is not treated as empty and never turns the guard off: the
unreadable part is skipped, a warning is shown once per session (and on stderr, and in `status`), and the global and
project layers keep being enforced. Individual invalid managed rules are skipped with the same warning. The CLI never
overwrites a corrupt state file: fix or remove it by hand. A corrupt managed file does not block changes to the other
scopes. Managed rules also apply when the global hook is disabled.

`agentMayEnable: false` guards against an honest agent, not against a determined one. A forged session record
(`by: "user"`) in the global state file or `env -u CLAUDECODE guardrails mode on …` still suspends a rule. The real
control is managed permissions and the sandbox (below), which stop an agent editing those files or unsetting the
environment.

The guard only sees the Bash tool and is not a security boundary. To make it hard to bypass, pair it with managed
Claude Code settings; guardrails does not manage these, they are shown here for reference:

```json
{
  "enabledPlugins": { "guardrails@gaetans-claude-plugins": true },
  "allowManagedHooksOnly": true,
  "strictKnownMarketplaces": [{ "source": "github", "repo": "gaetschwartz/claude-plugins" }],
  "allowManagedPermissionRulesOnly": true,
  "permissions": { "deny": ["Write(/etc/claude-code/**)"] }
}
```

`allowManagedHooksOnly` exempts hooks from force-enabled plugins, so the guardrails hook keeps running.

## Presets

| preset | rules |
|---|---|
| `docs-first` | `no-strings` (deny, retry), `binary-spelunking` (otool/nm/objdump, warn); mode `reverse-engineering` |
| `process-safety` | `no-pkill` (pkill/killall, deny, retry), `kill-9` (warn); mode `incident` |
| `modern-cli` | `find-fd`, `grep-rg` (deny, retry, fd/rg cheat sheet, only when installed) |

## CLI

An agent runs the CLI as `guardrails <verb>` (the plugin's `bin/` is on the Bash tool's PATH); from your own terminal
use `python3 <plugin dir>/lib/guard.py <verb>` (`--help` for the full
list: `status`, `rule add|set|rm|test`, `mode declare|undeclare|on|off`, `preset list|show|install`,
`enable|disable`; changes take `--scope global|project|managed`, with `--path <file>` to pick a managed-format file). When run by an agent (`CLAUDECODE` set), configuration changes need `--as-user`, and
`enable`/`disable` are refused. `rule test` dry-runs a draft (`--json`) or installed (`--id`) rule against sample
commands without changing anything. It checks the matcher only (`match` or `-`); it prints `note:` lines when the hook
would not act on a match: rule disabled, `requires` binary missing, a listed mode that suspends it (and whether it is
active now), global hook or project rules disabled.

### Rules from a file or stdin

Every `--json` (`rule add`, `rule test`, and `rule set`, which takes a JSON object of fields to change) accepts a
literal, `@<file>` (`~` and spaces work) or `-` for stdin (`rule add` and `rule test` also accept
`{"rule": {…}, "examples": […]}`, so one heredoc carries both), so a regex full of backticks, quotes and `$(` never has to
survive shell quoting. A missing file or invalid JSON exits 2 with a message. `--examples` takes the same three forms,
and only one of `--json` and `--examples` can read stdin.

```
guardrails rule add no-curl-sh --json @rule.json --scope global --as-user --reason "…"
guardrails rule test --json - 'curl x | sh' <<'EOF'
{"match": {"regex": "curl [^|]*\\| *sh"}, "message": "Do not pipe curl into a shell."}
EOF
```

### `--render`: output the skills paste verbatim

Claude Code cannot show a command's output in the chat by itself; an agent has to paste text. So the CLI prints the
final markdown, computed from real results, and the skills paste it unchanged.

`guardrails rule test --render` prints the rule card for a rule and a set of commands: title, `**Intent**`, `**Match**`,
`**Message**`, then `**Block**` / `**Warn**` (the matcher catches it) and `**Allow**` rows, `**Verified**`, `**Note**`
and `**Raw**`. Commands come from positional arguments (source `inferred`, or `--source`) and from `--examples
@file|-`, a JSON list of `{"cmd", "source": "yours|inferred|you chose", "expect": "match|pass"}`; `--intent`,
`--id-name` and `--scope` label a draft. The engine computes each row's `wrapped` tag (the match reached the rule through
sudo, `bash -c`, xargs, timeout, `$(…)` or a pipeline), and `expect` only feeds the mismatch count: a contradicted row
gets a `⚠`. Every command is an inline-code span padded inside the backticks to one width (the longest command, capped
at 40; longer ones go last, unpadded; CJK and emoji count two columns; a command with a backtick gets the longer fence;
newlines show as `⏎`).

`guardrails status --render` prints the state listing the same way: a first line about the managed files (platform file
present or absent, overrides and `--path` files), one row per rule (`id`, action, origin layers, state: `always
enforced`, `suspended by <modes>`, `disabled`, `enabled`), the modes and the problems. `status` (with or without
`--render`) also takes `--scope global|project|managed` (only rules, modes and problems of that layer) and `--problems`; `--render
--rule <id>` prints just that rule's row. `references/presentation.md` is the layout contract.

## Migrating from shell-guard

shell-guard is gone; its Bash rules live here as data instead of hard-coded Python. Uninstall shell-guard, install
guardrails, then run the `guardrails:setup` skill — nothing is active until you do, exactly as after a fresh install.
There is no `FIND_OK=1 find …` or `GREP_OK=1 grep -r …` escape hatch any more: for a rule with `retry: same-command`
(the `find-fd` / `grep-rg` rules included), re-run the exact command unchanged instead.

## Skills

Six skills; arguments are free text, each skill parses its own flags, and every long flag has a short one. Shared
material (matching semantics, presentation, ground rules for config changes) lives in `references/` and is read only
when a skill needs it.

| skill | arguments |
|---|---|
| `guardrails:status` | `[-s/--scope global\|project\|managed] [-p/--problems] [-P/--path <file>]` |
| `guardrails:explain` | `[<id>] [-c/--command '<cmd>'] [-s/--scope …] [-P/--path <file>]` |
| `guardrails:new` | `[-B/--block <cmd>]… [-A/--allow <cmd>]… [-s/--scope …] [-P/--path <file>] [-a/--action deny\|warn] [-R/--retry] [-m/--modes a,b] [-i/--id <id>] [-y/--yes] [description]` |
| `guardrails:edit` | `<id> [enable\|disable\|rm\|key=value …] [-s/--scope …] [-P/--path <file>] [-y/--yes]` |
| `guardrails:mode` | `[on\|off\|declare\|undeclare] [<name>] [-s/--scope …] [-P/--path <file>] [-e/--agent-may-enable]` |
| `guardrails:setup` | `[<preset>…] [-s/--scope …] [-P/--path <file>] [-y/--yes]` |

- `status` is a plain, compact list (rules with origin and state, modes, problems) and says so when the platform
  managed file is absent and an override or `--path` file is in use. It runs forked (`context: fork`) on Haiku and only
  pastes the output of `guardrails status --render` verbatim, so it is cheap and needs no conversation.
- `explain` answers why a command was denied or not caught and what a rule covers, pasting the `rule test --render` card. It runs
  forked on Sonnet because it reasons over the matching semantics (wrappers, `args` versus
  `match.regex`, layering, modes) in `references/matching.md`, which the skill injects with `!` commands so the fork is
  self-contained. A fork has no conversation history, so callers, other agents included, must pass the
  rule id or the exact command. `allowed-tools` pre-approves only `guardrails status`, `guardrails rule test` and `cat` of the references, and
  the skill is told never to change anything; `allowed-tools` does not itself restrict the other tools. Denial messages
  stay the first source.
- `new` interviews, tests the rule on your examples and on edge cases it thinks of, asks only about genuinely
  ambiguous ones, shows the `rule test --render` card for confirmation, then writes the very rule file it tested. It
  passes the rule and the examples on stdin through a quoted heredoc (no temp files, no inline quoting). Heredoc
  invocations are not pre-approved by `allowed-tools` (tested), and `new`/`edit` pre-approve no config-changing verb.
- `edit`, `mode` and `setup` change configuration only when you ask, always with `--as-user` and your own words in
  `--reason`. They never run `sudo`: a not-writable file prints the `sudo …` command for you to run.

`just test` runs the suite, `just check` lints and type-checks, `just validate` runs `claude plugin validate`.

## Layout

| path | role |
|---|---|
| `lib/` | all Python: `guard.py` (entry point: hook without arguments, CLI with them), `engine.py`, `policy.py`, `store.py`, `cli.py`, `shellwords.py` |
| `hooks/` | `hooks.json` and the `guardrails.sh` wrapper Claude Code runs on every Bash call |
| `references/` | text the skills read on demand: matching semantics, presentation conventions, config-change rules |
| `bin/guardrails` | the CLI wrapper on the Bash tool's PATH |
| `presets/`, `skills/`, `tests/` | preset rule sets, the six skills, shared skill references, the unittest suite |
