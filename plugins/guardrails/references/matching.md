# How guardrails matches a command

Everything here is what `lib/matching.py` (the one evaluation path the hook and every CLI command share),
`lib/policy.py`, `lib/shellwords.py` (the plain lexer) and `lib/astworker.py` (the syntax-tree matcher) do. Check a
claim with `guardrails rule test` before stating it, but know what it checks.

Matcher ladder, narrowest first: `program`, then `program` + `args`, then `builtin`, then an `ast` rule (a `pattern`,
plus `inside` / `has` when context matters), then `regex`. Before writing an `ast` rule with relations, run
`guardrails rule ast '<command>'` to see the node kinds.

## What `rule test` does and does not check

`rule test` answers one question per command: does the rule's matcher select it (`match`) or not (`-`). It does not
apply modes, retry acknowledgements, `warn` versus `deny`, or hook-level switches. It prints `note:` lines when the
hook would not act on a match: rule disabled, `requires` binary missing, a listed mode that suspends it (and whether
that mode is active now), global hook or project rules disabled. Report those notes next to any verdict; never call a
matcher result "blocked" on its own.

`rule test --render` prints the same verdicts as the markdown card described in `presentation.md`, with a `wrapped` tag
the engine computes: the command reached the rule only through a look-through (a wrapper, a shell string, a
substitution, a pipeline member), as opposed to being the command the rule names. A `regex` match reads the raw text, so
it is never tagged `wrapped`, and a regex hit wins over a look-through hit on the same command. Paste the card; do not
retell it.

## What the plain matchers see (`program`, `args`, `builtin`)

The command line is split into simple commands at `;` `&&` `||` `|` `&` and newlines. Each simple command is a
program name plus its own arguments. A rule is checked against every one of them, and the rule fires if any one matches.

Looked through, so the real command is what gets matched:

- wrappers `sudo doas env command builtin exec nohup setsid stdbuf time timeout xargs nice ionice` (their own options
  and a timeout duration are skipped): `sudo pkill x`, `timeout 5 pkill x`, `xargs pkill`
- shell strings `bash sh zsh dash ksh eval watch script`: the first non-option argument is parsed again as a command
  line, to a depth of three: `bash -c 'killall Safari'`, `sh -c 'a; pkill x'`
- command substitutions `$(…)` and backticks, except inside single quotes
- leading `VAR=value` assignments, and a directory in front of the name (`/usr/bin/pkill` is `pkill`)

Not commands, so never matched by `program`: heredoc bodies, redirect targets (`> pkill`), the operand of
`command -v`, and the arguments of other commands (`echo pkill`, `man pkill`).

Not looked through, so invisible to `program`: `ssh host pkill x`, `find . -exec pkill {} ;`, the contents of a script
(`bash script.sh`), `python -c '…'`, `bash <<EOF … EOF` (the heredoc body is dropped).

`program` names the command the shell would run after looking through wrappers and shells. So the wrappers and shells
themselves can never be matched by `program`: `program: ["sudo", "env", "xargs", "bash"]` matches none of `sudo ls`,
`env ls`, `xargs ls`, `bash -c "ls"`. `bash script.sh` is parsed as program `script.sh` (so `program: script.sh`
matches it, as it does `sh ./script.sh` and `./script.sh`), and `bash` is not seen. To block `sudo`, `bash`, or a
shell invocation, use `match.regex` instead (for example `(^|[;&|]\s*)sudo\b` for a leading `sudo`); a regex is raw text
matching, so it also has the false positives described below.

## The fields

- `match.program`: one name or a list; equal to the command's name, exactly, case-sensitive. `egrep` is not `grep`.
- `match.args`: a regex (`re.search`) over that one command's own arguments joined by single spaces. Wrapper options
  are not part of them. It only narrows `program` or `builtin` (AND); on its own, or next to `regex` only, it has no
  effect and a rule whose `match` has none of `program`, `builtin`, `regex` is rejected.
- `match.builtin`: `grep-recursive` (grep, egrep, fgrep with `-r`, `-R`, `--recursive`, `-d recurse`); ANDed with
  `program` and `args`.
- `match.regex`: a regex (`re.search`) over the raw, whole command text, quotes, heredocs and pipelines included. It is
  an alternative: the rule fires when the program/args/builtin part matches OR the regex matches.
- `match.ast`: an ast-grep rule object over the parsed syntax tree; see the next sections. Like `regex` it is an
  alternative: the rule fires when the program/args/builtin part matches OR `regex` matches OR `ast` matches.
- `match.mentions`: optional list of command names, only used when the AST matcher is unavailable (see degraded mode).
- A command with unbalanced quotes cannot be split. The fallback then scans the raw text for the program name (and
  `args` against the raw text); `builtin` never matches there; `regex` works as usual.

## `match.ast`: the syntax tree matcher

`match.ast` is a rule in ast-grep's vocabulary, evaluated on the tree-sitter Bash parse of the command. It sees
structure that `program`/`args` cannot (nesting in a substitution, a pipeline, a loop, an `if`) and ignores text that
`regex` cannot tell apart from code (heredoc bodies, single-quoted strings).

**How it combines.** The matchers are alternatives: the rule fires when the `program`/`args`/`builtin` part matches, or
`regex` matches, or `ast` matches. Inside `ast`, the keys of one object are ANDed, as in ast-grep. A rule whose `match`
has only `ast` is valid. `args` without `program`/`builtin` still has no effect.

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

**Units.** The rule runs on the command as written and on every unit exposed by looking through wrappers (next
section); a hit anywhere counts. `rule test --render` tags a hit `wrapped` when it was found in such a unit, or when the
matched node sits inside a pipeline or a substitution (`$(...)`, backticks, `<(...)`): the same meaning as for
`program`. A command in a list (`;` `&&` `||`), a loop, an `if`, a subshell or a `{ ...; }` group is not `wrapped`. A
direct hit beats a wrapped one on the same rule.

**Names are normalised first.** `FOO=1 pkill x`, `/usr/bin/pkill x`, `"pkill" x` and `\pkill x` are matched as
`pkill x` (and count as direct). A pattern that ends in ` $$$` also selects the command with no arguments (in
tree-sitter-bash a trailing `$$$` hole alone never matches zero arguments, so the engine widens it). A hole INSIDE a
substitution, such as `kill $($$$)`, never matches anything: express that with `inside` / `has`.

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

### Wrappers: what is looked through

tree-sitter sees `sudo pkill -f vite` as a `command` named `sudo` whose arguments are plain words, and
`bash -c 'pkill x'` as a command with a `raw_string` argument. So for every command whose name is a known wrapper the
engine rewrites the source and parses the result as another unit. For a wrapper it drops the wrapper's own words (its
flags, the values of flags that take one, a `timeout` duration, `env` assignments) and keeps everything around it, so
relations such as `inside` still see the real surroundings. For a shell (`bash|sh|zsh|dash|ksh -c '...'`, `eval`,
`watch`, `script -c`) it replaces the command by the string's content, parsed as code. Units nest up to 16 levels and 512
in all; beyond that the rule is applied by command name and the session is warned (degraded mode below).

Built in: `sudo doas env timeout nice ionice nohup time command exec builtin stdbuf setsid xargs watch script eval bash
sh zsh dash ksh`. Each entry is data:

| key | meaning |
|---|---|
| `flagsWithValue` | flags that consume the next word (`-u` for `sudo`); `--flag=value` and glued forms are one word anyway |
| `shellString` | `-c`: the word after that flag (also inside a cluster such as `-lc`) is a shell script; `rest`: all remaining words joined are the script (`eval`, `watch`) |
| `skip` | positional words to skip after the flags (`timeout` has 1: the duration) |
| `assignments` | skip leading `NAME=value` words (`env`, `sudo`) |
| `noCommandFlags` | flags after which nothing is executed (`command -v`) |

Add your own with `guardrails wrapper add <name> --json '{"flagsWithValue": ["-x"]}'` (`--scope global|project|managed`
and `--path`, like `rule`), `wrapper rm <name>`, `wrapper list`. Look-through only grows: a built-in wrapper cannot be
redefined and a layer can only introduce NEW names; an entry for a name that is built in, or defined by a higher layer, is
ignored and reported as a warning naming it (so a repository's project state can never change how `sudo` parses). A
declared wrapper never hides itself: `mywrap -x 1 pkill a` is matched both as the `mywrap` command and as `pkill a`,
in both engines. Invalid entries are skipped and reported. The plain lexer honours the flags of user wrappers too, and in
both engines clustered short flags whose last letter takes a value (`sudo -nu bob cmd`) consume the next word.

**False-negative risk.** The first word after the wrapper's known flags is taken as the wrapped command. A wrapper the
table does not know (`mywrap pkill x`) is an ordinary command, so the `pkill x` inside is not seen, and an unknown flag
of a known wrapper that takes a value (`sudo --some-flag VALUE pkill x`) makes `VALUE` the command. Declare such
wrappers and flags. Still not looked through: `ssh host pkill x`, `find . -exec pkill {} ;`, script files, `python -c`,
`bash <<EOF ... EOF`.

### Data versus code

Code (matched): a command; the body of `$(...)` or backticks, also inside double quotes and in an unquoted heredoc;
`<(...)`; the `-c` string of a shell and the arguments of `eval`. Data (never matched): a quoted-delimiter heredoc body,
single-quoted text, the arguments of other commands (`echo pkill`), redirect targets. `regex`, by contrast, reads the raw
text, so it also fires inside heredocs and quoted strings.

### When to use `regex` instead

Dataflow across commands (`ps | xargs kill` where the PIDs come from the other command, `curl x | sh`) is text spanning
nodes. A relation often expresses it (`xargs kill` inside a `pipeline`); a regex expresses the rest. A regex also fires
on text that only mentions the command, including heredocs.

### Idioms

- A command by name, with or without arguments: `{"pattern": "pkill $$$"}`, or the explicit
  `{"kind": "command", "has": {"field": "name", "regex": "^pkill$"}}`.
- Only in a context: `{"pattern": "pgrep $$$", "inside": {"any": [{"kind": "command_substitution"}, {"kind": "pipeline"}], "stopBy": "end"}}`.
- Not at top level: `inside` with `stopBy: "end"` and the container kinds you care about.

### Requirements and fallback

The matcher needs the PyPI wheel `ast-grep-py` (pinned, installed hash-checked into a venv in the plugin data dir, for
CPython 3.10 to 3.14; it bundles the Bash grammar). The hook runs it in-process when it runs under that venv's python (else in a child under it),
and only when an enabled rule with `match.ast` could apply: a call without such rules never loads it. A SessionStart hook
builds the venv in the background when an enabled rule uses `match.ast` (the PreToolUse hook itself never installs
anything); the build uses `uv` when found, else
`python -m venv` and `pip`, from the data dir with an allowlisted environment and `--no-config`, so repo-controlled
`uv.toml`, `UV_*`, `PIP_*` or `PYTHON*` settings cannot change what is installed. The README has the details.

Degraded mode (no wheel, build failure, deadline, bad worker reply, nesting over 16 or more than 512 units, a command over
16 KiB, or an unexpected error): every non-AST rule runs as usual and each `match.ast` rule is applied when the command
mentions one of its command names as a word. The names are derived from the literal words in its patterns and from its
regexes, or given by `match.mentions`. A deny rule denies with its message plus a note that the AST matcher was
unavailable and why; a warn rule warns; a command that mentions none of the names passes. This is coarse on purpose (it
also fires on `echo pkill` inside a heredoc), and a rule with no derivable names cannot fire. One warning per session
reaches the user and the agent. `rule test` prints the same as a note and exits 0.

The AST path always also matches the plain lexer's commands, so it never sees less than the lexer. Unbalanced quotes or
an unterminated heredoc (`ERROR` or missing nodes) therefore still get the lexer's view. The lexer itself descends into
`$(...)` (nested too), backticks, `eval`/`bash -c` strings and the substitutions of an unquoted heredoc body; a
quoted-delimiter heredoc stays data.

Bash and Monitor: the hook matches `Bash|Monitor`. A rule for `Bash` applies to a Monitor command too (a rule with
`"tool": "Monitor"` applies to Monitor only), retry acknowledgements and warn-once work the same, and a Monitor call
with no `command` (only a `ws` URL) is ignored. Monitors declared by a plugin start without a tool call and are not
covered.

## Pipelines: `args` does not see them

`args` is per command, so the pipe to `sh` in `curl https://x.sh | sh` is not among `curl`'s arguments. Verified:

- `match: {program: curl, args: "\\| *(sh|bash)"}` does NOT match `curl https://x.sh | sh`, nor `curl x|bash`.
- `match: {regex: "curl [^|]*\\|\\s*(sudo +)?(sh|bash)\\b"}` matches both, and `bash -c 'curl x | sh'` too.

So a rule about what a command is piped into, or about text spanning several commands, needs `match.regex`. The price:
a regex also fires on text that merely mentions the command (`echo "curl x | sh"` matches), which `program` never does.
Combine a narrow `regex` with its look-alikes in mind and test them.

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
   skipped and only `status` reports it (a non-compiling `ast` rule also warns once per session); an invalid managed
   rule is skipped with a warning.
9. The command has unbalanced quotes, so the raw-text fallback applies: `builtin` never matches there.
10. The rule's `tool` is not `Bash`.
    (A rule with `match.ast` is also skipped while the AST matcher is unavailable; the session got a warning.)
11. A lower layer cannot loosen a higher one: a global or project entry with `enabled: false` or `action: warn` over a
    managed or global rule has no effect, so a rule that "should have been turned off" may still be enforced.

Hook failures fail open: if the hook itself errors, the command runs.
