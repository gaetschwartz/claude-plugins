# Writing guardrails rules

The full procedure for one rule. Read it when a rule is anything beyond a `command` atom (with or without `args`), uses
a relation or a regex, covers several behaviors, or has wrapper or quoting concerns, and whenever you are unsure. Field
and matcher semantics are in [matching.md](matching.md) (not repeated here); worked rules by shape are in
[ast/index.md](ast/index.md).

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
- `pkill` and `killall` with the same message and action are one behavior: one rule with
  `{"command": ["pkill", "killall"]}` or an `any`.

A vague message on a rule that covers several things is the usual symptom of a rule that should be split.

## 3. Matcher ladder

Take the first rung that separates your examples. Each rung down costs precision or runtime.

| rung | right when | example |
|---|---|---|
| `command` | the command name alone decides | `{"command": "pkill"}` |
| `command` + `args` | one command's own words decide (regex over its text) | `{"command": "docker", "args": "\\bsystem prune\\b"}` |
| `statement`, `redirect`, `discards` | a redirect decides, or a sibling relation must see through one | [ast/redirects.md](ast/redirects.md) |
| `via`, `flag` | which wrappers or runners a command ran through, or one flag by name | [matching.md](matching.md#the-atoms) |
| `capture` | the message must quote a node the atoms matched (the last stage of a pipeline, an argument) | [ast/captures.md](ast/captures.md) |
| relations | structure decides: nesting, pipelines, order, a flag on one command among several, a wrapper or shell itself | [ast/index.md](ast/index.md) |
| whole-text regex | only raw text can say it, across nodes the tree cannot relate | `{"kind": "program", "regex": "..."}` |

The whole-text regex is last because it reads heredocs and quoted strings as if they were code. Every `regex` is Rust
regex syntax (ast-grep's engine, linear time): no backreferences or look-around, and `rule add/test` refuse them. `args`
is per command and never sees a pipe or a substitution. A `command` atom on the wrapped command needs nothing for
`sudo X`; a rule about the wrapper itself uses the `wrapper` atom, and `"wrappers": false` keeps a rule off the wrapped
forms. Each is a pitfall below. The keys of `match` are ANDed: say "either" with `any`, "except" with `not`.

## 4. Get the tree

For any rule with a relation, before writing it:

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

1. Start with a `command` atom or a `pattern` for the dangerous command (`{"command": "docker"}`, `docker rm $$$`).
   Anchor the rule on it, not on the wrapper or the context: wrappers are transparent (the command is also matched with
   the wrapper replaced), so `sudo X` needs no rule. When the wrapped forms must NOT count, set `"wrappers": false`.
2. Add one relation per fact about the surroundings (`inside` a substitution, `follows` a downloader). Give relations
   `stopBy: end` unless you mean the nearest level.
3. Flags and values: `has` with a `regex` on one word; `all` to require several, `any` for alternatives, `not` + `has`
   for an exception on the same command.
4. A bare `kind` is fine when a pattern cannot say it (`{"kind": "command", "has": {"assignment": {"name":
   "LD_PRELOAD"}}}` for an assignment prefix); a command name is the `command` atom, never a hand-written name regex.
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
wrong, what to run instead. `messageShort` replaces `message` after the first time it was shown in a session. Good: "Do
not kill by name. Look the PID up with `pgrep -xl <name>` as its own command, then `kill <pid>`." Bad: "Blocked."

Make the text fit the command rather than writing one vague text for every shape:

- Name what was caught with a capture: a pattern `chmod 777 $TARGET $$$` and a message "... chmod 755 {TARGET} ..."
  ([ast/messages.md](ast/messages.md)), or, in a rule built from atoms, a `capture` atom around the node (`{"capture":
  {...}, "name": "LAST", "field": "name"}`, [ast/captures.md](ast/captures.md)). An unbound capture is empty, so write
  the sentence so it still reads (a `messages` case on the shape that binds it is the clean way); a placeholder nothing
  binds is a validation error.
- When the advice differs by shape (`find -exec` versus `find -name`, a wrapped versus a direct call), add `messages`
  cases with `matches` / `wrapped` conditions instead of splitting the rule, as long as the action and the matcher
  stay the same; the rule's `message` is the default.
- When the alternative is a tool that may be missing or installed under another name, give the rule a `when` with a
  `bin` atom (`{"bin": ["fd", "fdfind"]}`) and write `{found}` where the binary's name goes.
- Literal braces are `{{` and `}}`; every other `{...}` is a placeholder and a typo makes the rule invalid, which
  `rule test` reports. Placeholder rules are in [matching.md](matching.md#messages-cases-and-placeholders).

Check every case with `rule test`: each caught row says which case it picked (`case N` or `default message`).

## 9. Settings

- **deny versus warn.** deny when the command is harmful or irreversible, or has a clear replacement. warn when it is
  merely discouraged or legitimate in some contexts: the command runs and the agent sees the message once per session.
- **`retry: same-command`** (deny only): the first occurrence is blocked, re-running the identical text passes. Use it
  when a legitimate use exists and the message explains what to double-check; use none when it must never run.
- **`modes` and `agentMayEnable`**: a rule listing a mode is suspended while that mode is on. Declare the mode
  agent-enablable only if the user should be able to say "this session is X" and have the agent switch it on; an agent
  never enables a mode on its own because of a denial.
- **Scopes**: global by default, `--scope project` for one repository, `--scope managed` for enforced rules; a lower layer
  can only tighten an existing id ([matching.md](matching.md#layers)).
- **`when`**: only for a fact about where the rule makes sense (the alternative is installed, a kind of project, an
  OS, the calling tool), never to switch a rule off for a session (that is a mode). Atoms in
  [matching.md](matching.md#conditions-when).

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
| absolute path, quoted name, assignment prefix | `/usr/bin/X`, `"X"`, `FOO=1 X` | caught by a `command` atom; a literal-argument `pattern` does not see through `FOO=1` |
| mention only | `man X`, `echo X`, `echo "X -f"` | passes |
| look-alike name | `visudo` vs `sudo`, `pgrep` vs `pkill` | passes |
| piped into or fed by | `a \| X`, `X \| b` | per the rule's intent |
| Monitor and background use | the same command in a Monitor call, `X &` | rules for Bash also apply to Monitor |

Known limits, where the rule cannot see the command (say so in the description rather than hoping): the list "Cannot be
analysed statically" and the notes on syntax errors and wrapper coarseness in
[matching.md](matching.md#how-a-command-is-looked-through).

## The six pitfalls

Each one makes a rule look right and be wrong.

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

`args` is a regex over one command's own text: `{"command": "curl", "args": "\| *sh"}` never matches `curl x | sh`,
because the pipe is not part of the `curl` command. A question about what a command is piped into, or fed by, needs a
relation (`inside` a `pipeline`, `follows` a `command`) or a whole-text regex. See [ast/pipelines.md](ast/pipelines.md).

### command-and-wrapper-words

A `command` atom matches a command by name, `{"command": "sudo"}` (or `bash`, `env`, `xargs`) included, and also the
command behind a wrapper, read from any of the wrapper's own words: `{"command": "pkill"}` matches `sudo -u bob pkill x`
and, as a known false positive, `sudo grep pkill file` and `command -v pkill`. Test the look-alikes; `"wrappers": false`
drops every wrapped form at once. See [ast/wrappers.md](ast/wrappers.md).

### regex-in-heredocs

A whole-text regex (`{"kind": "program", "regex": "..."}`) reads raw text: it fires on `echo "X"`, `man X` and inside
heredocs and quotes, and its hit on the command as written is never tagged wrapped. Use it only when no tree relation says
it, and then test the mentions.

## Anti-patterns

- A whole-text regex for a structural question ("only inside `$( )`", "as the last pipeline member").
- A hand-written name regex (`"regex": "^xargs\\b"`) where a `command` atom says it: it misses `/usr/bin/xargs` and `'ps'`.
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

Rung 1, `{"command": "docker"}`: blocks `docker ps`. Rung 2, `{"command": "docker", "args": "\\brm\\b.*\\$\\("}`:
`args` is a regex over the command's text, so it catches the real commands but cannot tell a substitution from text and
fires on `docker rm 'a$(b)'` (verified with `rule test`). Rung 4, `{"kind": "program", "regex": "docker (container
)?rm\\b.*\\$\\("}`: catches the real commands, but also `echo "docker rm $(docker ps -aq)"`, a quoted look-alike and a
heredoc that mentions it. So a relation.

`guardrails rule ast 'docker rm -f $(docker ps -aq)'` shows a `command` (name `docker`, words `rm`, `-f`) with a
`command_substitution` child. A substitution can also sit deeper (`"$(...)"`), hence `has` with `stopBy: end`;
`docker container rm` is a second spelling of the same behavior, hence `any`.

```rule-example
{
  "id": "no-docker-rm-by-substitution",
  "title": "docker rm fed by a substitution",
  "rule": {
    "all": [
      {"any": [{"pattern": "docker rm $$$"}, {"pattern": "docker container rm $$$"}]},
      {"has": {"kind": "command_substitution", "stopBy": "end"}}
    ]
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
