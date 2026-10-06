# AST cookbook: flags and arguments

Flags are sibling words of the command. Patterns are literal, order-sensitive and anchored at the start (`rm -rf` is
not `rm -fr` or `rm -r -f`), so match flag spellings with `has` + `regex` on a word, combine several words with `all`,
allow alternatives with `any`, and carve out exceptions with `not` + `has`. The `command` atom selects the command by
name in any spelling; its `args` is a regex over the command's whole text.
Every block is checked by `tests/test_ast_examples.py` against the real engine: each `catch` command matches, each
`pass` command does not. Rule syntax and semantics: [matching.md](../matching.md).

## Exception by flag

```rule-example
{
  "id": "no-force-push-any-form",
  "title": "force push, any spelling, not with lease",
  "rule": {
    "kind": "command",
    "has": {"field": "name", "regex": "^git$"},
    "all": [{"has": {"regex": "^push$"}}, {"has": {"regex": "^(--force|-[A-Za-z]*f[A-Za-z]*|\\+.+)$"}}],
    "not": {"has": {"regex": "^--force-with-lease"}}
  },
  "action": "deny",
  "catch": [
    "git push --force", "git -C ../repo push -f", "git push -uf origin x", "git push origin +main",
    "sudo git push --force"
  ],
  "pass": [
    "git push", "git push --force-with-lease origin main", "git push -u origin feat-f",
    "echo \"git push -f\"", "man git-push", "cat <<'EOF'\ngit push -f\nEOF"
  ],
  "tree": "command command_name word"
}
```

`-C dir` before `push`, clustered `-uf`, `--force` and a `+refspec` are all found; `--force-with-lease` is the
allowed spelling (`not` + `has` only looks inside this command, so it is reliable). The `git push -u origin feat-f`
pass shows why the flag regex is anchored to a whole word. Known gap: `--force-with-lease --force` is a real force
push that this rule allows.

## Flag clusters and targets

```rule-example
{
  "id": "rm-recursive-root",
  "title": "recursive rm of / or home",
  "rule": {
    "kind": "command",
    "has": {"field": "name", "regex": "^rm$"},
    "all": [
      {"has": {"regex": "^(--recursive|-[A-Za-z]*[rR][A-Za-z]*)$"}},
      {"has": {"regex": "^\"?(/|~|\\$HOME|\\$\\{HOME\\})/?\\*?\"?$"}}
    ]
  },
  "action": "deny",
  "catch": [
    "rm -rf /", "rm -r -f $HOME", "rm -rf \"$HOME\"", "rm -rf /*", "sudo rm -rf /",
    "rm --recursive --force ${HOME}"
  ],
  "pass": [
    "rm -rf ./build", "rm -rf /tmp/x", "rm -f /", "echo \"rm -rf /\"", "man rm", "cat <<EOF\nrm -rf /\nEOF"
  ]
}
```

Two `has` clauses under `all`: a recursive flag in any spelling (`-rf`, `-fr`, `-r -f`, `--recursive`) and a target of
`/`, `~`, `$HOME`, `"$HOME"`, `${HOME}`, optionally with `/` or `*` after. The regexes run on node text, so
`simple_expansion` and quoted `string` nodes both work. `rm -f /` has no recursive flag and passes; `rm -rf /tmp/x`
passes because the whole word is anchored.

```rule-example
{
  "id": "tail-follow", "title": "tail -f in any spelling, by the flag atom", "rule": {"command": "tail", "flag": ["f", "F", "follow"]},
  "action": "warn",
  "catch": ["tail -f x", "tail -n0 -F x", "tail --follow=name x", "sudo tail -fq x"],
  "pass": ["tail x", "tail -n 5 x", "tail -- -f", "echo tail -f"]
}
```

## Any of several dangerous arguments

```rule-example
{
  "id": "docker-run-host-access",
  "title": "docker run with host access",
  "rule": {
    "pattern": "docker run $$$",
    "has": {
      "any": [
        {"regex": "^--privileged"},
        {"regex": "^(-v|--volume)=?/:"},
        {"regex": "^/:", "follows": {"regex": "^(-v|--volume)$"}},
        {"regex": "^--mount(=|$)"}
      ]
    }
  },
  "action": "deny",
  "catch": [
    "docker run --privileged alpine", "docker run -v /:/host alpine", "docker run --volume=/:/host alpine",
    "sudo docker run --privileged img"
  ],
  "pass": [
    "docker run alpine", "docker run -v /data:/data alpine", "echo \"docker run --privileged x\"",
    "man docker", "cat <<EOF\ndocker run --privileged x\nEOF"
  ]
}
```

`any` inside `has`: `--privileged`, a `-v` / `--volume` value starting with `/:` (glued, `=`-joined, or as the next
word through `follows`, which here means the immediately preceding word) and `--mount`. Bind mounts of other paths
pass. `docker container run` and `docker -H h run` are not covered; add patterns for them if they matter.

## A flag

```rule-example
{
  "id": "kill-sigkill",
  "title": "kill -9",
  "rule": {"pattern": "kill $$$", "has": {"regex": "^-(9|KILL|SIGKILL)$"}},
  "action": "warn",
  "catch": [
    "kill -9 1234", "kill -KILL 1234", "sudo kill -9 1234"
  ],
  "pass": [
    "kill 1234", "kill -0 1234", "kill -TERM 1234", "echo \"kill -9 1\"", "cat <<EOF\nkill -9 1\nEOF"
  ],
  "tree": "command command_name number"
}
```

tree-sitter reads `-9` as a `number`, not a `word`; a `regex` on the node text does not care, a `kind: word` filter
would miss it. `-0` (existence check) and `-TERM` pass.

## A command unless it carries a flag

```rule-example
{
  "id": "docker-run-without-rm",
  "title": "docker run that leaves its container behind",
  "rule": {"command": "docker", "args": "(^|\\s)run(\\s|$)", "not": {"has": {"regex": "^--rm$"}}},
  "action": "warn",
  "catch": [
    "docker run alpine", "docker run -it ubuntu bash", "/usr/local/bin/docker run x", "sudo docker run alpine",
    "make && docker run x"
  ],
  "pass": [
    "docker run --rm alpine", "sudo docker run --rm -it x", "docker ps", "echo docker run x", "man docker",
    "cat <<EOF\ndocker run x\nEOF"
  ]
}
```

`not` + `has` looks at the matched command's own words, so the exception holds behind `sudo` too (the variant is the
same command). The atom reads `docker` in any spelling and `args` says which subcommand; `docker container run` is a
second spelling to add with `any` if it matters.

## Arguments produced by a substitution

```rule-example
{
  "id": "kill-by-substitution",
  "title": "kill fed by $( )",
  "rule": {"pattern": "kill $$$", "has": {"kind": "command_substitution", "stopBy": "end"}},
  "action": "deny",
  "catch": [
    "kill $(pidof vite)", "kill \"$(cat /tmp/pid)\"", "kill `pidof vite`", "sudo kill $(pidof vite)"
  ],
  "pass": [
    "kill 1234", "kill $PID", "echo \"kill $(pidof x)\"", "echo 'kill $(pidof x)'",
    "cat <<'EOF'\nkill $(pidof x)\nEOF"
  ],
  "tree": "command command_substitution"
}
```

A hole cannot sit inside a substitution: the pattern `kill $($$$)` never matches anything (verified, it passes `kill
$(pidof vite)`). Express it with `has` of kind `command_substitution` and `stopBy: end`, which also finds the
substitution inside `"$(...)"` and in backticks. Quoted `'$(...)'` and `echo` arguments are data.
