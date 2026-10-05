# guardrails

A PreToolUse hook for the Bash and Monitor tools whose rules are data. Each command is parsed in process by the
`ast-grep-py` library (its tree-sitter Bash grammar does all the lexing and parsing; guardrails has no shell parser of its
own), and every rule is matched on the command as written, on its wrapper variants (`sudo`, `xargs`, `timeout`, ...) and on
the scripts of `bash -c` and `eval`. Heredoc bodies and redirect targets are data. A Monitor call with only a `ws` URL has no
command and is ignored; monitors a plugin declares start without a tool call and are not covered.

**Nothing is active after install.** Run the `guardrails:setup` skill to pick presets.

## Rules

A rule matches a command and says what happens:

- `match`: one [ast-grep](https://ast-grep.github.io/) rule over the syntax tree (`pattern`, `kind`, `regex`, `inside`,
  `has`, `follows`, `precedes`, `not`, `any`, `all`, ...; every `regex` is Rust regex syntax: linear time, no
  backreferences or look-around), plus three atoms usable anywhere in it: `{"command": "pkill"}` (a command by name in
  any spelling, optionally with `args`, a regex over its text), `{"assignment": {"name": "LD_PRELOAD"}}` and
  `{"wrapper": true}`. Its keys are ANDed; `any` says "either". `wrappers: false` keeps the rule off commands reached
  through `sudo`, `env`, `xargs` and the other wrappers. [references/matching.md](references/matching.md) has the
  semantics, the node kinds and the known limits.
- `action`: `deny` (the agent gets the message and the call is blocked) or `warn` (the message arrives as context, once per
  session). `retry: same-command` lets the identical command through when re-issued in the same session.
- `modes`: session modes that suspend the rule, e.g. `reverse-engineering` for `strings`. `messageShort`: shown instead of
  `message` once the full text was seen in the session.
- `when`: where the rule applies at all, checked before the command is read: `all` / `any` / `not` over `bin` (on PATH),
  `os`, `arch`, `host`, `env`, `file` (in the project), `tool` (Bash or Monitor) and `background` (the Bash call asked to run in the
  background), e.g. `{"bin": ["fd", "fdfind"]}`.
  It replaced `requires`, which now makes a rule invalid (skipped and reported) until rewritten.
- `messages`: cases `{"when": ..., "text": ...}` that pick the text by the caught command's shape (`matches`, `wrapped`);
  the first that holds wins, else `message`. Texts take `{found}` (the `bin` name found on PATH), `{ARG}` (what `$ARG`
  captured) and `{{` `}}` for braces. See [references/matching.md](references/matching.md#conditions-when).

`guardrails rule ast '<command>'` prints the parse tree of any command; [references/writing-rules.md](references/writing-rules.md)
is the full how-to (matcher ladder, test matrix, edge cases, pitfalls) and [references/ast/](references/ast/index.md) a cookbook
of tested rules by shape; a test runs every example in it against the real engine. Known gaps: obfuscated or dynamic names
(`$'p\x6bill'`, `P=pkill; $P x`), `find -exec`, `ssh host cmd`, `echo cmd | sh`, scripts run from a file, ANSI-C script literals.

## The runtime

`ast-grep-py` has one wheel per CPython version, so guardrails installs its own Python under `${CLAUDE_PLUGIN_DATA}/runtime/`
(about 140 MB: a hash-pinned portable `uv`, CPython 3.13, the pinned library). Nobody runs an install command: SessionStart
installs it (3 to 10 s the first time), a hook that finds it missing allows the command with a loud notice and installs in the
background, and every `guardrails` CLI call ensures it first. A failed install backs off 10 minutes, then 1 hour, then 6 hours.
Runtimes of other pins are removed after 30 days. macOS (arm64, x86_64) and Linux glibc 2.28+ (x86_64, aarch64) are supported.
**While the runtime is not ready, no rule is enforced**, and the notice says so. The install steps, hosts, trust model and
failure behaviour are in [references/runtime.md](references/runtime.md); `guardrails engine status` shows the state offline.

**If guardrails ever blocks everything**, the user can run `claude plugin disable guardrails@<marketplace>` or, from a terminal,
`guardrails disable` (global hook off; managed rules stay). The hook also fails open, loudly, when its runtime is broken, and a
command the parser cannot finish in 5 seconds is denied ("command too complex to check") rather than let through.

## Config, state and layers

| file | holds |
|---|---|
| managed: `/Library/Application Support/ClaudeCode/guardrails.json` (macOS), `/etc/claude-code/guardrails.json` (Linux); POSIX only | config |
| global: `$XDG_CONFIG_HOME/dev.gaetans.guardrails/claude-plugin/config.json` (an absolute `XDG_CONFIG_HOME` only, else `~/.config/…`) | config |
| project: `<project>/.claude/guardrails.json`, meant to be committed like `settings.json` (none when the project is the home directory) | config |
| state: `${CLAUDE_PLUGIN_DATA}/state.json` (default `~/.claude/plugins/data/guardrails-gaetans-claude-plugins/`) | state |

Config is what you set on purpose: `rules` (each with a `setBy` record of who changed it, when and why), `modes`
declarations and persistent activations, and `enabled` / `disabledReason` (`guardrails disable`). Only the CLI writes it,
atomically, under a lock kept in the data dir (`config.lock`), never next to the config. State is what the hook remembers per
session (retry acknowledgements, session modes, warnings shown), one table for every project since session ids are unique;
the hook writes nothing else, never config, and the state is pruned after 7 days. `guardrails status` names each config
file. Configuration left in `state.json` (`rules`, `modes` or `enabled`), or a project's old
`.claude/plugins/data/guardrails-gaetans-claude-plugins/state.json`, is no longer read: the hook names it once per session
and `guardrails status --problems` lists it until it is moved to the config file.

Layers stack managed > global > project. A lower layer adds rules of its own, and for an id a higher layer defines it can only
tighten: `action` to deny, `retry` off, re-enable, fewer suspending modes, reworded text (not for managed rules); never a
different `match`, `wrappers`, `when` or `messages`. A config with a `wrappers` key (user-defined wrappers were removed) is skipped for that key
and `status` and the hook name it.

**Managed scope** is for rules an organisation or machine owner wants enforced whatever a user or project configures. A
managed rule without `modes` can never be suspended and applies even when the global hook is disabled; lower layers cannot
reword, loosen, disable or relax it, nor switch off a managed mode that is `active`. An unreadable or invalid managed file is
skipped and reported, never treated as a guard that is off, and the CLI never overwrites a corrupt file. Writing it is
governed by OS permissions only: `rule add|set|rm`, `mode declare|undeclare|on|off` and `preset install` take `--scope
managed`, a write that is not permitted exits 2 with the `sudo` command to re-run, and the file is created 0644 in a 0755
directory. `status` warns when the file or its directory is not owned by root or is writable by group or others. Note that
`sudo` drops `CLAUDECODE`, so an agent with passwordless sudo can write the managed file and the `--as-user` gating no longer
applies to it.

`agentMayEnable: false` guards against an honest agent, not a determined one: a forged session record or `env -u CLAUDECODE
guardrails mode on ...` still suspends a rule. The real control is managed permissions and the sandbox. The guard only sees
the Bash tool and is not a security boundary; to make it hard to bypass pair it with managed Claude Code settings (shown for
reference, guardrails does not manage them; `allowManagedHooksOnly` exempts hooks from force-enabled plugins):

```json
{
  "enabledPlugins": { "guardrails@gaetans-claude-plugins": true },
  "allowManagedHooksOnly": true,
  "strictKnownMarketplaces": [{ "source": "github", "repo": "gaetschwartz/claude-plugins" }],
  "allowManagedPermissionRulesOnly": true,
  "permissions": { "deny": ["Write(/etc/claude-code/**)"] }
}
```

## Modes and presets

A mode is declared with `agentMayEnable` (may an agent switch it on for a session when the user says so?) and is on per
session, for a project or globally. Agents switch *session* modes on only for modes that allow it and only by quoting the
user's own words; you get a notice when such a mode first suspends a rule. Activating a mode for a whole project or globally
is a configuration change and needs the user's explicit request (`--as-user`).

| preset | rules |
|---|---|
| `docs-first` | `no-strings` (deny, retry), `binary-spelunking` (otool/nm/objdump, warn); mode `reverse-engineering` |
| `process-safety` | `no-pkill` (pkill/killall, deny, retry), `kill-9` (warn); mode `incident` |
| `modern-cli` | `find-fd`, `grep-rg` (deny, retry, fd/rg cheat sheet), `cargo-nextest` (`cargo test` except `--doc`), `du-dust` (deny, retry); each only when its tool is installed |
| `shell-hygiene` | `pipe-status` (a pipeline's status read from a trailing `tail`/`head`/…, deny), `tail-follow` (foreground `tail -f`, deny), `ps-grep-self-match` (`ps \| grep` without a guard, warn) |

## CLI

An agent runs `guardrails <verb>` (the plugin's `bin/` is on the Bash tool's PATH); from your own terminal use `python3
<plugin dir>/lib/guard.py <verb>`. Verbs: `status`, `rule add|set|rm|test|ast`, `mode declare|undeclare|on|off`, `preset
list|show|install`, `stats`, `audit`, `engine status|ensure`, `enable|disable` (`--scope project` for the project rules); changes take `--scope
global|project|managed`. When run by an agent (`CLAUDECODE` set), configuration changes need `--as-user` and `enable` /
`disable` are refused. `rule test` dry-runs a draft (`--json`) or installed (`--id`) rule against sample commands without
changing anything; it checks the matcher only and prints a `**Note**` line when the hook would not act on a match.

Every `--json` (`rule add`, `rule test`, and `rule set`, which takes a JSON object of fields to change) accepts a literal,
`@<file>` or `-` for stdin (`rule add` and `rule test` also accept `{"rule": {...}, "examples": [...]}`), so a regex full of
backticks and `$(` never has to survive shell quoting; `--examples` takes the same three forms. The CLI prints the final
markdown (the rule card, the status listing) and the skills paste it unchanged: the layout contract is
[references/presentation.md](references/presentation.md).

## Telemetry

Every hook call counts, per rule id, how often the rule denied, warned, passed or was suspended and how long it took, in
`${CLAUDE_PLUGIN_DATA}/telemetry.db` on this machine only. Never a command, argument, path or message, and nothing is sent
anywhere. `guardrails stats` shows it (`guardrails stats --reset` deletes it); what is recorded, the cost and the failure
behaviour are in [references/runtime.md](references/runtime.md#telemetry).

## Audit

Telemetry stores no commands, so `guardrails audit [rule] [-n N] [-A N] [-B N] [-C N] [--all-rules] [--json]` finds the most
recent denials in Claude Code's own session transcripts (`projects/**/*.jsonl` under `$CLAUDE_CONFIG_DIR`, else `~/.claude`,
subagent transcripts included) and shows the messages around each. A denial is a `tool_result` of a Bash or Monitor call whose
text starts with the hook's `[guardrails:<id>#<hash>]` marker, so a transcript that merely quotes the marker never matches.
The hash identifies the rule as it was enforced (`match`, `wrappers`, `when`, `action`, `retry`, `message`, `messageShort`
and `messages`; rewording changes it, `description`, `enabled`, `modes` and `setBy` do not), so the audit keeps only denials
from the rule's current version and counts what it skipped: other version of the rule, recorded before rule hashing (marker
without a hash), rule no longer exists. `--all-rules` also keeps denials by rule ids that are not in the current config.
Right after a rule edit it can therefore show nothing. For each kept denial it runs the current rule on the full denied
command to show the statement that matched (`matched`, null with a note when the replay does not reproduce it). Read-only:
it writes nothing. Commands and results are printed to stdout and can contain secrets. Files are scanned in parallel newest first and the scan stops as
soon as no older file can hold a newer denial. The layout is in [references/presentation.md](references/presentation.md).

## Skills

Eight skills; arguments are free text, each skill parses its own flags, and every long flag has a short form. Shared material
lives in `references/` and is read only when a skill needs it.

| skill | arguments |
|---|---|
| `guardrails:status` | `[-s/--scope global\|project\|managed] [-p/--problems]` |
| `guardrails:stats` | `[-d/--days N] [-s/--slow] [<rule>]` |
| `guardrails:audit` | `[<rule>] [-n/--limit N] [-A/--after N] [-B/--before N] [-C/--context N]` |
| `guardrails:explain` | `[<id>] [-c/--command '<cmd>'] [-s/--scope ...]` |
| `guardrails:new` | `[-B/--block <cmd>]... [-A/--allow <cmd>]... [-s/--scope ...] [-a/--action deny\|warn] [-R/--retry] [-m/--modes a,b] [-i/--id <id>] [-y/--yes] [description]` |
| `guardrails:edit` | `<id> [enable\|disable\|rm\|key=value ...] [-s/--scope ...] [-y/--yes]` |
| `guardrails:mode` | `[on\|off\|declare\|undeclare] [<name>] [-s/--scope ...] [-e/--agent-may-enable]` |
| `guardrails:setup` | `[<preset>...] [-s/--scope ...] [-y/--yes]` |

`status`, `stats`, `explain` and `audit` run forked (Haiku, Haiku, Sonnet and Sonnet) and need no conversation: callers, other agents included, must pass the
rule id or the exact command to `explain`. `audit` judges recent denials from the transcripts and never edits a rule. `new` interviews, tests the rule on your examples and on edge cases it thinks of,
shows the `rule test` card for confirmation and writes the very rule it tested. `edit`, `mode` and `setup` change
configuration only when you ask, always with `--as-user` and your own words in `--reason`, and never run `sudo`: a
not-writable file prints the `sudo` command for you to run.

## Development

`just test` runs the suite on the pinned library; the tests that run the real hook install the real runtime once into
`~/.cache/guardrails-runtime-dev` (they skip when that fails; `GUARDRAILS_REQUIRE_AST=1` makes it a failure, for CI). The
bootstrap tests use a stub uv and also run under macOS's Python 3.9 (`just test-39`). `just check` lints and type-checks,
`just manifest` regenerates the runtime manifest after a pin bump, `just validate` runs `claude plugin validate`.
