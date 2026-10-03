# How guardrails matches a command

Everything here is what `lib/matching.py` (the one evaluation path the hook and every CLI command share),
`lib/rulebuilder.py` (the typed ast-grep rules built from a guardrails rule), `lib/scanner.py` (parse units, wrapper
variants, shell strings), `lib/bounded.py` (the hard deadline) and `lib/policy.py` do. Check a claim with `guardrails rule
test` before stating it, but know what it checks.

ast-grep does all the lexing and parsing, in process through the `ast-grep-py` library: guardrails writes no shell parser.
`program`, `args`, `regex` and `match.ast` become typed ast-grep rules and are matched on the tree-sitter Bash parse of the
command as written, of its wrapper variants and of each shell string; `regex` reads the raw text of the whole command with ast-grep's own regex engine, inside the same bounded checker.

Matcher ladder, narrowest first: `program`, then `program` + `args`, then an `ast` rule (a `pattern`,
plus `inside` / `has` when context matters), then `regex`. Before writing an `ast` rule with relations, run
`guardrails rule ast '<command>'` to see the node kinds.

## What `rule test` does and does not check

`rule test` answers one question per command: does the rule's matcher select it (a `Block` or `Warn` row), not (an
`Allow` row), or could it not be judged (a `Not evaluated` row: a rule that needs the engine while the engine is missing
or failing, or a command the hook would refuse). It does not apply modes, retry acknowledgements, `warn` versus `deny`,
or hook-level switches. It prints a `**Note**` line when the hook would not act on a match: rule disabled, `requires` binary missing, a listed mode that
suspends it (and whether that mode is active now), global hook or project rules disabled. Report those notes next to any
verdict; never call a matcher result "blocked" on its own, and never read a `Not evaluated` row as "no match".

`rule test` prints the same verdicts as the markdown card described in `presentation.md`, with a `wrapped` tag
the engine computes: the command reached the rule only through a look-through (a wrapper, a shell string, a
substitution, a pipeline member), as opposed to being the command the rule names. A `regex` match reads the raw text, so
it is never tagged `wrapped`, and a regex hit wins over a look-through hit on the same command. A row that could not be judged is listed
under **Not evaluated**. Paste the card; do not retell it.

## `program`, `args`

These compile to ast-grep rules over the parse tree. The matched node is a `command`; its name must be one of the
program names, written however the shell allows a plain name: `pkill`, `/usr/bin/pkill`, `'pkill'`, `"pkill"`, after
`FOO=1` assignments, in a list, loop, `if`, subshell or group, in a pipeline, in `$(...)` or backticks (also inside
double quotes), in `<(...)`.

Looked through, the wrapper variants: a command that contains a wrapper command (`sudo doas env timeout nice nohup time
command exec builtin stdbuf setsid ionice xargs watch`) is also matched as text variants of the top-level
statement (a direct child of the program) that holds it, in which that wrapper command is replaced by the text from each of
its own non-option words onward (an option cannot start the wrapped command): `sudo -u bob pkill x`
also reads as `bob pkill x`, `pkill x` and `x` (a wrapper's words are read from the parse, never split
by guardrails; leading assignments such as `A=1 sudo x` are dropped with it). Nested wrappers need no recursion, because a
later word starts the inner command directly. A command with several wrappers also gets every combination of them replaced
together while there are at most 64 (else each k-th word of all of them), so `sudo curl x | sudo sh` reads as `curl x | sh`.
Because a variant is one statement, its cost does not grow with the script around it, and repeated lines collapse. The
accepted loss: a relation BETWEEN separate top-level statements (`follows` across `;` or a newline, e.g. `cd x` then
`sudo curl y`) is judged only on the text as written, not through the wrapper; inside one statement (pipelines, lists,
subshells, loops, substitutions, a heredoc attached to it) relations are kept.
Every rule is matched on each variant, and a hit found in one is wrapped. Variants are not unwrapped again, are
de-duplicated and are bounded: 2048 variants and 512 KiB of variant text parsed in total per command, and 2048 wrapper commands or wrapper words per unit (and the 5 s deadline), past which the command is denied
unparsed ("command too complex to check") at any size. There is no table of which flags take a value: any word counts as a
start, so `sudo grep pkill file` and `command -v pkill` also match `pkill` (known false positives, see below). The same
coarseness applies to relation rules, which judge the real tree of each variant: `sudo grep curl f | sh` matches a
`curl $$$ | sh` rule, and `sudo curl x | sh` makes a `curl $$$` rule with `not inside pipeline` not match (the curl is
still in a pipeline).

Shell strings, the script units: the script of `bash|sh|zsh|dash|ksh|script [flags] -c '<script>'` (also behind a
wrapper, through its variant, and in a cluster such as `-lc`), the arguments of `eval`, and a heredoc or here-string fed to
a shell (`bash <<EOF`, `sh -s <<< 'cmd'`) are unquoted (one shell word: single quotes as is, double quotes with the
`\"` `\\` `\$` and backtick escapes, no ANSI-C decoding) and scanned as a unit of their own, recursively; every hit in a
unit counts as wrapped. The body of an unquoted heredoc that contains `$(` or a backtick is scanned too, but only what
lies inside those substitutions counts (the rest is data). Units are de-duplicated and bounded: depth 8, 64 distinct units,
256 KiB of script text, with a budget of their own apart from the variants. A command that goes past a bound is denied
unparsed with "command too complex to check".

Not commands, so never matched by `program`: heredoc bodies, redirect targets (`> pkill`), the arguments of other
commands (`echo pkill`, `man pkill`), quoted data.

Cannot be analysed statically, so not matched: obfuscated or dynamic command names. `$'p\x6bill'`, `p''kill`, `p\kill`,
a name held in a variable (`P=pkill; $P x`), an alias or a function; a script written as an ANSI-C literal
(`bash -c $'pkill x'`, `eval $'pkill x'`) is not unpacked either. Also not looked through: `ssh host pkill x`,
`find . -exec pkill {} ;`, the contents of a script file (`bash script.sh`), `python -c '...'`, `echo pkill | sh` and
`cat <<EOF | sh`, `source <(echo pkill)`, `su -c`, `env -S '...'`, and `watch 'pkill x'` (only `bash -c`, `eval`,
`script -c` and shell-fed heredocs and here-strings are scanned). A substitution in an unquoted heredoc body that is itself inside
single quotes in that body is text to the parser. When a rule must not miss these, use `regex` and accept its false
positives.

### The fields

- `match.program`: one name or a list (no spaces or `/`); case-sensitive, `egrep` is not `grep`. Wrapper names are
  programs too: `program: sudo` matches `sudo ls`.
- `match.args`: a Rust regex (ast-grep's `regex`, so no look-around or back-references) over the matched command's whole
  text, name included (for a hit behind a wrapper: the command as the variant reads it, without the wrapper's words), so
  anchor with `(^|\s)`. It narrows `program` (AND); on its own,
  or next to `regex` only, it has no effect and a rule whose `match` has none of `program`, `regex`, `ast` is rejected.
- `match.regex`: a Rust regex (ast-grep's engine: linear time, no backreferences or look-around; `rule add/set/test` reject
  those with exit 2, and a state rule that does not compile is skipped and named) searched in the command's whole text, quotes,
  heredocs and pipelines included. The text is the parse root's: it starts at the first token (leading blanks are not part of
  it) and `$` matches only at its very end, not before a trailing newline.
- `match.ast`: an ast-grep rule object over the parsed syntax tree; see below.
- The matchers are alternatives: the rule fires when the program/args part matches OR `regex` matches OR `ast`
  matches; `program` and `args` are ANDed with each other.

## `match.ast`: the syntax tree matcher

`match.ast` is a rule in ast-grep's vocabulary, evaluated on the tree-sitter Bash parse of the command as written, and
again on the parse of each shell string. It sees structure that `program`/`args` cannot (nesting in a substitution, a
pipeline, a loop, an `if`) and ignores text that `regex` cannot tell apart from code (heredoc bodies, single-quoted
strings). Because every rule runs on the real tree of the real text, negated relations (`not inside`, `not follows`,
`not precedes`) say what they mean.

**How it combines.** The keys of one object are ANDed, as in ast-grep. A rule whose `match` has only `ast` is valid.

**Supported keys** (anything else is rejected when the rule is added or tested, exit 2):

- `pattern`: a code snippet with metavariables, `$A` for one node and `$$$` for any number; or
  `{"context": "...", "selector": "command"}` to select a node kind inside a larger snippet.
- `kind`: a node kind such as `command` or `pipeline` (table below). `regex`: a Rust regex over the node's source text.
- Relations, each an object that is itself a rule: `inside` (an ancestor matches), `has` (a descendant matches),
  `follows` and `precedes` (a sibling before or after). They accept `stopBy`: `"neighbor"` (the default: only the
  nearest level), `"end"` (all the way to the root or the leaf), or a rule to stop at; and `field`, to restrict to a
  named child field.
- Composition: `not` (a rule), `any` and `all` (lists of rules).

A rule is compiled when it is added, tested, listed by `status` and run by the hook. The CLI rejects one that does not
compile (exit 2); the hook skips it and warns once per session.

**Single-command patterns are spelling-tolerant.** A `pattern` that is exactly one simple command with arguments
(`pkill -9 $$$`, `git push -f $$$`; no pipe, list, redirect, loop or substitution in it) also matches the command when its
name is quoted or has a directory (`/usr/bin/pkill`, `'pkill'`) and after up to three `VAR=x` assignments. Every other
pattern (`curl $$$ | sh`, `zap x > f`, `for ...; done`, `echo $(x)`) goes to ast-grep exactly as written. A pattern that ends
in ` $$$` also selects the command with no arguments (in tree-sitter-bash a trailing `$$$` hole alone never matches zero
arguments, so guardrails widens it). A hole INSIDE a substitution, such as `kill $($$$)`, never matches anything: express
that with `inside` / `has`.

**Behind a wrapper.** Every pattern, relation, `not`, `regex` and `kind` is matched on the whole statement with the wrapper
replaced, so pipeline and list patterns (`curl $$$ | sh`, `cd $A && rm $$$`) and negations (`not inside`, `not follows`)
work through `sudo`, `env`, `xargs` and the rest; see the variants above.

**Units and `wrapped`.** A hit counts as wrapped when it was found in a wrapper variant, inside a shell string,
or when the matched node sits inside a pipeline or a substitution (`$(...)`, backticks, `<(...)`). A command in a list
(`;` `&&` `||`), a loop, an `if`, a subshell or a `{ ...; }` group is not wrapped. A direct hit beats a wrapped one on the
same rule (a variant hit never downgrades a direct one).

### Tree-sitter Bash node kinds that matter

Verified with `guardrails rule ast '<command>'`:

| kind | what it is |
|---|---|
| `program` | the root |
| `command` | one simple command; children: `variable_assignment` prefixes, `command_name`, then the arguments |
| `command_name` | the name word; select by name with `{"kind": "command", "has": {"field": "name", "regex": "(^\|/)pkill$"}}` |
| `word`, `number` | an unquoted argument or flag (`-0` is a `number`) |
| `string` (child `string_content`) | a double-quoted argument; `$(...)` inside it runs |
| `raw_string` | a single-quoted argument: data, except as the `-c` string of a shell |
| `ansi_c_string`, `concatenation` | `$'...'` (data, never unpacked as a script); adjacent pieces such as `a"b"$c` |
| `command_substitution` | `$(...)` and backticks; the body is code |
| `process_substitution` | `<(...)` and `>(...)` |
| `simple_expansion`, `expansion`, `arithmetic_expansion` | `$x`, `${x}`, `$((1+2))` |
| `pipeline` | `a \| b` and `a \|& b`; the members are direct children |
| `list` | `a && b`, `a \|\| b` (left-nested); `;` and newlines only make sibling nodes under `program` |
| `redirected_statement` | a command or compound with redirects (`file_redirect`, `heredoc_redirect`); a `<<<` is a `herestring_redirect` child of the command |
| `heredoc_body` (with `heredoc_start`, `heredoc_end`) | heredoc text: data, never a command |
| `subshell`, `compound_statement` | `( ... )` and `{ ...; }` |
| `if_statement` (`elif_clause`, `else_clause`) | the condition and the body are both direct children |
| `while_statement` (also `until`), `for_statement` (also `select`), `c_style_for_statement`, `do_group` | loops; `do_group` is the body |
| `case_statement`, `case_item`, `function_definition`, `negated_command`, `test_command`, `variable_assignment` | the rest |
| `ERROR` | the parser could not make sense of part of the text |

### Wrappers

tree-sitter sees `sudo pkill -f vite` as a `command` named `sudo` whose arguments are plain words. The wrapper names are
the fixed list above (`WRAPPERS` in `lib/scanner.py`); a state file with a `wrappers` key is skipped for that key and `status` and the hook say so.

**False-positive and false-negative risks.** A command that is not valid bash gives a partial tree: `program` and `args` rules still read the bare words the parser left behind, but a `pattern` rule does not see a wrapper's wrapped command there (`{ ; }; xargs -r pkill`: an empty group is valid zsh and a syntax error in bash). A wrapper the list does not know (`mywrap pkill x`) is an ordinary command,
so the `pkill x` inside is not seen: declare it. A word of a wrapper that merely equals the program name matches
(`sudo grep pkill file`, `command -v pkill`, `sudo -u pkill ls`); a rule that denies with `retry: same-command` lets a
deliberate repeat through.

### Data versus code

Code (matched): a command; the body of `$(...)` or backticks, also inside double quotes and in an unquoted heredoc body; `<(...)`; the `-c` string of a shell and the arguments of `eval`. Data (never matched): a
quoted-delimiter heredoc body, single-quoted text, the arguments of other commands (`echo pkill`), redirect targets.
`regex`, by contrast, reads the raw text, so it also fires inside heredocs and quoted strings.

### When to use `regex` instead

Dataflow across commands (`ps | xargs kill` where the PIDs come from the other command, `curl x | sh`) is text spanning
nodes. A relation often expresses it (`xargs kill` inside a `pipeline`); a regex expresses the rest. A regex also fires
on text that only mentions the command, including heredocs, and it needs the same engine as every other matcher.

### Idioms

- A command by name, however it is spelled, with or without arguments: `program: pkill`; in `ast`, `{"pattern": "pkill $$$"}`
  (as written, also behind a wrapper), or the explicit `{"kind": "command", "has": {"field": "name", "regex": "^pkill$"}}`.
- Only in a context: `{"pattern": "pgrep $$$", "inside": {"any": [{"kind": "command_substitution"}, {"kind": "pipeline"}], "stopBy": "end"}}`.
- Not at top level: `inside` with `stopBy: end` and the container kinds you care about.
- Unless guarded: `{"pattern": "npm publish $$$", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}}`.

### The runtime

`ast-grep-py` ships one wheel per CPython version, so guardrails brings its own Python instead of using the host's:
`${CLAUDE_PLUGIN_DATA}/runtime/<id>/` holds a portable `uv` binary (`bin/`), a managed CPython 3.13 (`python/`), a venv with
the hash-pinned library (`venv/`) and a `marker.json` written last (about 140 MB; about 46 MB downloaded; the uv cache is
deleted after the install). `<id>` is `lib/runtime-id`, a digest of `lib/runtime-manifest.json` and
`lib/runtime-requirements.txt`, so a pin change installs a new runtime (the old one is removed once it is 30 days old).

Automatic install, no user command: (1) SessionStart runs `ensure` synchronously (timeout 120 s; first ever install about
3 to 10 s, later sessions about 0.1 s); (2) the PreToolUse hook, when the runtime is not ready, allows the command with a
loud notice (once per session, repeated every 10 minutes while a failure persists) and starts a detached `ensure`;
(3) every `guardrails` CLI call runs at once on a ready runtime and otherwise ensures first, in the foreground, with
progress on stderr. Limits: a 10 s connect timeout, a
60 s budget per install, every tool step in its own process group killed at the deadline. A failed attempt is stamped with a
class and a step, and the next attempt is 10 minutes later, then 1 hour, then 6 hours (reset by a success); while a stamp is
fresh SessionStart and the hot path do not try, they say when the next attempt is. `guardrails engine ensure --retry-now`
ignores the wait. Raw failure detail goes to `runtime/install.log`; notices carry only the class (dns, connect, timeout,
tls, http, hash, disk, tool, crash).

**What is enforced while the runtime is not ready: nothing.** `regex` rules included, because every rule runs on the managed
Python. The window is the first seconds of the first session, or until an install works after a failure. Managed rules fail
open in it too. The notice says so.

The data dir: the runtime is found only through `CLAUDE_PLUGIN_DATA` (default `~/.claude/plugins/data/<plugin>-<marketplace>`),
which must be absolute and canonical (no `..`, no symlink component), owned by you, not writable by group or others. If not,
the notice says "the plugin data directory is not a safe absolute path (<fixed reason>)", nothing is executed or written there,
and the notice repeats on every call. No notice, hook answer or `engine status` text ever contains a path or any other text
taken from the environment or the repository: the paths of the runtime and of the install log go to stderr of
`guardrails engine status` only. Residual, out of scope by decision: a `CLAUDE_PLUGIN_DATA` that the repository controls and
that points at a planted runtime passes these checks, because the variable is provided by the harness. The project directory and the cwd never decide (a session started in `$HOME` or `~/.claude` enforces
normally).

Only `lib/bootstrap.py` and `lib/hostcli.py` run on the host's Python (3.9 or newer, standard library only). The bootstrap
downloads the pinned `uv` wheel from the one `files.pythonhosted.org` URL in the manifest (sha256 checked before the file is
read, exactly one member unpacked), then runs that uv with a scrubbed environment (HOME, LANG, TMPDIR, proxy and `SSL_CERT_*`
variables; `PATH=/usr/bin:/bin`; `UV_*` pointing inside the runtime dir; `--no-config`; the runtime dir as cwd): `uv python
install 3.13`, `uv venv`, `uv pip install --require-hashes --only-binary :all: --no-deps`. A repository's `uv.toml`, `UV_*`,
`PATH`, `PYTHONPATH` or `pip.conf` never influence what runs. A self-test must import the library and match a pipeline before
the marker is written. Installs run under a `flock` (released by the OS if the installer dies); an install builds in a fresh
directory next to `runtime/<id>` (a symlink to the current build), self-tests it, points the link at it with one atomic
rename and only then removes the old build, so a failed or interrupted install never touches the runtime in use. Cleanup
runs only inside an install, under the lock: builds nobody points at, and runtimes of other
pins once they are 30 days old (two plugin versions sharing a data dir never delete each other's runtime; nothing is deleted
on the hot path).

How uv verifies the Python: `uv python install` fetches a python-build-standalone distribution (primarily from
`releases.astral.sh`, falling back to the GitHub releases of `astral-sh/python-build-standalone`, served from
`release-assets.githubusercontent.com`) and compares its sha256 with the one compiled into the uv binary; a mismatch aborts
(`Hash mismatch for cpython-...`, verified with a tampered archive on a local mirror). So the manifest's sha256 pins uv, uv
pins Python, and `--require-hashes` pins the library. Network hosts: `pypi.org`, `files.pythonhosted.org`,
`releases.astral.sh`, and `github.com` plus `release-assets.githubusercontent.com` as the fallback (all observed through a
logging proxy; `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and `SSL_CERT_FILE/DIR` are passed through, mirror and index variables
are not).

Platforms: macOS arm64 and x86_64, Linux glibc 2.28+ x86_64 and aarch64. Linux musl, older glibc, other architectures and
Windows get a precise "unsupported platform" notice and no install attempt.

Trust model: the wrapper runs the runtime's Python only when the data dir passes the checks above, `marker.json` exists, no
`broken` file is next to it and the interpreter is owned by you. Each `ensure` (every session start) also checks that the
interpreter and the extension module are owned by you, not writable by group or others, unchanged in size and mtime since the
install, and inside the runtime dir; any failure reinstalls. The hook runs with `python -I`. The wrapper picks the host Python
from fixed absolute locations first, then `PATH` entries that are absolute, outside the project and cwd and not world-writable;
each candidate is smoke-tested (`python3 -I -S -c` on the version), and a broken one is reported (its path on stderr, never in the notice), never skipped
silently. A hook Python that dies is reported ("failed to run (exit N)").

Troubleshooting: `guardrails engine status` needs no runtime and works offline: whether the runtime is ready (or why not), the
pins, the platform, the path, the installed Python, whether an install is running, and the last failure (class, step, count in
a row, next automatic attempt, the log file). Kill switches, for the user: `claude plugin disable guardrails@<marketplace>`,
or `guardrails disable` from a terminal (global hook off; managed rules stay).

### When the engine is unavailable or fails

There is no degraded parsing, and no rule runs outside the engine. If the runtime is not ready (above) or the library cannot be
imported or fails its self-test, rules cannot be evaluated, so the hook allows the command and says so loudly.

- **Engine failure** (import error, failed self-test, an unexpected error in the checker): that call is allowed with a
  warning that names the reason, in both channels; the same class is warned again at most every 10 minutes while it lasts,
  and `status` / `rule test` show the last failure. Managed deny rules fail open too; the warning and `status --problems`
  name them.
- **A crash of the library**: the checker runs a health probe on a trivial command in a fresh child. Probe passes: that
  command crashes the parser and is denied alone ("this command crashes the parser"). Probe crashes or answers wrongly: the
  library is broken, the command is allowed with a loud notice, the runtime is marked broken and rebuilt in the background
  (same backoff as an install; the old build stays in place until the new one is swapped in), and the notice repeats every 10
  minutes until it is healthy. Probe does not answer in 3 s: it is retried once with a longer deadline (both bounded by the
  hook's 10 s budget minus 2 s headroom); still silent means the library is unverified, not broken: the command is allowed
  with "could not verify the matcher (timed out)", the runtime is left alone and no rebuild is started. A checker that cannot even be started (fork
  failure) is a loud allow that leaves the runtime alone.
- **A command the parser does not finish in 5 s is a denial** ("command too complex to check"): anything that needs the
  parser, `regex` rules included, runs in a forked child killed at that deadline, because a native call into ast-grep holds
  the GIL (no signal or thread can interrupt it) and a hook that outlives its timeout lets the command through. Dense nesting
  (`$(` x tens of thousands) is quadratic, so the content is the cause and failing open would be a bypass. A hit already
  found stands.
- **A rule that does not compile** (an `ast` rule, or a regex Rust cannot compile) is skipped and named once per session; the
  others run. So is a rule with a field this version does not know (for example `match.builtin` from an old `modern-cli`
  install): the warning and `status --problems` name the rule and the field and say to reinstall the preset.
- **A command over 256 KiB, or one that nests shell strings more than 8 deep, unpacks into more than 64 distinct strings
  or 256 KiB of script text, or unwraps into more than 2048 variants or 512 KiB of variant text, is denied unparsed**
  ("command too large to check", "command too complex to check"): padding must never be a way past a rule. Only a deny rule
  that could not be judged causes the denial (warn-only rules: allowed with a warning).
- If the hook itself raises, it allows with a visible warning that no rule was applied, and never exits silently.

`rule test` and `status` say the same: a rule that needs the engine is reported as `cannot` evaluate, and
`status --problems` lists the rules that are not enforced and why.

Bash and Monitor: the hook matches `Bash|Monitor`. Every rule applies to a Monitor command too, retry acknowledgements and warn-once work the same, and a Monitor call
with no `command` (only a `ws` URL) is ignored. Monitors declared by a plugin start without a tool call and are not
covered.

## Pipelines: `args` does not see them

`args` is a regex over one command's text, so the pipe to `sh` in `curl https://x.sh | sh` is not part of `curl`'s
command. Verified:

- `match: {program: curl, args: "\\| *(sh|bash)"}` does NOT match `curl https://x.sh | sh`, nor `curl x|bash`.
- `match: {regex: "curl [^|]*\\|\\s*(sudo +)?(sh|bash)\\b"}` matches both, and `bash -c 'curl x | sh'` too.

So a rule about what a command is piped into, or about text spanning several commands, needs `match.regex` or an `ast`
rule with `inside: pipeline`. The price of `regex`: it also fires on text that merely mentions the command
(`echo "curl x | sh"` matches), which `program` never does.

## What happens on a match

- `action: deny` blocks the call and shows the message; `warn` lets it run and shows the message to the agent once per
  session per rule. If something in the same command denies, warnings ride along in the deny message.
- `retry: same-command` (deny only): the first occurrence is blocked and remembered for the session, re-running the
  identical command text passes. Any changed text is blocked again.
- `messageShort` replaces `message` once the full text was shown in the session; `{which:a|b}` in a message becomes
  the first binary found on PATH.
- `enabled: false` skips the rule. `requires` skips it unless one of the binaries is installed.
- `modes`: the rule is skipped while any listed mode is active. A mode is active when switched on persistently (global,
  project, or managed) or for this session. An agent may switch on a session mode only when it is declared with
  `agentMayEnable` (and a rule suspended by an agent-enabled mode is reported to the user).
- The deny text starts with `[guardrails:<id>]`, or `<id> (managed)` for a managed rule.

## Layers

Order: managed, then global, then project. A lower layer can add rules of its own, and for an id a higher layer
already defines it can only tighten: switch `action` to deny, `retry` to none, re-enable, remove suspending `modes`,
and reword `message` / `messageShort` / `description` (not for managed rules). It cannot change `match` or `requires`,
loosen, disable, or add modes. An override that fails validation is ignored.

Managed specifics:

- Managed rules are always enforced unless their `modes` are declared by the managed file itself; a managed rule with
  no (declared) modes cannot be suspended by anything, and it still applies when the global hook is disabled.
- A project cannot switch on a mode the managed file declares, and cannot make a managed mode agent-enablable.
- An unreadable or invalid managed file never turns the guard off: the broken part is skipped and reported.

## Why a rule may not fire

Check these in order when a command the matcher selects still runs:

1. The global hook is disabled (`guardrails disable`): global and project rules are off, managed rules still apply.
2. Project rules are disabled in the project state: project entries are dropped.
3. The rule is disabled (`enabled: false`).
4. `requires` lists binaries and none is installed.
5. A listed mode is active (switched on persistently, or for this session), so the rule is suspended.
6. `retry: same-command` and the identical command was already blocked once this session.
7. `action: warn`: the command runs, the agent only gets the message.
8. The rule is invalid (an unknown `match` key, a regex or an `ast` rule that does not compile): a global or project rule is
   skipped and only `status` reports it (a non-compiling rule also warns once per session); an invalid managed
   rule is skipped with a warning.
9. The runtime was not installed yet (the first seconds of the first session, or after a failed install), or the rule uses
   `program`, `args`, `regex` or `match.ast` and the engine failed on this call: the command was allowed and the session
   got a notice.
10. The command is one of the documented limits: an obfuscated or dynamic name, a wrapper the list does not know, a script
    file, a heredoc substitution the parser leaves as text.
11. A lower layer cannot loosen a higher one: a global or project entry with `enabled: false` or `action: warn` over a
    managed or global rule has no effect, so a rule that "should have been turned off" may still be enforced.

Hook failures never pass silently: if the hook itself errors, the `regex` rules still apply and the user is told.
