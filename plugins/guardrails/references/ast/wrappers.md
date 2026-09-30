# AST cookbook: wrappers, shells, quoting and heredocs

The engine parses the command as written, and again for the script of every shell string (`bash -c`, `sh -c`, `eval`,
...), unquoted and scanned as a unit of its own; `rule ast '<cmd>'` prints every unit. A command `pattern` (and a
`kind: command` rule with a `has` on `field: name`) is also tried behind a wrapper (`sudo env timeout nice ionice nohup
time command exec builtin stdbuf setsid xargs watch`, plus your own): `pkill $$$` matches `sudo -u bob pkill -f x`. Every
block is checked by `tests/test_ast_examples.py` against the real engine: each `catch` command matches, each `pass`
command does not. Rule syntax and semantics: [matching.md](../matching.md).

`match.program` matches the wrapper names themselves too (`program: sudo` matches `sudo ls`), and any word of a wrapper
command that equals the program name.

## Matching the wrapper itself

```rule-example
{
  "id": "no-sudo",
  "title": "any use of sudo",
  "rule": {"ast": {"pattern": "sudo $$$"}},
  "action": "deny",
  "catch": [
    "sudo ls", "make && sudo make install", "FOO=1 sudo ls", "env A=1 sudo ls", "bash -c 'sudo ls'",
    "echo x | sudo tee f", "x=$(sudo ls)"
  ],
  "pass": [
    "ls", "man sudo", "echo sudo", "echo \"sudo ls\"", "visudo", "pseudo ls", "cat <<EOF\nsudo ls\nEOF"
  ]
}
```

`sudo $$$` is found at the start, after `&&`, after an assignment, behind `env`, inside `bash -c` and in a
substitution. `visudo`, `pseudo` and `man sudo` are other names or plain arguments, so they pass.

## Shell strings

```rule-example
{
  "id": "no-eval",
  "title": "eval",
  "rule": {"ast": {"pattern": "eval $$$"}},
  "action": "warn",
  "catch": [
    "eval \"$cmd\"", "eval ls", "make && eval $(ssh-agent)", "sudo eval x", "bash -c 'eval x'",
    "x=$(eval ls)"
  ],
  "pass": [
    "ls", "man eval", "echo eval", "echo 'eval x'", "evaluate x", "cat <<'EOF'\neval x\nEOF"
  ]
}
```

`eval`'s arguments are code, so `eval $(ssh-agent)` is a hit, while `echo 'eval x'` is data. A single-quoted string
is data everywhere except as the argument of a shell (`bash -c 'eval x'` is parsed again). Double quotes keep `$( )`
live, single quotes do not (see [context.md](context.md)).

## Heredocs are data

```rule-example
{
  "id": "inline-python-heredoc",
  "title": "interpreter reading a heredoc",
  "rule": {
    "ast": {
      "any": [{"pattern": "python $$$"}, {"pattern": "python3 $$$"}],
      "inside": {"kind": "redirected_statement", "has": {"kind": "heredoc_redirect"}}
    }
  },
  "action": "warn",
  "catch": [
    "python3 - <<'EOF'\nprint(1)\nEOF", "python3 <<EOF\nprint(1)\nEOF",
    "python3 - <<-EOF\n\tprint(1)\n\tEOF", "sudo python3 <<EOF\nprint(1)\nEOF",
    "bash -c 'python3 <<EOF\nprint(1)\nEOF'"
  ],
  "pass": [
    "python3 script.py", "python3 -c 'print(1)'", "cat <<EOF\npython3 x\nEOF",
    "cat <<-EOF\n\tpython3 x\n\tEOF", "echo \"python3 <<EOF\"", "python3 < in.py", "man python3"
  ],
  "tree": "redirected_statement command heredoc_redirect heredoc_body"
}
```

The body of a heredoc is never a command, quoted delimiter or not; `cat <<EOF` mentioning `python3` passes, and `<<-`
(tab-stripping) changes nothing for the tree. The rule is about the redirect: the interpreter's node sits inside a
`redirected_statement` that has a `heredoc_redirect`. The default `inside` looks at the parent only: in `make && python
<<EOF` or `x | python <<EOF` the parser attaches the heredoc to the whole list or pipeline, so those forms are missed, and
`stopBy: end` would instead flag `python3 s.py && cat <<EOF` (both verified). Read the tree before choosing.

## Not looked through

`ssh host sudo x`, `find . -exec sudo x {} \;`, script files, `python -c`, `watch 'sudo x'`
(a string argument of a wrapper other than `bash -c`, `script -c` and `eval`), `echo sudo x | sh`, obfuscated or dynamic names (`$'s\x75do'`, `s''udo`,
`$CMD`), and wrappers the list does not know (declare them with `guardrails wrapper add`). Unbalanced quotes and
unterminated heredocs give a partial tree (`ERROR` nodes): the commands the parser could still read are matched.
