# How guardrails matches a command

Everything here is what `lib/matching.py` (the one evaluation path the hook and every CLI command share),
`lib/astrules.py` (the rules handed to ast-grep), `lib/astworker.py` (the scan loop) and `lib/policy.py` do. Check a
claim with `guardrails rule test` before stating it, but know what it checks.

The ast-grep binary does all the lexing and parsing: guardrails writes no shell parser. `program`, `args`, `builtin` and
`match.ast` are all compiled to ast-grep rules and run in one scan over the tree-sitter Bash parse of the command as
written; `regex` reads the raw text in Python and needs no engine.

Matcher ladder, narrowest first: `program`, then `program` + `args`, then `builtin`, then an `ast` rule (a `pattern`,
plus `inside` / `has` when context matters), then `regex`. Before writing an `ast` rule with relations, run
`guardrails rule ast '<command>'` to see the node kinds.

## What `rule test` does and does not check

`rule test` answers one question per command: does the rule's matcher select it (`match`), not (`-`), or could it not
be judged (`cannot`: a rule that needs the engine while the engine is missing or failing, or a command the hook would
refuse). It does not apply modes, retry acknowledgements, `warn` versus `deny`, or hook-level switches. It prints
`note:` lines when the hook would not act on a match: rule disabled, `requires` binary missing, a listed mode that
suspends it (and whether that mode is active now), global hook or project rules disabled. Report those notes next to any
verdict; never call a matcher result "blocked" on its own, and never read `cannot` as "no match".

`rule test --render` prints the same verdicts as the markdown card described in `presentation.md`, with a `wrapped` tag
the engine computes: the command reached the rule only through a look-through (a wrapper, a shell string, a
substitution, a pipeline member), as opposed to being the command the rule names. A `regex` match reads the raw text, so
it is never tagged `wrapped`, and a regex hit wins over a look-through hit on the same command. A `cannot` row is listed
under **Not evaluated**. Paste the card; do not retell it.

## `program`, `args`, `builtin`

These compile to ast-grep rules over the parse tree. The matched node is a `command`; its name must be one of the
program names, written however the shell allows a plain name: `pkill`, `/usr/bin/pkill`, `'pkill'`, `"pkill"`, after
`FOO=1` assignments, in a list, loop, `if`, subshell or group, in a pipeline, in `$(...)` or backticks (also inside
double quotes), in `<(...)`.

Looked through, the wrapper branch: the command's name is a wrapper (`sudo doas env timeout nice nohup time command exec
builtin stdbuf setsid ionice xargs watch`, plus your own) and one of the wrapper command's own words equals the program.
`sudo -u bob pkill x`, `env A=1 pkill x`, `timeout 5 pkill x`, `xargs -I{} pkill {}`, `nice -n 5 pkill`. There is no table
of which flags take a value: any word of the wrapper command that is the program name counts, so `sudo grep pkill file`
and `command -v pkill` also match `pkill` (known false positives, see below).

Shell strings, the script branch: the script of `bash|sh|zsh|dash|ksh|script [flags] -c '<script>'` (also behind a
wrapper, and in a cluster such as `-lc`), the arguments of `eval`, and a heredoc or here-string fed to a shell
(`bash <<EOF`, `sh -s <<< 'cmd'`) are unquoted (one shell word; `$'...'` escapes decoded) and scanned as a unit of
their own, recursively; every hit in a unit counts as wrapped. The body of an unquoted heredoc that contains `$(` or a
backtick is scanned too, but only what lies inside those substitutions counts (the rest is data). Units are de-duplicated and bounded: depth 8, 64 distinct units, 256 KiB of
script text. A command that goes past a bound is denied unparsed with "command too complex to check".

Not commands, so never matched by `program`: heredoc bodies, redirect targets (`> pkill`), the arguments of other
commands (`echo pkill`, `man pkill`), quoted data.

Cannot be analysed statically, so not matched: obfuscated or dynamic command names. `$'p\x6bill'`, `p''kill`, `p\kill`,
a name held in a variable (`P=pkill; $P x`), an alias or a function. Also not looked through: `ssh host pkill x`,
`find . -exec pkill {} ;`, the contents of a script file (`bash script.sh`), `python -c '...'`, `echo pkill | sh` and
`cat <<EOF | sh`, `source <(echo pkill)`, `su -c`, `env -S '...'`, and `watch 'pkill x'` (only `bash -c`, `eval`,
`script -c` and shell-fed heredocs and here-strings are scanned). `find` and similar can be declared a wrapper with
`guardrails wrapper add`, by word and without arity. A substitution in an unquoted heredoc body that is itself inside
single quotes in that body is text to the parser. When a rule must not miss these, use `regex` and accept its false
positives.

### The fields

- `match.program`: one name or a list (no spaces or `/`); case-sensitive, `egrep` is not `grep`. Wrapper names are
  programs too: `program: sudo` matches `sudo ls`.
- `match.args`: a Rust regex (ast-grep's `regex`, so no look-around or back-references) over the matched command's whole
  text, name and wrapper words included, so anchor with `(^|\s)`. It narrows `program` or `builtin` (AND); on its own,
  or next to `regex` only, it has no effect and a rule whose `match` has none of `program`, `builtin`, `regex`, `ast`
  is rejected.
- `match.builtin`: `grep-recursive`: a command named grep, egrep or fgrep (behind a wrapper too) with a recursive flag
  among its words: `-r`, `-R`, a cluster such as `-rn` or `-nr` (not when an option that takes a value, `e f m A B C d D`, comes
  first: `-er` is pattern `r`), `--recursive`, `--dereference-recursive`, `--directories=recurse`, `-d recurse`,
  `--directories recurse`. Words after `--` are ignored. ANDed with `program` and `args`.
- `match.regex`: a Python regex (`re.search`) over the raw, whole command text, quotes, heredocs and pipelines included.
- `match.ast`: an ast-grep rule object over the parsed syntax tree; see below.
- The matchers are alternatives: the rule fires when the program/args/builtin part matches OR `regex` matches OR `ast`
  matches; `program` and `args` are ANDed with each other.
- `match.mentions` is gone. A state file that still has it loads; the key is ignored.

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
name is quoted or has a directory (`/usr/bin/pkill`, `'pkill'`), after up to three `VAR=x` assignments, and behind a
wrapper: `sudo -u bob /usr/bin/pkill -9 x`, `FOO=1 sudo pkill -9 x`. Behind a wrapper the match is by the name and the
literal words (in any order, as `program` + `args` would), not by position. Every other pattern (`curl $$$ | sh`,
`zap x > f`, `for ...; done`, `echo $(x)`) goes to ast-grep exactly as written. A `kind: command` rule whose `has` names
the command with `field: name` also gets the wrapper form; its name regex is otherwise taken as written. A pattern
that ends in ` $$$` also selects the command with no arguments (in tree-sitter-bash a trailing `$$$` hole alone never
matches zero arguments, so the engine widens it). A hole INSIDE a substitution, such as `kill $($$$)`, never matches
anything: express that with `inside` / `has`.

**Behind a wrapper.** Relations, `not`, `regex` and other `kind` members are never shifted; they keep judging the real
tree.

**Units and `wrapped`.** A hit counts as wrapped when it was reached through the wrapper branch, inside a shell string,
or when the matched node sits inside a pipeline or a substitution (`$(...)`, backticks, `<(...)`). A command in a list
(`;` `&&` `||`), a loop, an `if`, a subshell or a `{ ...; }` group is not wrapped. A direct hit beats a wrapped one on the
same rule.

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
| `ansi_c_string`, `concatenation` | `$'...'`; adjacent pieces such as `a"b"$c` |
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
data: the built-in list above, extended per layer with `guardrails wrapper add <name>` (`--scope global|project|managed`
and `--path`, like `rule`), `wrapper rm <name>`, `wrapper list`. Look-through only grows: a layer can add names and never
remove or change a higher layer's; invalid names are skipped and reported. A state file from an older version, whose
wrapper entries carry options such as `flagsWithValue` or `shellString`, loads and the options are ignored.

**False-positive and false-negative risks.** A wrapper the list does not know (`mywrap pkill x`) is an ordinary command,
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
on text that only mentions the command, including heredocs, and it is the only matcher that still runs when the engine
is unavailable.

### Idioms

- A command by name, however it is spelled, with or without arguments: `program: pkill`; in `ast`, `{"pattern": "pkill $$$"}`
  (as written, also behind a wrapper), or the explicit `{"kind": "command", "has": {"field": "name", "regex": "^pkill$"}}`.
- Only in a context: `{"pattern": "pgrep $$$", "inside": {"any": [{"kind": "command_substitution"}, {"kind": "pipeline"}], "stopBy": "end"}}`.
- Not at top level: `inside` with `stopBy: end` and the container kinds you care about.
- Unless guarded: `{"pattern": "npm publish $$$", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}}`.

### Requirements

The matcher runs the standalone `ast-grep` binary (pinned, Bash grammar built in) as a child of the hook, which is plain
stdlib Python 3.9 or newer; there is no venv, uv or pip. The binary comes from two places, tried in order: the npm platform
package that Claude Code's automatic `npm ci --ignore-scripts` puts in the plugin's `node_modules`, then a hash-checked
wheel that `guardrails engine install` (or the detached SessionStart warm-up, only when neither source works and an enabled
rule needs the parser) unpacks into `${CLAUDE_PLUGIN_DATA}/engine/<pin>/`. The PreToolUse hook only looks; it never downloads
or installs. A plugin loaded in place from a local-directory marketplace never gets the automatic npm install, so it uses
the wheel fallback or a manual `cd <plugin root> && npm ci --ignore-scripts`. A call whose rules are all `regex` never
touches the engine.

Platforms: macOS arm64 and x86_64, Linux glibc x86_64 and aarch64 (the wheel needs glibc 2.28). Linux musl and Windows are
unsupported and get the notice below with the reason.

Trust model: the binary must be a regular file inside the plugin root (npm) or the plugin data dir (wheel), executable, owned
by you or root, not writable by group or others, in directories not writable by others (its location comes from the plugin's
own place and data dir, never from PATH, the project or the cwd); an npm binary's package version must equal the pin and a
wheel binary must match its marker (pin, hashes, size, mtime, inode). It runs with `PATH=/usr/bin:/bin` as its only
environment variable, from `/`, with an explicit empty config so a repository's `sgconfig.yml` is never read. No
environment variable selects the binary, the URL or the root. The README has the details.

Troubleshooting: `guardrails engine status` says which source is active, why another was rejected, the last download failure
and its retry time, and prints the fix commands (`guardrails engine install`, or `cd <plugin root> && npm ci
--ignore-scripts`); `guardrails engine verify` re-hashes the active binary against the committed manifest.

### When the engine is unavailable or fails

There is no degraded parsing. Rules that need the parser (`program`, `args`, `builtin`, `match.ast`) cannot be
evaluated, so the hook allows the command and says so loudly; `regex` rules are still enforced and keep running.

- **Engine missing** (unsupported platform, not installed, or a binary that failed its checks): the first Bash or
  Monitor call of every session carries the `GUARDRAILS ENGINE MISSING` notice in both the user-facing `systemMessage`
  and the agent-facing `additionalContext`: which rules are not enforced, that `regex` rules still are, the reason, the
  fixes that can work on this system, and an instruction to tell the user first.
- **Managed rules fail open too**: a managed `program`/`args`/`builtin`/`ast` deny rule is not enforced while the engine
  is missing or failing, only `regex` rules are. The notice and `status --problems` name the managed rules affected; after
  the first notice the rest of the session runs with them unenforced.
- **Engine failure during a call** (crash, unreadable or invalid output, missing canary match, timeout on a command under
  8 KiB): that call is allowed with a warning that names the failure class, in both channels; the same class is warned
  again at most every 10 minutes while it lasts, and `status` / `rule test` show the last failure.
- **A timeout on a command over 8 KiB is a denial** ("command too complex to check"): dense nesting (`$(` x thousands,
  `sudo ` chains) makes ast-grep quadratic, so the content is the cause and failing open would be a bypass. A hit found
  at a completed level (the plain top level before a slow `bash -c` string) stands.
- **A rule that does not compile** is skipped and named once per session; the others run.
- **A command over 256 KiB, or one that nests shell strings more than 8 deep, unpacks into more than 64 distinct
  strings or more than 256 KiB of script text, is denied unparsed** ("command too large to check", "command too complex to
  check"): padding must never be a way past a rule. Only a deny rule that could not be judged causes the denial (warn-only
  parse rules: allowed with a warning); a rule set with only `regex` rules never starts the engine.
- The whole hook runs under a 7 s watchdog (hook timeout 10 s); on expiry or any other exception it applies the `regex`
  rules, allows the rest with a visible warning, and never exits silently.

`rule test` and `status` say the same: a rule that needs the engine is reported as `cannot` evaluate, and
`status --problems` lists the rules that are not enforced and why.

Bash and Monitor: the hook matches `Bash|Monitor`. A rule for `Bash` applies to a Monitor command too (a rule with
`"tool": "Monitor"` applies to Monitor only), retry acknowledgements and warn-once work the same, and a Monitor call
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
- Several managed files stack: platform default first, then the `GUARDRAILS_MANAGED_PATH` file, then a `--path` file
  (status and `rule test` only); later ones can only tighten. For a mode an earlier file declares, a later file's
  `active` is ignored, it cannot add suspending `modes` to an earlier rule, and it cannot make a mode the earlier file
  left undeclared suspend that rule.
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
8. The rule is invalid (bad regex, unknown builtin, an `ast` rule that does not compile): a global or project rule is
   skipped and only `status` reports it (a non-compiling rule also warns once per session); an invalid managed
   rule is skipped with a warning.
9. The rule uses `program`, `args`, `builtin` or `match.ast` and the engine is missing or failed on this call: the
   command was allowed and the session got a notice.
10. The command is one of the documented limits: an obfuscated or dynamic name, a wrapper the list does not know, a script
    file, a heredoc substitution the parser leaves as text.
11. The rule's `tool` is not `Bash`.
12. A lower layer cannot loosen a higher one: a global or project entry with `enabled: false` or `action: warn` over a
    managed or global rule has no effect, so a rule that "should have been turned off" may still be enforced.

Hook failures never pass silently: if the hook itself errors, the `regex` rules still apply and the user is told.
