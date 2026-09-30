# guardrails

A PreToolUse hook for the Bash and Monitor tools whose rules are data (a rule for `Bash` also applies to a `Monitor`
command; a Monitor call with only a `ws` URL has no command and is ignored; monitors a plugin declares in
`monitors/monitors.json` start without any tool call, so this hook cannot cover them). Each command is tokenised with a real shell lexer
(wrappers like `sudo`/`xargs`/`timeout` are looked through, `bash -c` strings and `$(…)` are descended into, heredoc
bodies and redirect targets are ignored), then checked against the rules in state. Rules that need structure can use a
real syntax-tree matcher (`match.ast`, below).

**Nothing is active after install.** Run the `guardrails:setup` skill to pick presets.

## Rules

A rule matches a command (`program`, `args`, `builtin`, a syntax-tree rule `ast`, raw `regex`) and says what happens:

- `action`: `deny` (the agent gets the message and the call is blocked) or `warn` (the agent gets the message as
  context, once per session).
- `retry: same-command`: the identical command, re-issued in the same session, is allowed. That is the escape hatch
  for the cases a rule cannot anticipate.
- `modes`: session modes that suspend the rule, e.g. `reverse-engineering` for `strings`. Optional; a rule without
  modes is never suspended.
- `requires`: only active when one of these binaries is installed.
- `messageShort`: shown instead of `message` once the full message has been seen in the session.

## The syntax-tree matcher: `match.ast`

`program` and `args` see one flat command at a time; `regex` sees raw text, heredocs and quoted strings included.
`match.ast` is a rule in [ast-grep](https://ast-grep.github.io/)'s vocabulary (`pattern`, `kind`, `regex`, `inside`,
`has`, `follows`, `precedes`, `not`, `any`, `all`, `stopBy`, `field`) run on the tree-sitter Bash parse, so a rule can
say "`pgrep`, but only nested in a substitution, pipeline, list or loop" and never fires on text inside a heredoc or
single quotes. It is an alternative like `regex`: the rule fires when `program`/`args`/`builtin`, or `regex`, or `ast`
matches. `references/matching.md` has the vocabulary, the node kinds (verified), what is code versus data and the
idioms; `guardrails rule ast '<command>'` prints the tree of any command, including the units its wrappers expose.

**Wrappers.** ast-grep does not look through `sudo pkill x`, so the engine does: for every command whose name is in the
wrapper table it drops the wrapper's own flags and arguments (or, for `bash -c '…'`, `eval`, `watch`, `script -c`,
parses the string as code) and matches the inner command too, recursively, keeping the surrounding context. Built in:
`sudo doas env timeout nice ionice nohup time command exec builtin stdbuf setsid xargs watch script eval bash sh zsh dash
ksh`. Add more with `guardrails wrapper add <name> --json '{"flagsWithValue": ["-x"], "shellString": "-c"}'`
(`--scope global|project|managed`, `--path`, `--as-user` for agents, like `rule`); `wrapper rm` and `wrapper list` do
the rest. Look-through only ever grows: the built-in wrappers cannot be redefined and a layer can only introduce NEW
names (an entry for a name that is built in or defined by a higher layer is ignored, with a warning naming it), and a
declared wrapper never hides itself: `pkill x` is still matched as a command even when `pkill` is declared a wrapper.
An unknown wrapper, or an unknown flag that takes a value, is a false-negative risk: the first non-flag word after the
known flags is taken as the command.

**Requirements and fallback.** `match.ast` needs the PyPI wheel `ast-grep-py==0.45.3` (it bundles the Bash grammar), which
exists for CPython 3.10 to 3.14 on macOS and Linux (x86_64, arm64). It lives in a venv at
`${CLAUDE_PLUGIN_DATA}/venv`, built once, atomically, under a lock, from `lib/ast-requirements.txt` (a sha256 for every
wheel; `scripts/regen-ast-requirements.py` rewrites it on a pin bump) with `uv venv` and `uv pip install
--require-hashes` when uv is found, else `python -m venv` and `pip install --require-hashes`. Only the SessionStart
hook (in a detached process, when an enabled rule uses `match.ast`) and CLI commands you run build it; the PreToolUse
hook never installs or downloads anything, it only checks locally whether the venv is ready. A failed or timed-out
build is remembered for ten minutes, stale `.venv-*` leftovers older than ten minutes are removed at the next warm-up,
and a second builder never replaces a good venv. The build runs from the plugin data directory with `--no-config` and
an allowlisted environment (`HOME`, `LANG`, `TMPDIR`, a fixed `PATH`, plus only cache-dir and proxy variables): a
repository's `uv.toml`, `UV_*`, `PIP_*`, `PYTHON*` or `SSL_*` settings can neither redirect the install nor supply
code, and a tampered or unpublished wheel fails the hash check. There is no environment variable that chooses which
executable or engine mode runs (the former test knobs `GUARDRAILS_UV`, `GUARDRAILS_AST_INPROCESS`,
`GUARDRAILS_AST_BOOTSTRAP` and `GUARDRAILS_PARITY` are gone; tests use module attributes). uv and the build
interpreter are looked up in fixed locations first (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, Linux
linuxbrew, PATH last, newest `python3.N` from 3.10 to 3.14 first) and any candidate inside the project directory or the
hook's cwd, owned by another user, in a world-writable directory, or in a group-writable directory owned by another user
is ignored and reported once per session. If the running Python has no wheel (macOS `/usr/bin/python3` is 3.9), a newer
`python3.N` is used for the build, else the notice says "no wheel for Python X.Y; it needs 3.10 to 3.14". Trust model:
the venv is user-owned, hash-pinned at creation, and sits in the plugin data directory, not in the repository. A custom CA
or package mirror is not supported.

**Interpreter selection.** `hooks/guardrails.sh` is a tiny POSIX `sh` script that never changes `PATH` (hooks inherit
Claude Code's environment and the wrapper must not make a macOS lookup touch the `/home` automount). It runs, by absolute
path, the first of: the ready venv's python (steady state: one python process, about 55 ms with AST rules, nothing else on
`PATH` needed); `/opt/homebrew/bin/python3`; `/usr/local/bin/python3`; `/home/linuxbrew/.linuxbrew/bin/python3` on Linux
only (after a `uname -s` check, before any stat of `/home`); `python3` from the inherited `PATH`, skipping entries inside
the project or the cwd; `/usr/bin/python3`. The venv python runs as a child, not via `exec`: if it exits non-zero the
wrapper reruns the same payload with the system python and a notice. With no python at all it prints a one-line
`systemMessage` on every call (it cannot keep per-session state without python) and lets the call through. Latency
measured here: no AST rules about 35 ms, 3 or 30 AST rules about 55 ms, a system python that must start the venv's python
as a second process about 78 ms.

**Degraded mode.** If the venv is not built yet or cannot run, the run exceeds its deadline, a worker reply is
malformed, a command nests wrappers more than 16 deep or expands past 512 units, or a command is larger than 16 KiB, the
hook does not allow silently. Every non-AST rule runs as usual (oversize commands skip the lexer and use command names
and regexes, scanning the whole text linearly), and a rule with `match.ast` is applied when the command mentions one of
its command names as a word, after shell quoting is resolved (`p''kill`, `p\kill`, `$'p\x6bill'` and `"pkill"` all read
as `pkill`): the names derived from its patterns and regexes, or the optional `match.mentions` list. A deny rule then
denies with its message plus a note that the AST matcher was unavailable and why, a warn rule warns, and commands that
mention none of the names pass. The warning reaches both the user (`systemMessage`) and the agent (`additionalContext`,
or the deny reason), once per session. An ast rule with no derivable names and no `mentions` cannot fire in this mode.
The lexer is linear-time on every construct and the whole hook runs under a 7 s watchdog (hook timeout 10 s): on expiry
or any other exception it falls back to a minimal evaluation (stdlib matchers and names, no session state) and, if that
also fails, a visible warning; it never exits silently. `rule test` reports degradation as a note and exits 0. The plain
lexer's commands are always matched against `match.ast` rules too, so the AST path never sees less than the lexer does.

Known gaps, for any engine: `find -exec`/`-execdir`, variable-held names (`P=pkill; $P x`), `bash <<< 'cmd'`,
`echo cmd | sh`, `su -c`, `ssh host cmd`, and scripts run from a file.

### Worked example: one bundled policy, three AST rules

A policy that says "do not kill by name, do not use `pgrep` inside a substitution, pipeline or loop, do not pipe PIDs
into `xargs kill`" is three rules with their own message. Each rule below was run with `guardrails rule test` against
`pkill node`, `sudo killall Finder`, `bash -c 'pkill x'`, `kill $(pgrep -f vite)`, `pgrep -xl node`,
`pgrep node | head -1`, `if pgrep -q x; then echo up; fi`, `ps aux | xargs kill -9`, `xargs kill < pids`,
`echo "pkill is banned"`, a heredoc that mentions `pkill`, and `kill 4242`; each selects exactly the commands its
name says and nothing else (a heredoc or a quoted mention is data).

```json
{"match": {"ast": {"any": [{"pattern": "pkill $$$"}, {"pattern": "killall $$$"}]}}, "message": "Do not kill by name: it can hit your own shell or an innocent process. Look the PID up with `pgrep -xl <name>` as its own command, then `kill <pid>`."}
```

```json
{"match": {"ast": {"pattern": "pgrep $$$", "inside": {"any": [{"kind": "command_substitution"}, {"kind": "pipeline"}, {"kind": "list"}, {"kind": "while_statement"}, {"kind": "if_statement"}, {"kind": "for_statement"}], "stopBy": "end"}}}, "message": "Run `pgrep -xl <name>` as its own command and read the PID; do not nest it in a substitution, pipeline, list or loop."}
```

```json
{"match": {"ast": {"pattern": "xargs kill $$$", "inside": {"kind": "pipeline"}}}, "message": "Do not pipe PIDs into `xargs kill`; read them first, then `kill <pid>`."}
```

Add each with `guardrails rule add <id> --json - <<'EOF' … EOF`. A `regex` for the first two would also match
`echo "pkill x"` and heredocs, and would need a pattern per wrapper form; `regex` stays the tool for dataflow the tree
cannot express.

**Matcher parity.** `program`, `args` and `builtin` stay on the stdlib lexer. `lib/parity.py` compiles them into
`ast` rules and `tests/parity_study.py` (`matching.PARITY` switches the engine itself) runs the whole corpus plus
a fuzz over command shapes through both: on the corpus the only difference is the unbalanced-quote fallback, and on
the fuzz the tree is right where the lexer loses commands in nested substitutions (`echo "$(nm $(z))"`) or misses a
pipeline marker; an `args` regex anchored on the joined arguments cannot be expressed as an ast rule, so the two
engines are not interchangeable. Tests switch engine modes through module attributes (`matching.PARITY`, `astrun.INPROCESS`, `astrun.BUILD_ALLOWED`), never through the environment.

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
list: `status`, `rule add|set|rm|test|ast`, `wrapper add|rm|list`, `mode declare|undeclare|on|off`,
`preset list|show|install`, `enable|disable`; changes take `--scope global|project|managed`, with `--path <file>` to pick a managed-format file). When run by an agent (`CLAUDECODE` set), configuration changes need `--as-user`, and
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

`just test` runs the suite under `uv run --with ast-grep-py==0.45.3` (an unhashed, test-only install), so the AST tests run;
plain `python3 -m unittest discover -s tests` works too, and the tests that need `ast-grep-py` then build the hashed venv
(network once) or skip with a message saying how to get it; set `GUARDRAILS_REQUIRE_AST=1` to make that a failure instead
(for CI). The fallback and build tests stub the external commands.
`just check` lints and type-checks, `just validate` runs `claude plugin validate`.

## Layout

| path | role |
|---|---|
| `lib/` | all Python: `guard.py` (entry point: hook without arguments, CLI with them), `engine.py`, `matching.py` (the one evaluation path), `policy.py`, `store.py`, `cli.py`, `shellwords.py` (plain lexer), `wrappers.py` (wrapper table), `astrun.py` + `astworker.py` (the venv builder/runner and the ast-grep worker), `parity.py` |
| `hooks/` | `hooks.json` (PreToolUse on `Bash\|Monitor`, and SessionStart to build the AST venv) and the `guardrails.sh` POSIX wrapper that picks the interpreter |
| `scripts/` | `regen-ast-requirements.py`: rewrites the hashed requirements for the pin |
| `references/` | text the skills read on demand: matching semantics, presentation conventions, config-change rules |
| `bin/guardrails` | the CLI wrapper on the Bash tool's PATH |
| `presets/`, `skills/`, `tests/` | preset rule sets, the six skills, shared skill references, the unittest suite |
