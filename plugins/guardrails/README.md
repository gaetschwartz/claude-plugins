# guardrails

A PreToolUse hook for the Bash and Monitor tools whose rules are data (every rule also applies to a `Monitor`
command; a Monitor call with only a `ws` URL has no command and is ignored; monitors a plugin declares in
`monitors/monitors.json` start without any tool call, so this hook cannot cover them). Each command is parsed in
process by the `ast-grep-py` library (its tree-sitter Bash grammar does all the lexing and parsing; guardrails has no
shell parser of its own). `program`, `args`, `builtin` and `match.ast` rules become typed ast-grep rules: wrappers like
`sudo`/`xargs`/`timeout` are transparent (the command is also matched as if the wrapper were not there), the scripts of
`bash -c` and `eval` are scanned as units of their own, heredoc bodies and redirect targets are data. `regex` rules read
the raw text of the whole command with ast-grep's own regex engine (Rust syntax). The library lives in a runtime that guardrails installs by itself (below); nobody runs an install command.

**Nothing is active after install.** Run the `guardrails:setup` skill to pick presets.

## Rules

A rule matches a command (`program`, `args`, a syntax-tree rule `ast`, raw `regex`) and says what happens:

- `action`: `deny` (the agent gets the message and the call is blocked) or `warn` (the agent gets the message as
  context, once per session).
- `retry: same-command`: the identical command, re-issued in the same session, is allowed. That is the escape hatch
  for the cases a rule cannot anticipate.
- `modes`: session modes that suspend the rule, e.g. `reverse-engineering` for `strings`. Optional; a rule without
  modes is never suspended.
- `requires`: only active when one of these binaries is installed.
- `messageShort`: shown instead of `message` once the full message has been seen in the session.

## The syntax-tree matcher: `match.ast`

`program` and `args` match one command at a time; `regex` (Rust regex syntax: linear time, no backreferences or look-around;
`$` matches only at the very end and `^` at the first token) sees raw text, heredocs and quoted strings included.
`match.ast` is a rule in [ast-grep](https://ast-grep.github.io/)'s vocabulary (`pattern`, `kind`, `regex`, `inside`,
`has`, `follows`, `precedes`, `not`, `any`, `all`, `stopBy`, `field`) run on the tree-sitter Bash parse, so a rule can
say "`pgrep`, but only nested in a substitution, pipeline, list or loop" or "`npm publish`, unless inside an `if`" and
never fires on text inside a heredoc or single quotes. It is an alternative like `regex`: the rule fires when
`program`/`args`, or `regex`, or `ast` matches. `references/matching.md` has the vocabulary, the node kinds
(verified), what is code versus data and the idioms; `guardrails rule ast '<command>'` prints the tree of any command,
including the shell-string units. `references/writing-rules.md` is the full how-to (matcher ladder, test matrix,
edge-case checklist, the pitfalls), and `references/ast/` is a cookbook of tested rules by shape (context, pipelines,
flags, lists, wrappers); a test runs every example in it against the real engine.

**Wrappers and shell strings.** A command that contains a wrapper command (`sudo doas env timeout nice ionice nohup time
command exec builtin stdbuf setsid xargs watch`, plus your own) is matched as text variants too: the whole command with that
wrapper replaced by the text from each of its own words onward (`sudo -u bob pkill x` also reads as `-u bob pkill x`,
`bob pkill x`, `pkill x`, `x`), several wrappers replaced together while the combinations stay few. Every rule is matched
on the real tree of each variant, so a `program`, a command `pattern`, a pipeline or list pattern (`curl $$$ | sh`,
`cd $A && rm $$$`) and a `not inside` all see through wrappers, and a hit found in a variant is `wrapped`. There is no table
of flags that take a value, so any word can start a command: `sudo grep pkill file` also matches `pkill`, and
`sudo grep curl f | sh` matches a `curl $$$ | sh` rule (known coarseness). At most 2048 variants and 16 MiB of variant text
parsed in total per command (the 5 s deadline bounds the time as well); past that the command is denied ("command too complex to check") whatever its size. The script of
`bash|sh|zsh|dash|ksh|script [flags] -c '<script>'`, the arguments of `eval`, heredocs and here-strings fed to a shell, and
the substitutions in unquoted heredoc bodies are unquoted (one shell word, nothing else) and scanned as units of their own
(depth 8, 64 distinct units, 256 KiB). Add wrapper names with `guardrails wrapper add <name>`
(`--scope global|project|managed`, `--path`, `--as-user` for agents, like `rule`); `wrapper rm` and `wrapper list` do the
rest. Look-through only ever grows: a layer adds names and never removes or changes a higher layer's.
Obfuscated or dynamic command names (`$'p\x6bill'`, `p''kill`, variables, aliases, functions) cannot be analysed
statically and are not matched; `references/matching.md` lists the known limits.

**The runtime.** `ast-grep-py` has one wheel per CPython version, so guardrails does not depend on the host's Python: it
installs its own, once, under `${CLAUDE_PLUGIN_DATA}/runtime/<id>/` (about 140 MB on disk: a portable `uv` 34 MB, a managed
CPython 3.13 67 MB, a venv with ast-grep-py 42 MB; about 46 MB downloaded). The user never runs an install command; setup
happens at every opportunity:

1. **SessionStart** runs `ensure` synchronously (timeout 120 s). The first ever session takes about 3 to 10 s (Claude's
   first answer waits for it); later sessions find a valid marker and take about 0.1 s. A dead network does not stall
   every session: the connect timeout is 10 s, an install has a 60 s budget in all, and a failed install is not retried
   while its backoff runs (10 minutes after the first failure, then 1 hour, then 6 hours, reset by a success): SessionStart
   prints the notice with the retry time instead of trying.
2. **The PreToolUse hook** runs on the runtime's Python when the marker exists. When it does not, it allows the command with a
   loud notice (once per session, repeated every 10 minutes while a failure persists) and starts a detached `ensure`
   (obeying the same lock and backoff), so the next call is covered.
3. **Every `guardrails` CLI call** (`bin/guardrails`) runs at once on a ready runtime; otherwise it ensures first, in the
   foreground, with progress on stderr, and stops with one line (the reason and the next retry time). `guardrails engine
   status` needs no runtime and works offline.
4. **The `setup` skill's first step** runs `guardrails engine ensure`.

**While the runtime is not ready, no rule is enforced**, `regex` rules included, because every rule runs on the managed
Python. That window is the first seconds of the first session; after a failed install (offline, a blocked host) it lasts
until an attempt works. The notice (user-facing `systemMessage` and agent-facing `additionalContext`) says the runtime is
being installed automatically and how long that takes, or the failure and the time of the next attempt, and that rules are
NOT enforced meanwhile. Its wording is fixed by the plugin and a failure is only ever one of a few classes (dns, connect,
timeout, tls, http, hash, disk, tool, crash), never text from the network or the repository; the raw detail goes to
`runtime/install.log` in the data dir, which `engine status` names. With no usable `python3` (3.9 or newer; the wrapper
smoke-tests each candidate and names a broken one) it prints a fixed notice on every call.

**The data dir is the one thing trusted.** The runtime is found only through `CLAUDE_PLUGIN_DATA`, which must be an absolute,
canonical path (no `..`, no symlink in it), owned by you and not writable by group or others; otherwise the notice says
"plugin data dir is not a safe absolute path: <path>", nothing is run from it and nothing is written to it. Where the project
or the cwd is (`$HOME`, `~/.claude`, `/`, the data dir's parent) never matters.

**If guardrails ever blocks everything.** The user can run `claude plugin disable guardrails@<marketplace>` or, from a
terminal, `guardrails disable` (global hook off; managed rules stay). The hook itself also fails open, loudly, when its
runtime is broken: a crash of the library triggers a health probe on a trivial command in a fresh child. If that crashes
too the library is broken: the command is allowed with a notice that repeats every 10 minutes, the runtime is marked broken
and rebuilt in the background (same backoff). If the probe passes, only that command is denied ("this command crashes the
parser"). A command the parser does not finish in 5 s is denied.

`lib/bootstrap.py` and `lib/hostcli.py` are the only code that runs on the host's own Python (3.9 or newer, standard library
only; everything else needs 3.13). `hooks/guardrails.sh` is a tiny POSIX `sh` script that never changes `PATH` (hooks inherit
Claude Code's environment and a macOS lookup must not touch the `/home` automount). On a ready runtime it runs the hook (or
the CLI) on the runtime's Python and reports a hook that dies. Otherwise it starts the bootstrap with the first of
`/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `/usr/bin/python3`, `/home/linuxbrew/.linuxbrew/bin/python3` on Linux
only (after a `uname` check), then `python3` from the inherited `PATH`, skipping relative entries, any inside the project or
the cwd and any that is world-writable, always with `-I -S`. The bootstrap downloads the pinned `uv` wheel from the one
files.pythonhosted.org URL in `lib/runtime-manifest.json` (the sha256 is checked **before** the file is read; exactly one
named member is unpacked), then runs that uv with a scrubbed environment (HOME, LANG, TMPDIR, proxy variables and
`SSL_CERT_*` only, `PATH=/usr/bin:/bin`, every `UV_*` set to a place inside the runtime dir, `--no-config`, the runtime dir
as cwd, so a repository's `uv.toml`, `UV_*`, `PATH`, `PYTHONPATH` or `pip.conf` never decide what runs), each step in its own
process group killed at the deadline: `uv python install 3.13`, `uv venv`, `uv pip install --require-hashes --only-binary
:all: --no-deps` from `lib/runtime-requirements.txt` (ast-grep-py 0.45.3, one hash per platform wheel). A self-test imports
the library and matches a pipeline; the marker is written last (the pins, the installed Python, and the size and mtime of
the interpreter and the extension module), the uv cache is deleted. Runtimes of other pins (another plugin version sharing
the data dir) are kept until they are 30 days old, so two versions never delete each other's; nothing is deleted on the hot
path. Installs run under a lock the OS releases if the installer dies; an interrupted install has no marker and the next one
wipes it. The manifest, requirements and runtime id come from `scripts/gen-runtime-manifest.py`; a test ties them to the pins.

| platform | uv wheel | ast-grep-py wheel |
|---|---|---|
| macOS arm64 | `macosx_11_0_arm64` | `cp313 macosx_11_0_arm64` |
| macOS x86_64 | `macosx_10_12_x86_64` | `cp313 macosx_10_12_x86_64` |
| Linux glibc x86_64 | `manylinux_2_17_x86_64` | `cp313 manylinux_2_28_x86_64` (glibc 2.28+) |
| Linux glibc aarch64 | `manylinux_2_17_aarch64` | `cp313 manylinux_2_28_aarch64` (glibc 2.28+) |

Linux musl (Alpine), older glibc, other architectures and Windows get a precise "unsupported platform" notice and no
install attempt.

**Trust model.** The Python comes from `uv python install`, which downloads a python-build-standalone distribution and checks
it against the sha256 compiled into the pinned uv (a tampered archive served from a local mirror stopped with `Hash
mismatch`): our manifest pins uv, uv pins Python, `--require-hashes` pins the library. Hosts needed: `files.pythonhosted.org`
and `pypi.org` (uv wheel, ast-grep-py wheel and index metadata), `releases.astral.sh` for the Python (uv falls back to
`github.com/astral-sh/python-build-standalone` releases, served from `release-assets.githubusercontent.com`, when it cannot
reach it; both observed through a logging proxy). `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and `SSL_CERT_FILE/DIR` are passed on;
no mirror or index variable is, so a repository cannot redirect a download, and a corporate mirror is not supported. At run
time the wrapper executes the runtime's Python only when the data dir passes the checks above, the marker exists and the
interpreter is owned by you; every session start (and each `ensure`) re-checks that the interpreter and the
extension module are owned by you, not writable by group or others, unchanged in size and mtime since the install and still
inside the runtime dir, and reinstalls when they are not. The hook runs `python -I`, so `PYTHON*` and the cwd never reach it.

**Troubleshooting.** `guardrails engine status` shows whether the runtime is ready (or why not), the pins, the platform, the
path, the installed Python, whether an install is running, and the last install failure (its class, how many in a row, the
time of the next automatic attempt and where the raw detail is); `guardrails engine ensure --retry-now` installs right away and ignores the wait. `guardrails status
--problems` and `rule test` say when a rule is not enforced or could not be judged.

**How an evaluation runs.** Every `program`, `args`, `regex` and `match.ast` rule becomes typed ast-grep rules
(`lib/rulebuilder.py`); `lib/scanner.py` parses the command as written, then its wrapper variants and each shell string,
and matches every rule on each; a hit is `wrapped` when it was found in a variant or a script, or when the matched node
sits inside a pipeline, command substitution or process substitution (read from the node's ancestors). Anything that
needs the parser runs in a forked child (`lib/bounded.py`) that is killed after 5 s: a call into ast-grep cannot be
interrupted (it holds the GIL, verified), a hook that times out lets the command through, so a command the parser cannot
finish in time is denied ("command too complex to check") instead. A typical command costs about 2 ms of fork and 8 ms
of parsing and matching in the child. A `match.ast` rule is limited to 16 KiB (`rule add/set/test` exit 2, and the hook
skips and names an oversized one without touching the others).

**When the engine fails.** There is no degraded parsing, and no rule runs outside the engine: `regex` rules are evaluated by
ast-grep's own regex engine (Rust syntax, linear time: no backreferences or look-around, `rule add/set/test` reject them
with exit 2 and a state rule that does not compile is skipped and named) inside the same bounded child, so a catastrophic
pattern can never outlast the deadline. If the library cannot be imported or its self-test fails, rules cannot be evaluated:
the command is allowed with a loud warning naming the reason (repeated at most every 10 minutes). Managed deny rules fail
open too in that case; the warning and `status --problems` name them. A command over 256 KiB, one that unpacks past the
shell-string or variant bounds, one the parser does not finish in 5 s, or one that crashes the parser is denied unparsed:
padding must never be a way past a rule. Only a deny rule that could not be judged causes the denial; if just `warn` rules
could not be judged the command is allowed with a warning. If the hook itself raises, it allows with a visible warning that
no rule was applied, and never exits silently.

`rule test` and `status` say the same: a rule that needs the engine is reported as `cannot` evaluate (never as "no
match"), and `status --problems` lists the rules that are not enforced and why.

Known gaps, for any engine: `find -exec`/`-execdir`, variable-held or obfuscated names (`P=pkill; $P x`, `$'p\x6bill'`),
`bash -c $'cmd'` and `eval $'cmd'` (ANSI-C script literals), `echo cmd | sh`, `cat <<EOF | sh`, `source <(echo cmd)`, `su -c`, `ssh host cmd`, `watch 'cmd'`, scripts run from a file.

### Worked example: one bundled policy, three rules

A policy that says "do not kill by name, do not use `pgrep` inside a substitution, pipeline or loop, do not pipe PIDs
into `xargs kill`" is three rules with their own message. Each rule below was run with `guardrails rule test` against
`pkill node`, `sudo killall Finder`, `bash -c 'pkill x'`, `kill $(pgrep -f vite)`, `pgrep -xl node`,
`pgrep node | head -1`, `if pgrep -q x; then echo up; fi`, `ps aux | xargs kill -9`, `xargs kill < pids`,
`echo "pkill is banned"`, a heredoc that mentions `pkill`, and `kill 4242`; each selects exactly the commands its
name says and nothing else (a heredoc or a quoted mention is data).

```json
{"match": {"program": ["pkill", "killall"]}, "message": "Do not kill by name: it can hit your own shell or an innocent process. Look the PID up with `pgrep -xl <name>` as its own command, then `kill <pid>`."}
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
that mode. Every layer can also add wrapper names under `wrappers` (an object whose keys are the names; values are
ignored); names only accumulate. Session state (retry acknowledgements, modes, warnings shown) lives in the global file
and is pruned after 7 days.

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
umask) and the file is written mode 0644 atomically. `status` warns when the managed file or its directory
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
`preset list|show|install`, `enable|disable` (`--scope project` for the project rules); changes take `--scope global|project|managed`, with `--path <file>` to pick a managed-format file). When run by an agent (`CLAUDECODE` set), configuration changes need `--as-user`, and
`enable`/`disable` are refused. `rule test` dry-runs a draft (`--json`) or installed (`--id`) rule against sample
commands without changing anything. It checks the matcher only (a row is caught or allowed); it prints a `**Note**` line when the hook
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

### Output the skills paste verbatim

Claude Code cannot show a command's output in the chat by itself; an agent has to paste text. So the CLI prints the
final markdown, computed from real results, and the skills paste it unchanged.

`guardrails rule test` prints the rule card for a rule and a set of commands: title, `**Intent**`, `**Match**`,
`**Message**`, then `**Block**` / `**Warn**` (the matcher catches it) and `**Allow**` rows, `**Verified**`, `**Note**`
and `**Raw**`. Commands come from positional arguments (source `inferred`, or `--source`) and from `--examples
@file|-`, a JSON list of `{"cmd", "source": "yours|inferred|you chose", "expect": "match|pass"}`; `--intent`,
`--id-name` and `--scope` label a draft. The matcher computes each row's `wrapped` tag (the match reached the rule through
sudo, `bash -c`, xargs, timeout, `$(…)` or a pipeline), and `expect` only feeds the mismatch count: a contradicted row
gets a `⚠`. Every command is an inline-code span padded inside the backticks to one width (the longest command, capped
at 40; longer ones go last, unpadded; CJK and emoji count two columns; a command with a backtick gets the longer fence;
newlines show as `⏎`).

`guardrails status` prints the state listing the same way: a first line about the managed files (platform file
present or absent, overrides and `--path` files), one row per rule (`id`, action, origin layers, state: `always
enforced`, `suspended by <modes>`, `disabled`, `enabled`), the modes and the problems. `status` also takes
`--scope global|project|managed` (only rules, modes and problems of that layer) and `--problems`; `--rule <id>` prints
just that rule's row. `references/presentation.md` is the layout contract.

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
  pastes the output of `guardrails status` verbatim, so it is cheap and needs no conversation.
- `explain` answers why a command was denied or not caught and what a rule covers, pasting the `rule test` card. It runs
  forked on Sonnet because it reasons over the matching semantics (wrappers, `args` versus
  `match.regex`, layering, modes) in `references/matching.md`, which the skill injects with `!` commands so the fork is
  self-contained. A fork has no conversation history, so callers, other agents included, must pass the
  rule id or the exact command. `allowed-tools` pre-approves only `guardrails status`, `guardrails rule test` and `cat` of the references, and
  the skill is told never to change anything; `allowed-tools` does not itself restrict the other tools. Denial messages
  stay the first source.
- `new` interviews, tests the rule on your examples and on edge cases it thinks of, asks only about genuinely
  ambiguous ones, shows the `rule test` card for confirmation, then writes the very rule file it tested. It
  passes the rule and the examples on stdin through a quoted heredoc (no temp files, no inline quoting). Heredoc
  invocations are not pre-approved by `allowed-tools` (tested), and `new`/`edit` pre-approve no config-changing verb.
  `new` embeds a short how-to and three example rules, and points to `references/writing-rules.md` (read for any
  non-trivial rule) and `references/ast/index.md`; `edit` and `explain` point to the same two.
- `edit`, `mode` and `setup` change configuration only when you ask, always with `--as-user` and your own words in
  `--reason`. They never run `sudo`: a not-writable file prints the `sudo …` command for you to run.

`just test` runs the suite on the pinned library (`uv run --python 3.13 --with ast-grep-py==<pin>`). The tests that run the
real hook install the real runtime once into `~/.cache/guardrails-runtime-dev` (network once; they skip with a message
when that fails, `GUARDRAILS_REQUIRE_AST=1` makes that a failure, for CI); the bootstrap tests use a stub uv and stub
downloads and also run under macOS's Python 3.9 (`just test-39`). `just check` lints and type-checks (ruff and ty),
`just manifest` regenerates the runtime manifest, `just validate` runs `claude plugin validate`.

## Layout

| path | role |
|---|---|
| `lib/` | all Python: `guard.py` (entry point: hook without arguments, CLI with them), `engine.py` (the hook), `matching.py` (the one evaluation path), `rulebuilder.py` (typed ast-grep rules), `scanner.py` (units, wrapper variants, shell strings), `bounded.py` (the hard deadline), `verdict.py`, `policy.py`, `store.py`, `cli.py`, `render.py`, `wrappers.py`; `bootstrap.py` (the host-Python installer); data: `runtime-manifest.json`, `runtime-requirements.txt`, `runtime-id` |
| `hooks/` | `hooks.json` (PreToolUse on `Bash\|Monitor`, and a synchronous SessionStart that ensures the runtime) and the `guardrails.sh` POSIX wrapper |
| `scripts/` | `gen-runtime-manifest.py`: rewrites the manifest, requirements and runtime id for the pins |
| `references/` | text the skills read on demand: matching semantics, presentation conventions, config-change rules, the rule-writing guide (`writing-rules.md`) and the tested `ast/` cookbook |
| `bin/guardrails` | the CLI wrapper on the Bash tool's PATH (ensures the runtime first) |
| `presets/`, `skills/`, `tests/` | preset rule sets, the six skills, shared skill references, the unittest suite |
