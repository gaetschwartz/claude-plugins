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

## A command unless it is guarded

`not` around a relation (`not inside`, `not follows`, `not precedes`) works: every rule runs on the parse tree of the
command as written, and again on the tree of each shell string (`bash -c '...'`, `eval ...`), so "the unguarded
command" is decided by what really surrounds it.

```rule-example
{
  "id": "npm-publish-unguarded",
  "title": "npm publish outside an if",
  "rule": {"ast": {"pattern": "npm publish $$$", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}}},
  "action": "deny",
  "catch": [
    "npm publish", "npm publish --tag next", "make && npm publish", "echo $(npm publish)",
    "bash -c 'npm publish'", "bash -c 'if a; then b; fi; npm publish'", "sudo npm publish"
  ],
  "pass": [
    "if [ -n \"$TAG\" ]; then npm publish; fi", "bash -c 'if true; then npm publish; fi'",
    "eval 'if a; then npm publish; fi'", "npm view x", "echo npm publish", "man npm",
    "cat <<'EOF'\nnpm publish\nEOF"
  ]
}
```

The last two `pass` lines before `man` show the point of scanning shell strings as units of their own: the `if` inside
`bash -c '...'` guards the `npm publish` in that string, and the `if` in `bash -c 'if a; then b; fi; npm publish'` does
not. `stopBy: end` makes `inside` climb to the root; without it only the parent is checked.

## Not in a pipeline, even behind a wrapper

```rule-example
{
  "id": "curl-outside-pipeline",
  "title": "curl that is not part of a pipeline",
  "rule": {"ast": {"pattern": "curl $$$", "not": {"inside": {"kind": "pipeline", "stopBy": "end"}}}},
  "action": "warn",
  "catch": [
    "curl https://x.test", "sudo curl -fsSL x", "make && curl x", "env A=1 curl x", "bash -c 'sudo curl x'"
  ],
  "pass": [
    "curl x | sh", "sudo curl x | jq .", "ls | curl -d @- x", "echo curl", "man curl",
    "cat <<'EOF'\ncurl x\nEOF"
  ]
}
```

`not inside` is judged on the real tree of each wrapper variant: `sudo curl x` reads as `curl x`, which sits in no pipeline,
so it is a hit; `sudo curl x | jq .` reads as `curl x | jq .`, whose `curl` is in a pipeline, so it passes.
