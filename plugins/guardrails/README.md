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
`references/writing-rules.md` is the full how-to (matcher ladder, test matrix, edge-case checklist, the six pitfalls),
and `references/ast/` is a cookbook of tested rules by shape (context, pipelines, flags, lists, wrappers); a test runs
every example in it against the real engine.

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

**Requirements and fallback.** `match.ast` runs the standalone Rust `ast-grep` binary (pinned to 0.45.3, which bundles the
Bash grammar) as a child process of the hook. The hook itself is plain stdlib Python, any Python 3.9 or newer, so macOS
`/usr/bin/python3` works: there is no venv, no uv and no pip. The binary reaches the machine by two mechanisms, tried in
this order:

1. **npm.** The plugin ships `package.json` and `package-lock.json` (an exact pin, sha512 integrity for all four platform
   packages, no second lockfile: with several Claude Code would pick bun first and not fall back to npm). When Claude Code
   copies a marketplace plugin into its cache it runs `npm ci --ignore-scripts` there (60 s limit; a failure never blocks
   the plugin), which installs `node_modules/@ast-grep/cli-<platform>/ast-grep`. The hook runs that real binary by
   absolute path; the `@ast-grep/cli` launcher script, `node_modules/.bin` and `PATH` are never involved.
   **A plugin loaded in place from a local-directory marketplace never gets this install** (Claude Code skips it), so it
   uses the wheel fallback, or you run `cd <plugin root> && npm ci --ignore-scripts` once.
2. **Wheel fallback.** PyPI's `ast-grep-cli` ships the same binary in a `py3-none-<platform>` wheel. `guardrails engine
   install` (foreground, with progress and exit codes) or the detached SessionStart warm-up downloads it with urllib from
   the one https://files.pythonhosted.org URL recorded in `lib/engine-manifest.json` (connect and total time limits, 60 s),
   checks the wheel's sha256 **before** reading it, unpacks exactly one named member (no other entry, no symlink, no
   absolute or `..` path is ever read), and places `${CLAUDE_PLUGIN_DATA}/engine/<pin>/ast-grep` (temp file, `chmod 0755`,
   atomic rename) and then a marker (pin, wheel hash, binary hash, size, mtime, inode). Installs run under a lock, never
   replace an install that still passes its checks, and any failure (timeouts included) is remembered for ten minutes: the
   warm-up obeys that backoff, an explicit `engine install` does not. Temp files older than ten minutes are removed.
   Only the SessionStart hook (detached, silent, and only when neither mechanism works and an enabled rule uses
   `match.ast`) and the CLI ever download; the PreToolUse hook only looks.
3. **Neither.** A loud notice (below) and the degraded mode.

`lib/engine-manifest.json` (wheel filenames, URLs, sha256, expected member and binary hashes per platform, and the binary
hash of each npm platform package) is generated by `scripts/gen-engine-manifest.py` from PyPI and npm for the version in
`package.json`; a test checks that `package.json`, `package-lock.json` and the manifest pin the same version.

| platform | npm package | wheel |
|---|---|---|
| macOS arm64 | `@ast-grep/cli-darwin-arm64` | `macosx_10_12_universal2` |
| macOS x86_64 | `@ast-grep/cli-darwin-x64` | `macosx_10_12_universal2` |
| Linux glibc x86_64 | `@ast-grep/cli-linux-x64-gnu` | `manylinux_2_28_x86_64` (glibc 2.28+) |
| Linux glibc aarch64 | `@ast-grep/cli-linux-arm64-gnu` | `manylinux_2_28_aarch64` (glibc 2.28+) |

Linux musl (Alpine) and Windows are not supported: there is no build, and the notice says so and offers no install command,
only to remove or disable the `match.ast` rules or use a supported system.

**Trust model.** Before each run the hook checks the binary cheaply. npm binary: a regular file (no symlink) that resolves
inside the plugin root, executable, owned by you or root, not writable by group or others, every directory up to the plugin
root owned by you or root and not world-writable, and its platform
package's `package.json` version equals the pin. Wheel binary: the same, plus size, mtime and inode must match the marker
and the marker's pin and hashes must match the manifest. The one relaxation is that a group-writable directory owned by you
or root is fine, because a `0002` umask (and some Homebrew layouts) creates them that way. A full re-hash happens only at
install time and in `guardrails engine verify` (the active binary against the manifest's `binarySha256`; for npm also npm's
own install record against `package-lock.json`). The child runs with the environment `PATH=/usr/bin:/bin` and nothing else
(no `UV_*`, `PYTHON*`, `NODE_*`, `LD_*`), from `/`, with `-c lib/engine-sgconfig.yml`: without an explicit config ast-grep
reads an `sgconfig.yml` from the working directory or a parent, which can name a dynamic library to load. No environment
variable selects the binary, the URL or the plugin root (the root is the parent of `lib/`, which is where Claude Code runs
`npm ci`); tests use module attributes. Install and verify change no configuration, so `--as-user` does not apply to them.

**Troubleshooting.** `guardrails engine status` shows which mechanism is active (npm or wheel), the pin, the platform,
both paths with their state (usable, or why it was rejected), the last download failure with its retry time, and the exact
fix commands: `guardrails engine install` and `cd "<plugin root>" && npm ci --ignore-scripts`. `guardrails engine verify`
re-hashes the active binary. `guardrails status --problems` and `rule test` repeat the fix when the engine is missing.

**Interpreter selection.** `hooks/guardrails.sh` is a tiny POSIX `sh` script that never changes `PATH` (hooks inherit
Claude Code's environment and the wrapper must not make a macOS lookup touch the `/home` automount). It runs, by absolute
path, the first of: `/opt/homebrew/bin/python3`; `/usr/local/bin/python3`; `/home/linuxbrew/.linuxbrew/bin/python3` on Linux
only (after a `uname -s` check, before any stat of `/home`); `python3` from the inherited `PATH`, skipping entries inside
the project or the cwd; `/usr/bin/python3`, always with `-I -S`. With no python at all it prints a one-line
`systemMessage` on every call (it cannot keep per-session state without python) and lets the call through. Latency
measured here (Python 3.14 from Homebrew, 3 AST rules, typical command, warm, machine load about 5): about 50 ms for a
command with no wrapper, 55 to 60 ms through a wrapper or shell string, against about 35 ms with no AST rules. Apple's
`/usr/bin/python3` 3.9 spends about 50 ms just importing the standard library (it ships no cached bytecode), so a hook
running under it takes about 115 ms; install a newer `python3` in `/opt/homebrew/bin` or `/usr/local/bin` to avoid that. The very
first run of a binary that was just written takes about half a second once (the OS inspects it); the 4 s engine deadline
covers that.

The binary's location comes only from the plugin's own location and data dir, never from `PATH`, the project or the cwd, so a
project or cwd that contains the plugin (developing it, or starting in `~/.claude`) does not matter, and a `node_modules` planted
in a repository is never looked at.

**How an evaluation runs.** `ast-grep` has no ancestry or wrapper knowledge, so the worker drives it with small tasks: the
parse tree of each candidate source comes from `ast-grep run --debug-query`, the wrapper and normalisation rewrites are done
in Python exactly as before, and every unit (the command as written, its normalised form, each rewrite, the plain
lexer's commands) is scanned by one `ast-grep scan` (each rule plus a twin that adds `inside pipeline | command_substitution |
process_substitution`, which is how `wrapped` is told from `direct`). Rules and units never travel on the command line:
they are files in a private (`0700`) temporary directory with its own `sgconfig.yml`, so no rule or command size can overflow
the argument list. Every call also carries a canary rule (`kind: program`) that must match every non-empty unit, and the reply
must be exactly ast-grep's JSON list with known rule ids and in-range offsets; anything else (empty output, `{}`, a missing
canary, a truncated reply, a non-zero exit) counts as an engine failure and degrades that call with a warning. Scanning 512
units takes about 70 ms. Units that cannot hide another wrapper are not even parsed; the lexer's units are de-duplicated.
Expansion is bounded by counts and bytes, never by the clock, so the same command gets the same answer under any load: at
most 512 distinct units, 16 levels, 32 KiB per unit and about 1 MiB of parsed text, and the notice says which bound was hit.
A `match.ast` rule is limited to 16 KiB (`rule add/set/test` exit 2, and the hook skips and names an oversized one without
touching the others) and the enabled rules together to 256 KiB (later rules are skipped and named).

**Degraded mode.** If the engine is missing or cannot run, the run exceeds its deadline, a reply is malformed, a command
nests wrappers more than 16 deep, expands past 512 distinct units or 1 MiB of text, or is larger than 16 KiB, the
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

**The missing-engine notice.** When neither the npm binary nor the wheel binary is usable and an enabled `match.ast`
rule applies, the first Bash or Monitor call of every session (until it is fixed) carries, in both the user-facing
`systemMessage` and the agent-facing `additionalContext`, a notice that begins `GUARDRAILS ENGINE MISSING`: it says
`match.ast` rules are not being enforced, what still is, the precise reason (unsupported platform, npm install missing or
failed, or the download failure with its retry time), tells the agent to tell the user first, and gives both fixes. The
text is sanitised and is framed as coming from the guardrails plugin, not from the repository. If the once-per-session
mark cannot be saved, the notice simply repeats.

Every `$'...'` string is decoded the way the shell does (`\xHH`, octal, `\u`/`\U`, `\n \t \e \a \b \f \r \v`, `\cX`, a NUL
ends the string) in command names and arguments by the plain lexer, by the wrapper and normalisation step, and by the
degraded name scan, so `$'p\x6bill' x` is seen as `pkill x` everywhere.

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
engines are not interchangeable. Tests switch engine modes through module attributes (`matching.PARITY`), never through the environment.

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
  `new` embeds a short how-to and three example rules, and points to `references/writing-rules.md` (read for any
  non-trivial rule) and `references/ast/index.md`; `edit` and `explain` point to the same two.
- `edit`, `mode` and `setup` change configuration only when you ask, always with `--as-user` and your own words in
  `--reason`. They never run `sudo`: a not-writable file prints the `sudo …` command for you to run.

`just test` first runs `just engine`, which installs the pinned binary through the wheel path into
`~/.cache/guardrails-engine-dev` (network once; it is skipped quietly when offline), then runs the suite; the tests that need
the real binary use that install, or one they make themselves in a temporary directory, and otherwise skip with a message
saying how to get it (set `GUARDRAILS_REQUIRE_AST=1` to make that a failure, for CI). Plain `python3 -m unittest discover -s
tests` works too, under any Python from 3.9 (`just test-39` runs it under `/usr/bin/python3`). The install, download and
fallback tests stub the network and use a stub binary. `just check` lints and type-checks, `just validate` runs
`claude plugin validate`.

## Layout

| path | role |
|---|---|
| `lib/` | all Python: `guard.py` (entry point: hook without arguments, CLI with them), `engine.py`, `matching.py` (the one evaluation path), `policy.py`, `store.py`, `cli.py`, `shellwords.py` (plain lexer), `wrappers.py` (wrapper table), `astbin.py` (find, install and verify the ast-grep binary), `astrun.py` (run a request against it), `astworker.py` (the wrapper look-through and rule evaluation) and `astcli.py` (the ast-grep subprocess calls), `ansic.py` (`$'...'` decoding), `parity.py`; data: `engine-manifest.json`, `engine-sgconfig.yml` |
| `hooks/` | `hooks.json` (PreToolUse on `Bash\|Monitor`, and SessionStart to fetch the AST engine) and the `guardrails.sh` POSIX wrapper that picks the interpreter |
| `package.json`, `package-lock.json` | the npm pin Claude Code installs with `npm ci --ignore-scripts` |
| `scripts/` | `gen-engine-manifest.py`: rewrites `lib/engine-manifest.json` for the pin |
| `references/` | text the skills read on demand: matching semantics, presentation conventions, config-change rules, the rule-writing guide (`writing-rules.md`) and the tested `ast/` cookbook |
| `bin/guardrails` | the CLI wrapper on the Bash tool's PATH |
| `presets/`, `skills/`, `tests/` | preset rule sets, the six skills, shared skill references, the unittest suite |
