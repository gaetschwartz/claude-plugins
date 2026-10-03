# How guardrails matches a command

What `lib/matching.py` (the one evaluation path the hook and every CLI command share), `lib/rulebuilder.py` (the typed
ast-grep rules built from a guardrails rule), `lib/scanner.py` (parse units, wrapper variants, shell strings),
`lib/bounded.py` (the hard deadline) and `lib/policy.py` do. Check a claim with `guardrails rule test` before stating it.
The runtime, its trust model and what happens when the engine fails are in [runtime.md](runtime.md).

ast-grep does all the lexing and parsing, in process through the `ast-grep-py` library: guardrails writes no shell parser.
`program`, `args`, `regex` and `match.ast` become typed ast-grep rules and are matched on the tree-sitter Bash parse of the
command as written, of its wrapper variants and of each shell string. `regex` reads the raw text of the whole command with
ast-grep's own regex engine, in the same bounded checker.

Matcher ladder, narrowest first: `program`, then `program` + `args`, then an `ast` rule (a `pattern`, plus `inside` / `has`
when context matters), then `regex`. Before writing an `ast` rule with relations, run `guardrails rule ast '<command>'` to
see the node kinds.

## What `rule test` does and does not check

`rule test` answers one question per command: does the rule's matcher select it (a `Block` or `Warn` row), not (an `Allow`
row), or could it not be judged (a `Not evaluated` row: the rule needs the engine and it is missing or failing, or the hook
would refuse the command). It does not apply modes, retry acknowledgements, `warn` versus `deny` or hook-level switches; it
prints a `**Note**` line when the hook would not act on a match. Report those notes next to any verdict; never call a matcher
result "blocked" on its own, and never read a `Not evaluated` row as "no match". The card is described in
[presentation.md](presentation.md); its `wrapped` tag means the command reached the rule only through a look-through (a
wrapper, a shell string, a substitution, a pipeline member). A `regex` match is never `wrapped`, and a regex hit wins over a
look-through hit on the same command.

## `program`, `args`

These compile to ast-grep rules over the parse tree. The matched node is a `command`; its name must be one of the program
names, written however the shell allows a plain name: `pkill`, `/usr/bin/pkill`, `'pkill'`, `"pkill"`, after `FOO=1`
assignments, in a list, loop, `if`, subshell or group, in a pipeline, in `$(...)` or backticks (also inside double quotes),
in `<(...)`.

**Wrapper variants.** A command that contains a wrapper command (`sudo doas env timeout nice nohup time command exec builtin
stdbuf setsid ionice xargs watch`; the list is fixed) is also matched as text variants of the top-level statement (a direct
child of the program) that holds it, in which that wrapper command is replaced by the text from each of its own non-option
words onward: `sudo -u bob pkill x` also reads as `bob pkill x`, `pkill x` and `x` (the words come from the parse, never
split by guardrails; leading assignments such as `A=1 sudo x` are dropped with it). Nested wrappers need no recursion, because
a later word starts the inner command directly. Several wrappers in one statement also get every combination replaced
together while there are at most 64 (else each k-th word of all of them), so `sudo curl x | sudo sh` reads as `curl x | sh`.
Every rule is matched on each variant and a hit found in one is wrapped. Because a variant is one statement, its cost does
not grow with the script around it, and repeated lines collapse. Variants are not unwrapped again, are de-duplicated and are
bounded: 2048 variants, 512 KiB of variant text parsed in total per command, 2048 wrapper commands or words per unit and the
5 s deadline; past that the command is denied unparsed ("command too complex to check").

There is no table of which flags take a value: any word counts as a start, so `sudo grep pkill file` and `command -v pkill`
also match `pkill` (known false positives; `retry: same-command` lets a deliberate repeat through). The same coarseness
applies to relation rules: `sudo grep curl f | sh` matches a `curl $$$ | sh` rule. The accepted loss: a relation BETWEEN
separate top-level statements (`follows` across `;` or a newline) is judged only on the text as written, not through the
wrapper; inside one statement (pipelines, lists, subshells, loops, substitutions, an attached heredoc) relations are kept.

**Shell strings.** The script of `bash|sh|zsh|dash|ksh|script [flags] -c '<script>'` (also behind a wrapper and in a cluster
such as `-lc`), the arguments of `eval`, and a heredoc or here-string fed to a shell (`bash <<EOF`, `sh -s <<< 'cmd'`) are
unquoted (one shell word: single quotes as is, double quotes with the `\"` `\\` `\$` and backtick escapes, no ANSI-C
decoding) and scanned as units of their own, recursively; every hit in a unit counts as wrapped. The body of an unquoted
heredoc that contains `$(` or a backtick is scanned too, but only what lies inside those substitutions counts. Units are
de-duplicated and bounded: depth 8, 64 distinct units, 256 KiB of script text, with a budget apart from the variants.

**Never matched by `program`:** heredoc bodies, redirect targets (`> pkill`), the arguments of other commands (`echo pkill`,
`man pkill`), quoted data.

**Cannot be analysed statically, so not matched:** obfuscated or dynamic names (`$'p\x6bill'`, `p''kill`, `p\kill`, a name
held in a variable, an alias or a function), scripts written as ANSI-C literals (`bash -c $'pkill x'`, `eval $'pkill x'`),
`ssh host pkill x`, `find . -exec pkill {} ;`, a script file (`bash script.sh`), `python -c '...'`, `echo pkill | sh` and
`cat <<EOF | sh`, `source <(echo pkill)`, `su -c`, `env -S '...'`, `watch 'pkill x'`, and a wrapper the fixed list does not
know. When a rule must not miss these, use `regex` and accept its false positives.

**After a syntax error** the tree is partial: `program` and `args` rules still read the bare words the parser left behind,
but a `pattern` rule does not see a wrapper's wrapped command there (`{ ; }; xargs -r pkill`: an empty group is valid zsh
and a syntax error in bash).

### The fields

- `match.program`: one name or a list (no spaces or `/`); case-sensitive, `egrep` is not `grep`. Wrapper names are programs
  too: `program: sudo` matches `sudo ls`.
- `match.args`: a Rust regex (ast-grep's `regex`: no look-around or back-references) over the matched command's whole text,
  name included (behind a wrapper: the command as the variant reads it), so anchor with `(^|\s)`. It narrows `program`
  (AND); alone, or next to `regex` only, it has no effect and a rule whose `match` has none of `program`, `regex`, `ast` is
  rejected.
- `match.regex`: a Rust regex (linear time, no backreferences or look-around; `rule add/set/test` reject those with exit 2)
  searched in the command's whole text, quotes, heredocs and pipelines included. The text starts at the first token (leading
  blanks are not part of it) and `$` matches only at its very end.
- `match.ast`: an ast-grep rule object over the parsed syntax tree; see below.
- The matchers are alternatives: the rule fires when the program/args part matches OR `regex` matches OR `ast` matches.

## `match.ast`: the syntax tree matcher

An ast-grep rule evaluated on the tree-sitter Bash parse of the command as written, and again on each shell string. It sees
structure that `program`/`args` cannot (nesting in a substitution, a pipeline, a loop, an `if`) and ignores text that `regex`
cannot tell apart from code (heredoc bodies, single-quoted strings). Negated relations (`not inside`, `not follows`, `not
precedes`) say what they mean because every rule runs on the real tree of the real text. Pipeline and list patterns (`curl
$$$ | sh`, `cd $A && rm $$$`) and negations work through wrappers, because each variant is parsed as a whole statement.

The keys of one object are ANDed. Supported keys (anything else is rejected when the rule is added or tested, exit 2):

- `pattern`: a code snippet with metavariables, `$A` for one node and `$$$` for any number; or `{"context": "...",
  "selector": "command"}` to select a node kind inside a larger snippet.
- `kind`: a node kind such as `command` or `pipeline` (table below). `regex`: a Rust regex over the node's source text.
- Relations, each an object that is itself a rule: `inside` (an ancestor matches), `has` (a descendant matches), `follows`
  and `precedes` (a sibling before or after). They accept `stopBy`: `"neighbor"` (the default: only the nearest level),
  `"end"` (all the way to the root or the leaf), or a rule to stop at; and `field`, to restrict to a named child field.
- Composition: `not` (a rule), `any` and `all` (lists of rules).

A rule is compiled when it is added, tested, listed by `status` and run by the hook. The CLI rejects one that does not
compile (exit 2); the hook skips it and warns once per session.

**Single-command patterns are spelling-tolerant.** A `pattern` that is exactly one simple command with arguments (`pkill -9
$$$`, `git push -f $$$`; no pipe, list, redirect, loop or substitution) also matches the command when its name is quoted or
has a directory (`/usr/bin/pkill`, `'pkill'`) and after up to three `VAR=x` assignments. Every other pattern goes to ast-grep
exactly as written. A pattern that ends in ` $$$` also selects the command with no arguments (a trailing `$$$` hole alone
never matches zero arguments in tree-sitter-bash, so guardrails widens it). A hole INSIDE a substitution, such as `kill
$($$$)`, never matches anything: use `inside` / `has`.

**`wrapped`.** A hit counts as wrapped when it was found in a wrapper variant or inside a shell string, or when the matched
node sits inside a pipeline or a substitution (`$(...)`, backticks, `<(...)`). A command in a list (`;` `&&` `||`), a loop,
an `if`, a subshell or a `{ ...; }` group is not wrapped. A direct hit beats a wrapped one on the same rule.

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

### Data versus code, and when to use `regex`

Code (matched): a command; the body of `$(...)` or backticks, also inside double quotes and in an unquoted heredoc body;
`<(...)`; the `-c` string of a shell and the arguments of `eval`. Data (never matched): a quoted-delimiter heredoc body,
single-quoted text, the arguments of other commands, redirect targets. `regex` reads the raw text, so it also fires inside
heredocs and quoted strings. Use it for dataflow across commands that no relation expresses (`ps | xargs kill` where the PIDs
come from the other command); a relation often expresses the rest (`xargs kill` inside a `pipeline`).

### Idioms

- A command by name, however it is spelled, with or without arguments: `program: pkill`; in `ast`, `{"pattern": "pkill $$$"}`,
  or the explicit `{"kind": "command", "has": {"field": "name", "regex": "^pkill$"}}`.
- Only in a context: `{"pattern": "pgrep $$$", "inside": {"any": [{"kind": "command_substitution"}, {"kind": "pipeline"}], "stopBy": "end"}}`.
- Unless guarded: `{"pattern": "npm publish $$$", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}}`.

Bash and Monitor: the hook matches `Bash|Monitor`. Every rule applies to a Monitor command too, retry acknowledgements and
warn-once work the same, and a Monitor call with no `command` (only a `ws` URL) is ignored. Monitors a plugin declares
itself start without a tool call and are not covered.

## What happens on a match

- `action: deny` blocks the call and shows the message; `warn` lets it run and shows the message to the agent once per
  session per rule. If something in the same command denies, warnings ride along in the deny message.
- `retry: same-command` (deny only): the first occurrence is blocked and remembered for the session, re-running the
  identical command text passes. Any changed text is blocked again.
- `messageShort` replaces `message` once the full text was shown in the session; `{which:a|b}` in a message becomes the
  first binary found on PATH.
- `enabled: false` skips the rule. `requires` skips it unless one of the binaries is installed.
- `modes`: the rule is skipped while any listed mode is active. A mode is active when switched on persistently (global,
  project, or managed) or for this session. An agent may switch on a session mode only when it is declared with
  `agentMayEnable` (and a rule suspended by an agent-enabled mode is reported to the user).
- The deny text starts with `[guardrails:<id>]`, or `<id> (managed)` for a managed rule.

## Layers

Order: managed, then global, then project. A lower layer can add rules of its own, and for an id a higher layer already
defines it can only tighten: switch `action` to deny, `retry` to none, re-enable, remove suspending `modes`, and reword
`message` / `messageShort` / `description` (not for managed rules). It cannot change `match` or `requires`, loosen, disable
or add modes. An override that fails validation is ignored. A project whose state file is the global one (a session started
in the home directory) has no project layer.

- Managed rules are always enforced unless their `modes` are declared by the managed file itself; a managed rule with no
  (declared) modes cannot be suspended by anything, and it still applies when the global hook is disabled.
- A project cannot switch on a mode the managed file declares, and cannot make a managed mode agent-enablable.
- An unreadable or invalid managed file never turns the guard off: the broken part is skipped and reported.

## Why a rule may not fire

Check these in order when a command the matcher selects still runs:

1. The global hook is disabled (`guardrails disable`): global and project rules are off, managed rules still apply.
2. Project rules are disabled in the project state: project entries are dropped.
3. The rule is disabled (`enabled: false`), or `requires` lists binaries and none is installed.
4. A listed mode is active, so the rule is suspended.
5. `retry: same-command` and the identical command was already blocked once this session.
6. `action: warn`: the command runs, the agent only gets the message.
7. The rule is invalid (an unknown field, a regex or an `ast` rule that does not compile): it is skipped and `status` names it.
8. The runtime was not installed yet, or the engine failed on this call: the command was allowed and the session got a notice
   ([runtime.md](runtime.md)).
9. The command is one of the documented limits above.
10. A lower layer cannot loosen a higher one: a global or project entry with `enabled: false` or `action: warn` over a managed
    or global rule has no effect.
