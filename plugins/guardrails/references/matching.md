# How guardrails matches a command

What `lib/matching.py` (the one evaluation path the hook and every CLI command share), `lib/rulebuilder.py` (the typed
ast-grep rules built from a guardrails rule), `lib/scanner.py` (parse units, wrapper variants, shell strings),
`lib/bounded.py` (the hard deadline) and `lib/policy.py` do. Check a claim with `guardrails rule test` before stating it.
The runtime, its trust model and what happens when the engine fails are in [runtime.md](runtime.md).

ast-grep does all the lexing and parsing, in process through the `ast-grep-py` library: guardrails writes no shell parser.
A rule's `match` is one ast-grep rule object, plus three guardrails atoms (`command`, `assignment`, `wrapper`) that are
expanded into plain ast-grep rules when the rule is compiled. It is matched on the tree-sitter Bash parse of the command
as written, of its wrapper variants and of each shell string, in a bounded checker.

Matcher ladder, narrowest first: `{"command": "pkill"}`, then a `command` with `args`, then a `pattern` plus `inside` /
`has` / `follows` / `precedes` when context matters, then `{"kind": "program", "regex": "..."}` over the whole text.
Before writing a rule with relations, run `guardrails rule ast '<command>'` to see the node kinds.

## What `rule test` does and does not check

`rule test` answers one question per command: does the rule's matcher select it (a `Block` or `Warn` row), not (an `Allow`
row), or could it not be judged (a `Not evaluated` row: the rule needs the engine and it is missing or failing, or the hook
would refuse the command). It does not apply modes, retry acknowledgements, `warn` versus `deny` or hook-level switches; it
prints a `**Note**` line when the hook would not act on a match. Report those notes next to any verdict; never call a matcher
result "blocked" on its own, and never read a `Not evaluated` row as "no match". The card is described in
[presentation.md](presentation.md); its `wrapped` tag means the command reached the rule only through a look-through (a
wrapper, a shell string, a substitution, a pipeline member). A direct hit wins over a look-through hit on the same command,
so a whole-text regex (`kind: program`) that matches the command as written is never `wrapped`.

## The match object

`match` is ONE rule. Its keys are ANDed on the same node; there are no alternatives between keys. Say "either" with `any`,
"also" with `all`, "except" with `not`. Every key is one of these (anything else is rejected when the rule is added,
tested or loaded, exit 2):

- ast-grep's own: `pattern`, `kind`, `regex`, `nthChild`, `range`, the relations `inside`, `has`, `follows`, `precedes`
  (each takes a rule, plus `stopBy` and `field`), and the combinators `all`, `any` (lists of rules) and `not` (a rule).
- the atoms below, usable anywhere a rule goes: at the top, inside `any` / `all` / `not`, as the rule of a relation or of
  a `stopBy`, next to any other key.

### The atoms

- `{"command": "pkill"}` or `{"command": ["pkill", "killall"]}`: a `command` node whose name is one of these, written
  however the shell allows a plain name: `pkill`, `/usr/bin/pkill`, `'pkill'`, `"pkill"`, after any number of `FOO=1`
  assignments (they are children of their own, not part of the name). Names have no spaces or `/` and are
  case-sensitive (`egrep` is not `grep`). After a syntax error it also selects the bare name the parser left behind.
  Wrapper names are commands too: `{"command": "sudo"}` matches `sudo ls`.
- `args`, only next to `command`: a Rust regex (ast-grep's `regex`: no look-around or back-references) that the matched
  command's whole text must contain, name and leading assignments included (behind a wrapper: the command as the variant
  reads it), so anchor with `(^|\s)`. It never sees a pipe or a substitution around the command.
- `{"assignment": {"name": "LD_PRELOAD", "value": "x.so"}}`: a `variable_assignment` node, `name` and `value` both
  optional (`{"assignment": {}}` is any assignment). A string is exact (a value may also be written in single or double
  quotes), `{"regex": "..."}` is used as written. An assignment is also a `declaration_command` child (`export A=1`), so
  for a command prefix say so: `{"kind": "command", "has": {"assignment": {"name": "LD_PRELOAD"}}}`.
- `{"wrapper": true}` or `{"wrapper": ["sudo", "env"]}`: a `command` named by one of the fixed wrapper words (below), or
  only the listed ones, spelled as `command` allows. Use it for a rule about the wrapper itself ("no `sudo curl | sh`");
  a rule about the wrapped command needs no wrapper atom, because wrappers are looked through.

### `wrappers`: not through wrappers

A rule field next to `match` and `action`, default `true`. With `"wrappers": false` the wrapper variants are not
matched, nor the shell strings reached only through them: `{"command": "pkill"}` with `"wrappers": false` matches
`pkill x`, `a | pkill x`, `echo $(pkill x)` and `bash -c 'pkill x'`, not `sudo pkill x`, `env A=1 pkill x` or
`sudo bash -c 'pkill x'`. Pipelines, substitutions and shell strings are looked through either way, and the `wrapped`
tag keeps its meaning.

### Removed keys

`match.program`, `match.args` (outside a `command` atom), `match.regex` alone and `match.ast` are gone; a rule that
still has them is refused by `rule add` / `rule test` and skipped by the hook, which names it once per session and in
`status`. The new form: `{"program": X}` is `{"command": X}`; `{"program": X, "args": A}` is `{"command": X, "args": A}`;
`{"ast": R}` is `R`; `{"regex": T}` is `{"kind": "program", "regex": T}`; several of them together become an `any` of
those.

### Composition

```json
{"command": "rm", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}}
{"any": [{"command": "shred"}, {"command": "rm", "args": "(^|\\s)-[a-z]*P"}]}
{"command": "sh", "inside": {"kind": "pipeline"}, "follows": {"command": ["curl", "wget"], "stopBy": "end"}}
{"command": "xargs", "args": "\\bkill\\b", "inside": {"kind": "pipeline"},
 "follows": {"command": ["ps", "pidof", "lsof"], "stopBy": "end"}}
```

`rm` unless inside an `if`; `shred`, or `rm -P`; `sh` fed by a downloader; `xargs kill` fed by a process lookup, which
also catches `ps x | /usr/bin/xargs -r kill` and `'ps' x | xargs kill`. Negated relations (`not` around `inside`,
`follows`, `precedes`, `has`) say what they mean because every rule runs on the real tree of the real text.

## How a command is looked through

Every rule is matched on the parse of the command as written, then on each wrapper variant and each shell string. A
`command` atom (or a `pattern`) reaches the command in a list, loop, `if`, subshell or group, in a pipeline, in `$(...)`
or backticks (also inside double quotes), in `<(...)`.

**Wrapper variants.** A command that contains a wrapper command (`sudo doas env timeout nice nohup time command exec builtin
stdbuf setsid ionice xargs watch`; the list is fixed) is also matched as text variants of the top-level statement (a direct
child of the program) that holds it, in which that wrapper command is replaced by the text from each of its own non-option
words onward: `sudo -u bob pkill x` also reads as `bob pkill x`, `pkill x` and `x` (the words come from the parse, never
split by guardrails; leading assignments such as `A=1 sudo x` are dropped with it). Nested wrappers need no recursion, because
a later word starts the inner command directly. Several wrappers in one statement also get every combination replaced
together while there are at most 64 (else each k-th word of all of them), so `sudo curl x | sudo sh` reads as `curl x | sh`.
Every rule (unless `"wrappers": false`) is matched on each variant and a hit found in one is wrapped. Because a variant is
one statement, its cost does not grow with the script around it, and repeated lines collapse. Variants are not unwrapped
again, are de-duplicated and are bounded: 2048 variants, 512 KiB of variant text parsed in total per command, 2048 wrapper
commands or words per unit and the 5 s deadline; past that the command is denied unparsed ("command too complex to check").

There is no table of which flags take a value: any word counts as a start, so `sudo grep pkill file` and `command -v pkill`
also match `{"command": "pkill"}` (known false positives; `retry: same-command` lets a deliberate repeat through, and
`"wrappers": false` drops them with every other wrapped form). The same coarseness applies to relation rules: `sudo grep
curl f | sh` matches a `curl $$$ | sh` rule. The accepted loss: a relation BETWEEN separate top-level statements (`follows`
across `;` or a newline) is judged only on the text as written, not through the wrapper; inside one statement (pipelines,
lists, subshells, loops, substitutions, an attached heredoc) relations are kept.

**Shell strings.** The script of `bash|sh|zsh|dash|ksh|script [flags] -c '<script>'` (also behind a wrapper and in a cluster
such as `-lc`), the arguments of `eval`, and a heredoc or here-string fed to a shell (`bash <<EOF`, `sh -s <<< 'cmd'`) are
unquoted (one shell word: single quotes as is, double quotes with the `\"` `\\` `\$` and backtick escapes, no ANSI-C
decoding) and scanned as units of their own, recursively; every hit in a unit counts as wrapped. The body of an unquoted
heredoc that contains `$(` or a backtick is scanned too, but only what lies inside those substitutions counts. Units are
de-duplicated and bounded: depth 8, 64 distinct units, 256 KiB of script text, with a budget apart from the variants.

**Never matched by a `command` atom:** heredoc bodies, redirect targets (`> pkill`), the arguments of other commands (`echo
pkill`, `man pkill`), quoted data.

**Cannot be analysed statically, so not matched:** obfuscated or dynamic names (`$'p\x6bill'`, `p''kill`, `p\kill`, a name
held in a variable, an alias or a function), scripts written as ANSI-C literals (`bash -c $'pkill x'`, `eval $'pkill x'`),
`ssh host pkill x`, `find . -exec pkill {} ;`, a script file (`bash script.sh`), `python -c '...'`, `echo pkill | sh` and
`cat <<EOF | sh`, `source <(echo pkill)`, `su -c`, `env -S '...'`, `watch 'pkill x'`, and a wrapper the fixed list does not
know. When a rule must not miss these, use a whole-text regex (`{"kind": "program", "regex": "..."}`) and accept its false
positives.

**After a syntax error** the tree is partial: a `command` atom still reads the bare words the parser left behind, but a
`pattern` rule does not see a wrapper's wrapped command there (`{ ; }; xargs -r pkill`: an empty group is valid zsh and a
syntax error in bash).

**Single-command patterns are spelling-tolerant.** A `pattern` that is exactly one simple command with arguments (`pkill -9
$$$`, `git push -f $$$`; no pipe, list, redirect, loop or substitution) also matches the command when its name is quoted or
has a directory (`/usr/bin/pkill`, `'pkill'`) and after up to three `VAR=x` assignments. Every other pattern goes to ast-grep
exactly as written. A pattern that ends in ` $$$` also selects the command with no arguments (a trailing `$$$` hole alone
never matches zero arguments in tree-sitter-bash, so guardrails widens it). A hole INSIDE a substitution, such as `kill
$($$$)`, never matches anything: use `inside` / `has`.

**`wrapped`.** A hit counts as wrapped when it was found in a wrapper variant or inside a shell string, or when the matched
node sits inside a pipeline or a substitution (`$(...)`, backticks, `<(...)`). A command in a list (`;` `&&` `||`), a loop,
an `if`, a subshell or a `{ ...; }` group is not wrapped. A direct hit beats a wrapped one on the same rule.

A rule is compiled when it is added, tested, listed by `status` and run by the hook. The CLI rejects one that does not
compile (exit 2); the hook skips it and warns once per session.

### Tree-sitter Bash node kinds that matter

Verified with `guardrails rule ast '<command>'`:

| kind | what it is |
|---|---|
| `program` | the root; its text is the whole command (from the first token) |
| `command` | one simple command; children: `variable_assignment` prefixes, `command_name`, then the arguments |
| `command_name` | the name word; the `command` atom selects it in any spelling |
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
| `variable_assignment` (fields `name`, `value`), `declaration_command` | `A=1` before a command or alone; `export A=1` |
| `case_statement`, `case_item`, `function_definition`, `negated_command`, `test_command` | the rest |
| `ERROR` | the parser could not make sense of part of the text |

### Data versus code, and when to use a whole-text regex

Code (matched): a command; the body of `$(...)` or backticks, also inside double quotes and in an unquoted heredoc body;
`<(...)`; the `-c` string of a shell and the arguments of `eval`. Data (never matched by a `command` atom or a `pattern`): a
quoted-delimiter heredoc body, single-quoted text, the arguments of other commands, redirect targets. A `regex` reads the
text of the node it is on; `{"kind": "program", "regex": "..."}` reads the whole command's text (quotes, heredocs and
pipelines included; it starts at the first token and `$` matches only at its very end), so it also fires inside heredocs
and quoted strings. Use it for dataflow across commands that no relation expresses; a relation often expresses the rest
(`xargs kill` inside a `pipeline`).

### Idioms

- A command by name, however it is spelled, with or without arguments: `{"command": "pkill"}`; with an argument:
  `{"command": "kill", "args": "(^|\\s)-9(\\s|$)"}` or the pattern `{"pattern": "kill -9 $$$"}`.
- Only in a context: `{"command": "pgrep", "inside": {"any": [{"kind": "command_substitution"}, {"kind": "pipeline"}], "stopBy": "end"}}`.
- Unless guarded: `{"pattern": "npm publish $$$", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}}`.
- Never after a specific step: `{"pattern": "git push $$$", "not": {"follows": {"pattern": "git pull $$$", "stopBy": "end"}}}`.
- A dangerous prefix: `{"kind": "command", "has": {"assignment": {"name": {"regex": "^(LD_PRELOAD|DYLD_INSERT_LIBRARIES)$"}}}}`.
- Only as typed, never through a wrapper: `{"match": {"command": "pkill"}, "wrappers": false, ...}`.

Bash and Monitor: the hook matches `Bash|Monitor`. Every rule applies to a Monitor command too, retry acknowledgements and
warn-once work the same, and a Monitor call with no `command` (only a `ws` URL) is ignored. Monitors a plugin declares
itself start without a tool call and are not covered.

## What happens on a match

- `action: deny` blocks the call and shows the message; `warn` lets it run and shows the message to the agent once per
  session per rule. If something in the same command denies, warnings ride along in the deny message.
- `retry: same-command` (deny only): the first occurrence is blocked and remembered for the session, re-running the
  identical command text passes. Any changed text is blocked again.
- `messageShort` replaces `message` once the full text was shown in the session (captures aside: the same text for
  another command counts as shown). `messages` cases and placeholders pick and fill the text (below).
- `enabled: false` skips the rule, and so does a `when` that does not hold (below).
- `modes`: the rule is skipped while any listed mode is active. A mode is active when switched on persistently (global,
  project, or managed) or for this session. An agent may switch on a session mode only when it is declared with
  `agentMayEnable` (and a rule suspended by an agent-enabled mode is reported to the user).
- The deny text starts with `[guardrails:<id>#<hash>]` (`<id>#<hash> (managed)` for a managed rule). The hash is eight
  hex digits of the effective rule as enforced: `match`, `wrappers`, `when`, `action`, `retry`, `message`, `messageShort`
  and `messages` (so rewording changes it), never `description`, `enabled`, `modes` or who set it. A rule id cannot
  contain `#`. Warn texts carry the same marker.

## Conditions: `when`

`when` (optional, next to `match`) says where the rule applies at all. It is checked before the command is parsed, so a
rule whose `when` does not hold costs nothing, writes no telemetry row, is `inactive here` in `status` and gets a
`**Note**` in `rule test`. It is a tree built like `match`: `{"all": [...]}`, `{"any": [...]}` (non-empty lists) and
`{"not": <condition>}` around atoms, one key per object, at most 8 levels deep and 64 objects in all. The atoms:

- `{"bin": "fd"}` or `{"bin": ["fd", "fdfind"]}`: one of these names is an executable on PATH (looked up once per name
  and PATH in a process). Names have no `/` and no spaces.
- `{"os": "linux"}` or `{"os": "macos"}`; `{"arch": "arm64"}` or `{"arch": "x86_64"}` (`aarch64` reads as `arm64`,
  `amd64` as `x86_64`).
- `{"host": "box"}`: the host name, or its short form before the first dot, is exactly this.
- `{"env": "CI"}`: the variable is set and not empty; `{"env": {"CI": "true"}}`: it equals this value (one variable per
  atom; combine with `all`).
- `{"file": "Cargo.toml"}`: the path exists relative to the project root (`CLAUDE_PROJECT_DIR`, else the git
  top level). Relative only, no `..`; false outside a project.
- `{"tool": "Bash"}` or `{"tool": "Monitor"}`: the tool that made the call. `status` and `rule test` judge a `when` as
  for a Bash call.
- `{"background": true}` or `{"background": false}`: whether the call asked to run in the background, the Bash tool's
  `run_in_background` input. A call without the field, and every Monitor call, is not in the background; `status` and
  `rule test` judge a `when` as for a foreground call.

`when` replaced `requires`: `"requires": ["fd", "fdfind"]` is `"when": {"bin": ["fd", "fdfind"]}`. A rule that still
has `requires` is invalid: `rule add` / `rule test` refuse it (exit 2), the hook skips it and names it once per session,
and `status --problems` lists it. It never runs without its condition. Conditions read the environment the hook runs
in, which `settings.json` (`env`) and the shell can change; that is trusted by design ([runtime.md](runtime.md)).

## Messages: cases and placeholders

`messages` (optional) is an ordered list of cases `{"when": <condition>, "text": "...", "messageShort": "..."}`. For a
hit, the first case whose `when` holds supplies the text (and its `messageShort`; a case without one is always shown in
full); when none holds, the rule's own `message` and `messageShort` apply, so `message` stays required. At most 16
cases. A case's `when` takes every atom above plus two that read the hit (refused in a rule's own `when`, which runs
before any command is read):

- `{"matches": <rule>}`: the matched node also satisfies this rule, an ast-grep rule object that may use the
  `command`, `assignment` and `wrapper` atoms and everything else `match` can (it is built the same way), tested with
  ast-grep's own node matcher on that one node. Relations look from that node: `has` sees its children, `inside` its
  ancestors.
- `{"wrapped": true}` or `false`: the hit is wrapped (the `wrapped` tag of `rule test`) or direct.

The matched node is the node of the hit that decided the rule's verdict: the first node the rule's `match` selected in
scan order (the command as written first, then its wrapper variants and shell strings, breadth first), except that a
later direct hit replaces a wrapped one. It is the node `rule test` reports as `direct` or `wrapped`; `matches`,
`wrapped` and the captures all read it.

Placeholders in `message`, `messageShort` and a case's `text` / `messageShort`:

- `{found}`: the first name of a `bin` atom of the rule's own `when` that is on PATH, in document order (atoms under a
  `not` do not count), so `{"bin": ["fd", "fdfind"]}` gives `fdfind` where only that is installed. A text that uses
  `{found}` needs such a `bin` atom (else the rule is invalid); when `when` held through another branch it is empty.
- `{ARG}`: what the metavariable `$ARG` of the rule's `match` patterns captured on the matched node; `$$$ARGS` gives
  the captured words joined by one space. A capture the node did not bind (another branch of an `any`, a case's
  `matches`, which only tests) is empty. Each capture is cut at 200 characters and a rule names at most 8.
- `{{` and `}}` are literal braces. Anything else in braces (`{x.y}`, `{x[0]}`, `{x!r}`, `{x:>5}`, `{}`, a lowercase
  name, an unbalanced brace) makes the rule invalid. Texts are split with Python's `string.Formatter` parser and only
  simple names are substituted; rule text is never run through `str.format`.

## Layers

Order: managed, then global, then project. A lower layer can add rules of its own, and for an id a higher layer already
defines it can only tighten: switch `action` to deny, `retry` to none, re-enable, remove suspending `modes`, and reword
`message` / `messageShort` / `description` (not for managed rules; a reworded text is checked like the rule's own, so
`{found}` needs a `bin` atom in the higher layer's `when`). It cannot change `match`, `wrappers`, `when` or `messages`
(cases are not overridable), loosen, disable or add modes. An override that fails validation is ignored. The layers live in the managed file,
`config.json` under the XDG config dir (global) and `<project>/.claude/guardrails.json` (project). A project at the home
directory (a session started there) has no project layer: `~/.claude/guardrails.json` is never read.

- Managed rules are always enforced unless their `modes` are declared by the managed file itself; a managed rule with no
  (declared) modes cannot be suspended by anything, and it still applies when the global hook is disabled.
- A project cannot switch on a mode the managed file declares, and cannot make a managed mode agent-enablable.
- An unreadable or invalid managed file never turns the guard off: the broken part is skipped and reported.

## Why a rule may not fire

Check these in order when a command the matcher selects still runs:

1. The global hook is disabled (`guardrails disable`): global and project rules are off, managed rules still apply.
2. Project rules are disabled in the project config: project entries are dropped.
3. The rule is disabled (`enabled: false`), or its `when` does not hold here (`status` says `inactive here`).
4. A listed mode is active, so the rule is suspended.
5. `retry: same-command` and the identical command was already blocked once this session.
6. `action: warn`: the command runs, the agent only gets the message.
7. The rule is invalid (an unknown field, the removed `requires`, a removed `match` key such as `program` or `ast`, a
   malformed atom or condition, a bad placeholder, or a `match` or case `matches` that does not compile): it is skipped,
   the session is told once and `status` names it.
8. The runtime was not installed yet, or the engine failed on this call: the command was allowed and the session got a notice
   ([runtime.md](runtime.md)).
9. The command is one of the documented limits above, or it came through a wrapper and the rule has `"wrappers": false`.
10. A lower layer cannot loosen a higher one: a global or project entry with `enabled: false` or `action: warn` over a managed
    or global rule has no effect.
11. The rule is in `state.json` in the plugin data dir, or in a project's old `.claude/plugins/data/…/state.json`, not in a
    config file: it is not read, the session is told once and `status --problems` names it.
