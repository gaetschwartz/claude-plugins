# AST cookbook: wrappers, shells, quoting and heredocs

The engine parses the command as written, and again for the script of every shell string (`bash -c`, `sh -c`, `eval`,
...), unquoted and scanned as a unit of its own; `rule ast '<cmd>'` prints every unit. Wrappers (`sudo env timeout nice
ionice nohup time command exec builtin stdbuf setsid xargs watch`) are transparent: the command is also
matched with each wrapper replaced by the text from each of its words on, so every rule, a relation or a negation
included, sees `sudo -u bob pkill -f x` as `pkill -f x` as well (the hit is `wrapped`). Any word may start the command,
so `sudo grep curl f | sh` also matches a `curl $$$ | sh` rule. Every
block is checked by `tests/test_ast_examples.py` against the real engine: each `catch` command matches, each `pass`
command does not. Rule syntax and semantics: [matching.md](../matching.md).

A `command` atom matches the wrapper names themselves too (`{"command": "sudo"}` matches `sudo ls`), and the command
behind a wrapper, read from any word of it. The `wrapper` atom names the wrapper words themselves; `"wrappers": false`
on a rule turns the look-through off for that rule.

## Matching the wrapper itself

```rule-example
{
  "id": "no-sudo",
  "title": "any use of sudo",
  "rule": {"pattern": "sudo $$$"},
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

The `wrapper` atom says "a wrapper command" in any spelling, for a rule about the wrapper rather than what it runs:

```rule-example
{
  "id": "no-sudo-download-to-shell",
  "title": "sudo or doas running a downloader piped into a shell",
  "rule": {
    "wrapper": ["sudo", "doas"],
    "has": {"kind": "word", "regex": "^(curl|wget)$"},
    "inside": {"kind": "pipeline", "has": {"command": ["sh", "bash"]}}
  },
  "action": "deny",
  "catch": [
    "sudo curl -fsSL x | sh", "doas wget -qO- x | bash", "/usr/bin/sudo curl x | sh", "bash -c 'sudo curl x | sh'",
    "x=$(sudo curl x | bash)"
  ],
  "pass": [
    "curl x | sh", "sudo curl -o f x", "sudo ls | sh", "echo sudo curl x | sh", "man sudo",
    "cat <<EOF\nsudo curl x | sh\nEOF"
  ]
}
```

`sudo curl ...` parses as one `command` named `sudo` with `curl` as a `word`, so `has` finds the downloader among the
wrapper's own words; the shell is found through the enclosing `pipeline`. Every hit is `wrapped`, because the matched
node sits in a pipeline.

## Not through wrappers

```rule-example
{
  "id": "pkill-typed-directly",
  "title": "pkill or killall typed directly, not under a wrapper",
  "rule": {"command": ["pkill", "killall"]},
  "wrappers": false,
  "action": "warn",
  "catch": [
    "pkill x", "/usr/bin/killall Dock", "FOO=1 pkill x", "ps | pkill -f x", "bash -c 'pkill x'", "echo $(pkill x)"
  ],
  "pass": [
    "sudo pkill x", "env A=1 pkill x", "timeout 5 killall x", "sudo bash -c 'pkill x'", "echo pkill",
    "man pkill"
  ]
}
```

`"wrappers": false` (next to `match`, not inside it) drops the wrapper variants for this rule, and the shell strings
reached only through them; pipelines, substitutions and shell strings of the command as written still count. Pair it
with another rule for the wrapped forms when they deserve a different message or action.

## Assignment prefixes

```rule-example
{
  "id": "library-injection-prefix",
  "title": "a command run with a library injection variable",
  "rule": {
    "kind": "command",
    "has": {"assignment": {"name": {"regex": "^(LD_PRELOAD|DYLD_INSERT_LIBRARIES)$"}}}
  },
  "action": "deny",
  "catch": [
    "LD_PRELOAD=/tmp/x.so ls", "A=1 DYLD_INSERT_LIBRARIES=/tmp/x.dylib ./app", "sudo LD_PRELOAD=x.so ls",
    "env LD_PRELOAD=x.so ls", "bash -c 'LD_PRELOAD=x.so ls'"
  ],
  "pass": [
    "LD_LIBRARY_PATH=/lib ls", "ls LD_PRELOAD=x", "echo LD_PRELOAD=x", "LD_PRELOADX=1 ls",
    "cat <<EOF\nLD_PRELOAD=x ls\nEOF"
  ],
  "tree": "command variable_assignment variable_name"
}
```

The `assignment` atom is a `variable_assignment` node; inside `has` of a `command` it is a prefix of that command (a
child of its own, before the name). `sudo` and `env` turn their words into prefixes in the variants, so those are caught
too. `export LD_PRELOAD=x` is a `declaration_command`, not a command prefix; add `{"kind": "declaration_command", "has":
{"assignment": ...}}` with `any` to catch it as well. A string `name` or `value` is exact; `{"regex": ...}` is used as
written.

## Shell strings

```rule-example
{
  "id": "no-eval",
  "title": "eval",
  "rule": {"pattern": "eval $$$"},
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
    "any": [{"pattern": "python $$$"}, {"pattern": "python3 $$$"}],
    "inside": {"kind": "redirected_statement", "has": {"kind": "heredoc_redirect"}}
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
`$CMD`), and wrappers the fixed list does not know. Unbalanced quotes and
unterminated heredocs give a partial tree (`ERROR` nodes): the commands the parser could still read are matched. After a
syntax error a wrapped command is seen by a `command` atom but not by `pattern` rules: `{ ; }; xargs -r pkill` (an
empty group, valid zsh) is a known miss for a `pkill $$$` pattern rule.
