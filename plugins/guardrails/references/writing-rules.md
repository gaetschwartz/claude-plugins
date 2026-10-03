# Writing guardrails rules

The full procedure for one rule. Read it when a rule is anything beyond `program` / `args`, uses a relation or a regex,
covers several behaviors, or has wrapper or quoting concerns, and whenever you are unsure. Field and matcher semantics
are in [matching.md](matching.md) (not repeated here); worked `ast` rules by shape are in [ast/index.md](ast/index.md).

The steps: intent, one behavior, cheapest matcher, read the tree, write, test, tighten, message, settings.

## 1. Intent

State it as one sentence: "deny/warn on X, except Y, because Z, and the agent should do W instead." Collect at least
one command that must be caught and one that must pass before touching a matcher. If you cannot name W, ask; a rule
without an alternative only teaches the agent to retry with a spelling you did not think of.

## 2. One behavior per rule

Split on "and". Two rules are better than one whenever the behaviors differ in message, alternative, action, `retry`,
or `modes`:

- "Block `pkill` and `killall`, and warn on `kill -9`": two rules (deny, warn), each with its own message.
- "Block `rm -rf /` and `docker rm $(...)`": two rules; the messages name different alternatives.
- `pkill` and `killall` with the same message and action are one behavior: one rule with `program: [pkill, killall]`
  or an `any`.

A vague message on a rule that covers several things is the usual symptom of a rule that should be split.

## 3. Matcher ladder

Take the first rung that separates your examples. Each rung down costs precision or runtime.

| rung | right when | example |
|---|---|---|
| `program` | the command name alone decides | `program: pkill` |
| `program` + `args` | one command's own words decide (regex over its arguments) | `program: docker`, `args: "\\bsystem prune\\b"` |
| `builtin` | a named shape the engine knows (`grep-recursive`) | `builtin: grep-recursive` |
| `ast` | structure decides: nesting, pipelines, order, a flag on one command among several, a wrapper or shell itself | [ast/index.md](ast/index.md) |
| `regex` | only raw text can say it, across nodes the tree cannot relate | text of one shell line, or unparseable input |

`regex` is last because it reads heredocs and quoted strings as if they were code. It is Rust regex syntax (ast-grep's engine,
linear time): no backreferences or look-around, and `rule add/test` refuse them. `args` is per command and never sees
a pipe or a substitution. `program` never matches a wrapper or shell (`sudo`, `bash`). Each is a pitfall below.

## 4. Get the tree

For any `ast` rule with a relation, before writing it:

    guardrails rule ast 'sudo kill -9 $(pidof vite)'

prints one tree per unit: the command as written, then one per shell string it hands to a shell (the wrapper variants
the matcher also tries are not trees of their own). Excerpt:

```text
tree: command as written
  program
    command
      command_name «sudo»
      word «kill»
      number «-9»
      command_substitution
        command
          command_name «pidof»
          word «vite»
tree: through sudo, source: kill -9 $(pidof vite)
  program
    command
      command_name «kill»
      number «-9»
      command_substitution
        ...
```

Reading it: indentation is depth; the first line of a unit's node is its kind and leaves show their text. A relation
walks these edges: `inside` goes up, `has` goes down, `follows` / `precedes` go sideways. The name is a `command_name`
child (select it with `field: name`); arguments are the `word`, `number`, `string`, `raw_string` and substitution
children that follow. `-9` is a `number`. A substitution is a node of its own, a pipeline's members are sibling
`command` nodes, and `;` / newline separated commands are siblings under `program`. The kinds table is in
[matching.md](matching.md#tree-sitter-bash-node-kinds-that-matter). Never guess a kind: a wrong one is rejected or,
worse, never matches.

## 5. Write the rule

1. Start with a `pattern` for the dangerous command (`docker rm $$$`). Anchor the rule on it, not on the wrapper or the
   context: wrappers are transparent (the command is also matched with the wrapper replaced), so `sudo X` needs no rule.
2. Add one relation per fact about the surroundings (`inside` a substitution, `follows` a downloader). Give relations
   `stopBy: end` unless you mean the nearest level.
3. Flags and values: `has` with a `regex` on one word; `all` to require several, `any` for alternatives, `not` + `has`
   for an exception on the same command.
4. A bare `kind` is fine when a pattern cannot say it (`kind: command` with `has: {field: name, regex: ...}`).
5. `id` from the intent (`no-pkill`), `description` in the user's words, `action`, `message`.

Negated context (`not` around `inside`, `follows`, `precedes`) works, because every rule runs on the real tree of the
real text and of each shell string: see [ast/context.md](ast/context.md#a-command-unless-it-is-guarded).

## 6. Test matrix

Write it before tuning. Run it with `guardrails rule test --json - ... <<'EOF'` (document shape
`{"rule": {...}, "examples": [{"cmd": "...", "expect": "match|pass"}]}`); it prints the card.

| group | minimum | why |
|---|---|---|
| must catch, direct | 1 | the plain form |
| must catch, wrapped | 1 to 3 | `sudo X`, `bash -c 'X'`, a pipe or `$( )` |
| must pass, look-alike | 1 | the near miss the rule is for (`pgrep -xl` vs `pgrep` in `$( )`, `--force-with-lease`) |
| must pass, mention | 3 | `man X`, `echo "X"`, a heredoc that mentions X |
| edge cases | as many as apply | the checklist below |

`expect` is what you want, never what the matcher did. A mismatch means the rule and the intent disagree: fix the rule.

## 7. Tighten

Loop: run the matrix, fix one mismatch, re-run all of it. Stop at zero mismatches, not at "mostly". Fix a false positive
by narrowing (a relation, an anchored regex, `not` + `has`), a false negative by adding an alternative, not by
loosening the pattern until it matches everything. If the examples cannot all be satisfied, drop or rephrase one with
the user; do not stack special cases.

## 8. Message

The message is the only thing the agent reads when it is denied. Name the alternative and keep it short: what was
wrong, what to run instead. `{which:a|b}` becomes the first binary found on PATH; `messageShort` replaces `message`
after the first time it was shown in a session. Good: "Do not kill by name. Look the PID up with `pgrep -xl <name>`
as its own command, then `kill <pid>`." Bad: "Blocked."

## 9. Settings

- **deny versus warn.** deny when the command is harmful or irreversible, or has a clear replacement. warn when it is
  merely discouraged or legitimate in some contexts: the command runs and the agent sees the message once per session.
- **`retry: same-command`** (deny only): the first occurrence is blocked, re-running the identical text passes. Use it
  when a legitimate use exists and the message explains what to double-check; use none when it must never run.
- **`modes` and `agentMayEnable`**: a rule listing a mode is suspended while that mode is on. Declare the mode
  agent-enablable only if the user should be able to say "this session is X" and have the agent switch it on; an agent
  never enables a mode on its own because of a denial.
- **Scopes and layering**: global by default, `--scope project` for one repository, `--scope managed` for enforced
  rules. Order is managed, global, project; a lower layer can add rules and, for an existing id, only tighten
  (action to deny, retry off, re-enable, reword); it can never change `match`, loosen or disable. Managed rules with no
  declared modes cannot be suspended. Details in [matching.md](matching.md#layers).

## 10. Edge-case checklist

Test each that applies to the rule. Expected results are for a rule about the command `X`.

| case | example | expected |
|---|---|---|
| sudo / env / timeout / nice / xargs | `sudo X`, `env A=1 X`, `timeout 5 X`, `xargs X` | caught (wrapped) |
| `bash -c` and friends | `bash -c 'X'`, `sh -c "a; X"`, `eval X` | caught |
| double versus single quotes | `echo "$(X)"` versus `echo '$(X)'` | caught versus passes |
| substitution glued to a word | `echo foo$(X)bar`, `` echo `X` `` | caught |
| heredoc, unquoted and quoted | `cat <<EOF` with `$(X)` or `` `X` `` inside; `cat <<'EOF'` mentioning X | substitution caught; quoted passes |
| `<<-` | `cat <<-EOF` with tab-indented body and terminator | body is data |
| lists, subshells, groups | `a; X`, `a && X`, `(X)`, `{ X; }`, `X &` | caught, direct |
| newline and continuation | `a\nX`, `X \` + newline + `-f` | caught |
| absolute path, quoted name, assignment prefix | `/usr/bin/X`, `"X"`, `FOO=1 X` | caught by `program`; a literal-argument `pattern` does not see through `FOO=1` |
| mention only | `man X`, `echo X`, `echo "X -f"` | passes |
| look-alike name | `visudo` vs `sudo`, `pgrep` vs `pkill` | passes |
| piped into or fed by | `a \| X`, `X \| b` | per the rule's intent |
| Monitor and background use | the same command in a Monitor call, `X &` | rules for Bash also apply to Monitor |

Known limits, where the rule cannot see the command (say so in the description rather than hoping):

- a name held in a variable (`cmd=X; $cmd`), a function or an alias that expands to it
- `ssh host X`, `find . -exec X {} \;`, scripts (`bash script.sh`), `python -c '...'`
- a wrapper the table does not know (declare it with `guardrails wrapper add`)
- an obfuscated name (`p''kill`, `$'p\x6bill'`, `p\kill`) or a script given as an ANSI-C literal (`bash -c $'X'`): it cannot be analysed statically
- `watch 'X'`, `su -c 'X'`, `echo X | sh`, `source <(echo X)`: only `bash -c`, `script -c`, `eval` and shell-fed heredocs and here-strings are scanned
- the coarseness of wrapper variants: any word of a wrapper can start a command, so `sudo grep curl f | sh` matches a `curl $$$ | sh` rule

## The six pitfalls

Each one makes a rule look right and be wrong. The short list in the `new` skill uses the same ids.

### trailing-holes

End a pattern with `$$$` (zero or more words), never `$A` (exactly one, so `cat $A` misses bare `cat`). A pattern is
anchored at the start only: trailing words always match (`git push` matches `git push -f`), and literal flags are
order-sensitive (`rm -rf` is not `rm -fr`), so match flag spellings with `has` + `regex`. guardrails widens a trailing
` $$$` so that it also matches zero arguments.

### hole-in-substitution

A hole inside a substitution never matches: `kill $($$$)` passes `kill $(pidof x)`. Use `has` with
`kind: command_substitution` and `stopBy: end`. See [ast/flags.md](ast/flags.md).

### stop-by

Relations default to the nearest level: `inside` checks only the parent (a `pgrep` in a loop body has a `do_group`
parent), `has` only direct children (a substitution inside `"..."` is deeper), and `follows` / `precedes` see the `|`
or `&&` token next to the node, not the command beyond it. Add `stopBy: end` unless the nearest level is what you mean.

### args-no-pipelines

`args` is a regex over one command's own text: `program: curl` + `args: "\| *sh"` never matches `curl x | sh`, because
the pipe is not part of the `curl` command. A question about what a command is piped into, or fed by, needs an `ast`
relation or a `regex`. See [ast/pipelines.md](ast/pipelines.md).

### program-and-wrapper-words

`program` matches a command by name, `program: sudo` (or `bash`, `env`, `xargs`) included, and also the command behind a
wrapper, read from any of the wrapper's own words: `program: pkill` matches `sudo -u bob pkill x` and, as a known false positive,
`sudo grep pkill file` and `command -v pkill`. Test the look-alikes. See [ast/wrappers.md](ast/wrappers.md).

### regex-in-heredocs

`regex` reads raw text: it fires on `echo "X"`, `man X` and inside heredocs and quotes, and a regex hit is never tagged
wrapped. Use it only when no tree relation says it, and then test the mentions.

## Anti-patterns

- A raw-text `regex` for a structural question ("only inside `$( )`", "as the last pipeline member").
- Matching the wrapper instead of the wrapped command (`sudo` rather than what runs under it).
- One rule for several behaviors under a vague message.
- Relying on holes inside `$( )`.
- Loosening the pattern to fix a false negative instead of adding an alternative.
- Testing only the commands you expect to catch.
- Changing a rule to get past a denial. A denial is never a reason to edit, disable or remove a rule.

## Worked example

Intent: "Deny `docker rm` when its targets come from a command substitution (`docker rm -f $(docker ps -aq)`); the
agent should list the containers and remove the ones it means." One behavior (the `docker ps | xargs docker rm` form is
another rule with its own shape).

Rung 1, `program: docker`: blocks `docker ps`. Rung 2, `program: docker` with `args: "\brm\b.*\$\("`: `args` is a
regex over the command's text, so it catches the real commands but cannot tell a substitution from text and fires on
`docker rm 'a$(b)'` (verified with `rule test`). Rung 5, `regex: "docker (container )?rm\b.*\$\("`: catches the real commands, but
also `echo "docker rm $(docker ps -aq)"`, a quoted look-alike and a heredoc that mentions it. So `ast`.

`guardrails rule ast 'docker rm -f $(docker ps -aq)'` shows a `command` (name `docker`, words `rm`, `-f`) with a
`command_substitution` child. A substitution can also sit deeper (`"$(...)"`), hence `has` with `stopBy: end`;
`docker container rm` is a second spelling of the same behavior, hence `any`.

```rule-example
{
  "id": "no-docker-rm-by-substitution",
  "title": "docker rm fed by a substitution",
  "rule": {
    "ast": {
      "all": [
        {"any": [{"pattern": "docker rm $$$"}, {"pattern": "docker container rm $$$"}]},
        {"has": {"kind": "command_substitution", "stopBy": "end"}}
      ]
    }
  },
  "action": "deny",
  "catch": [
    "docker rm -f $(docker ps -aq)", "docker container rm $(docker ps -aq)",
    "sudo docker rm -f $(docker ps -aq)", "docker rm -f `docker ps -aq`", "docker rm \"$(docker ps -aq)\"",
    "bash -c 'docker rm $(docker ps -aq)'"
  ],
  "pass": [
    "docker rm web", "docker run --rm -e D=$(date) img", "docker rm 'a$(b)'",
    "echo \"docker rm $(docker ps -aq)\"", "docker ps -aq", "man docker",
    "cat <<'EOF'\ndocker rm $(docker ps -aq)\nEOF"
  ],
  "tree": "command command_substitution"
}
```

The matrix has six catches (direct, `container`, backticks, double quotes, and two wrapped) and seven passes (another
subcommand, `--rm` on `docker run`, a single-quoted look-alike, `echo`, an unrelated docker command, `man`, a quoted
heredoc). Message: "Do not remove containers from a substitution. List them with `docker ps -a`, then
`docker rm <name>` for the ones you mean." Settings: deny, `retry: same-command` (removing everything is sometimes
exactly what was asked), global scope, no modes.
