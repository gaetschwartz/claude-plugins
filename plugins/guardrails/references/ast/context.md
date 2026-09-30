# AST cookbook: context

Rules about where a command sits: nested in a substitution, a loop, an `if`, a pipeline. Anchor the rule on the
dangerous command (the `pattern`) and put the surroundings in `inside`. Every block is checked by
`tests/test_ast_examples.py` against the real engine: each `catch` command matches, each `pass` command does not. Rule
syntax and semantics: [matching.md](../matching.md).

## A command only when nested

```rule-example
{
  "id": "pgrep-nested",
  "title": "pgrep only when nested",
  "rule": {
    "ast": {
      "pattern": "pgrep $$$",
      "inside": {
        "any": [
          {"kind": "command_substitution"},
          {"kind": "pipeline"},
          {"kind": "if_statement"},
          {"kind": "while_statement"},
          {"kind": "for_statement"},
          {"kind": "list"}
        ],
        "stopBy": "end"
      }
    }
  },
  "action": "deny",
  "catch": [
    "kill $(pgrep -f vite)", "pgrep -f vite | xargs echo", "sudo sh -c 'echo $(pgrep x)'",
    "if pgrep -q vite; then echo up; fi", "until ! pgrep -x vite; do sleep 1; done",
    "echo \"pids: $(pgrep x)\"", "make && pgrep -f x", "echo `pgrep x`"
  ],
  "pass": [
    "pgrep -xl vite", "pgrep", "sudo pgrep -xl vite", "man pgrep", "echo \"pgrep -f x | head\"",
    "echo '$(pgrep x)'", "cat <<'EOF'\n$(pgrep x)\nEOF", "pgrep -xl vite; echo done"
  ],
  "tree": "command_substitution command command_name"
}
```

`stopBy: end` makes `inside` climb every ancestor; the default checks only the parent, so a `pgrep` in a loop body
(parent `do_group`) would be missed. `"$(...)"` runs, `'$(...)'` and a quoted heredoc are data, which is why the
single-quoted and heredoc commands pass. A naive `program: pgrep` also blocks the harmless `pgrep -xl vite`, and a
`regex` also blocks `man pgrep`. Drop `list` from the kinds to allow `make && pgrep x`.

## A command inside a loop

```rule-example
{
  "id": "sleep-poll",
  "title": "sleep in a polling loop",
  "rule": {"ast": {"pattern": "sleep $$$", "inside": {"kind": "while_statement", "stopBy": "end"}}},
  "action": "deny",
  "catch": [
    "while ! curl -sf localhost:3000; do sleep 1; done", "until curl -sf localhost:3000; do sleep 2; done",
    "sudo bash -c 'while :; do sleep 1; done'", "x=$(while true; do sleep 1; done)",
    "for i in 1 2; do while true; do sleep 1; done; done"
  ],
  "pass": [
    "sleep 5", "for i in 1 2 3; do sleep 1; done", "echo \"while true; do sleep 1; done\"", "man sleep",
    "cat <<EOF\nwhile true; do sleep 1; done\nEOF", "while true; do echo x; done"
  ],
  "tree": "while_statement do_group command"
}
```

`until` is a `while_statement` in tree-sitter-bash (there is no `until_statement` kind; a rule naming it does not
compile). The condition and the body are both inside the loop. A `for` loop is not a polling loop, so it passes.
A bare `sleep 5` passes because nothing contains it.

## A denied command inside any substitution

```rule-example
{
  "id": "deny-in-subst",
  "title": "kill-by-name inside $( )",
  "rule": {
    "ast": {
      "any": [{"pattern": "pkill $$$"}, {"pattern": "killall $$$"}],
      "inside": {"kind": "command_substitution", "stopBy": "end"}
    }
  },
  "action": "deny",
  "catch": [
    "echo $(pkill x)", "echo \"$(killall node)\"", "echo `pkill x`", "sudo bash -c 'x=$(pkill y)'",
    "FOO=$(pkill x) make"
  ],
  "pass": [
    "pkill x", "echo pkill", "echo '$(pkill x)'", "man killall", "cat <<'EOF'\n$(pkill x)\nEOF",
    "x=$(pgrep y)"
  ],
  "tree": "command_substitution command"
}
```

`any` lists the commands, one `inside` covers them all. `$(...)` and backticks are the same kind, and the
substitution is found through `sudo bash -c '...'` because the shell string is parsed again. Top-level `pkill x` is not
matched here on purpose: that is another rule's job. A regex like `\$\(.*pkill` would fire on `echo '$(pkill x)'`.

## Negated context does not work

`not` around a relation to an ancestor or sibling (`not inside`, `not follows`, `not precedes`) gives false matches:
every simple command is also re-checked on its own, without its surroundings, so the "unguarded" command is always
found. Verified:

```json
{"ast": {"pattern": "npm publish $$$", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}}}
```

matches `if [ -n "$TAG" ]; then npm publish; fi` as well as `npm publish`. State the dangerous context positively (an
`inside` or `follows` rule), or split the rule. `not has` looks inside the matched command only, so it is reliable:
see the `--force-with-lease` exception in [flags.md](flags.md).
