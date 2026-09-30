# AST rule cookbook

Worked `match.ast` rules, each tested against the real engine. Read only the file whose shape matches your rule; rule
syntax and semantics are in [matching.md](../matching.md) and the full procedure (when to use `ast` at all, the test
matrix, the pitfalls) in [writing-rules.md](../writing-rules.md).

| If you want to ... | Read |
|---|---|
| allow a command at top level but catch it nested in `$( )`, a pipeline, `if` or a loop; `sleep` in a polling loop; any command inside a substitution | [context.md](context.md) |
| catch what a command is piped into or fed by (`curl \| sh`, `xargs kill`, `printenv \| curl`); `follows` / `precedes` | [pipelines.md](pipelines.md) |
| match a flag in any spelling or cluster, an argument value, an exception (`--force-with-lease`), `git -C dir push -f`, `rm -rf /`, `docker run --privileged`, `kill -9`, `kill $( )` | [flags.md](flags.md) |
| depend on order in `&&` / `;` lists (`cd x && git ...`); understand subshells, groups and what `wrapped` means | [lists.md](lists.md) |
| match `sudo`, `bash -c`, `eval`; how wrappers and shell strings are looked through; single versus double quotes; heredocs and `<<-` as data | [wrappers.md](wrappers.md) |

## Method in three lines

1. Run `guardrails rule ast '<a command the rule must catch>'` and read the node kinds; never guess them.
2. Write the narrowest `pattern` anchored on the dangerous command; add one relation (`inside`, `has`, `follows`,
   `precedes`) per fact about its surroundings, with `stopBy: end` unless the nearest level is what you mean.
3. Test at least three commands to catch (one wrapped) and three to pass (a look-alike, `man X`, `echo "X"`, a heredoc
   mentioning X) with `guardrails rule test`, and tighten until nothing mismatches.

## Example format

Each example is a `rule-example` block: the `match` object (`rule`), the `action`, the commands to `catch` and to
`pass`, and optionally the node kinds that matter (`tree`). `tests/test_ast_examples.py` runs every block through the
same code path as `rule test`.
