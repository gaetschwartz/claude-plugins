# AST cookbook: lists, subshells and groups

`a && b` and `a || b` are a `list` (left-nested: `a && b && c` is `list(list(a, b), c)`); `;` and newlines make
sibling nodes under `program` or the enclosing block. Order-dependent rules use `follows` with `stopBy: end` among
siblings. Every block is checked by `tests/test_ast_examples.py` against the real engine: each `catch` command
matches, each `pass` command does not. Rule syntax and semantics: [matching.md](../matching.md).

What `rule test` tags `wrapped`:

| shape | tag |
|---|---|
| command alone, in a `;` `&&` `\|\|` list, a subshell `( )`, a group `{ ...; }`, a function body, an `if` or loop, backgrounded `&` | direct |
| a pipeline member, `$( )` or backticks, `<( )`, through `sudo` / `env` / `timeout`, a `bash -c` string | wrapped |

A direct hit beats a wrapped one on the same rule.

## Order-dependent lists

```rule-example
{
  "id": "git-after-cd",
  "title": "git after cd",
  "rule": {
    "pattern": "git $$$",
    "follows": {
      "any": [{"pattern": "cd $$$"}, {"kind": "list", "has": {"pattern": "cd $$$", "stopBy": "end"}}],
      "stopBy": "end"
    }
  },
  "action": "warn",
  "catch": [
    "cd repo && git status", "cd repo; git status", "cd a && make && git diff", "(cd repo && git log)",
    "cd repo && sudo git log", "bash -c 'cd repo && git log'", "cd repo\ngit status"
  ],
  "pass": [
    "git -C repo status", "git status", "cd repo", "cd repo && ls", "git status && cd repo",
    "(cd repo); git status", "echo \"cd repo && git status\"", "man git",
    "cat <<EOF\ncd repo && git status\nEOF"
  ],
  "tree": "list command"
}
```

`follows` has two alternatives because `cd a && make && git diff` nests the `cd` inside a sibling `list`; the second
alternative looks for a `cd` anywhere in that sibling. `(cd repo); git status` passes: the `cd` ran in a subshell and
is not a sibling. `git status && cd repo` passes: `follows` is directional. The message should name `git -C <dir>`.

## Only when something did not come first

```rule-example
{
  "id": "push-without-pull",
  "title": "git push with no git pull before it",
  "rule": {
    "pattern": "git push $$$",
    "not": {
      "follows": {
        "any": [{"pattern": "git pull $$$"}, {"kind": "list", "has": {"pattern": "git pull $$$", "stopBy": "end"}}],
        "stopBy": "end"
      }
    }
  },
  "action": "warn",
  "catch": [
    "git push", "git fetch; git push origin main", "make && git push", "(git push)", "bash -c 'git push'",
    "git push && git pull", "cd repo && git push"
  ],
  "pass": [
    "git pull; git push", "git pull && git push", "git pull --rebase && make && git push",
    "bash -c 'git pull && git push'", "git status", "echo git push", "man git"
  ]
}
```

`not follows` reads "no earlier sibling is a `git pull`". `git push && git pull` is still caught: `follows` is
directional. The same shape with `precedes` says "nothing later is ...". The second alternative inside `follows` is
needed for the same reason as in the `git after cd` rule: `a && b && git push` nests the earlier commands in a sibling
`list`.

## Every list shape

```rule-example
{
  "id": "terraform-auto-approve",
  "title": "terraform apply -auto-approve",
  "rule": {"pattern": "terraform apply $$$", "has": {"regex": "^-auto-approve$"}},
  "action": "deny",
  "catch": [
    "terraform apply -auto-approve", "make && terraform apply -auto-approve",
    "make || terraform apply -auto-approve", "make; terraform apply -auto-approve",
    "(terraform apply -auto-approve)", "{ terraform apply -auto-approve; }",
    "terraform apply -auto-approve | tee log", "sudo terraform apply -auto-approve"
  ],
  "pass": [
    "terraform apply", "terraform apply -var x=-auto-approve", "terraform plan",
    "echo \"terraform apply -auto-approve\"", "man terraform",
    "cat <<EOF\nterraform apply -auto-approve\nEOF"
  ]
}
```

One pattern covers `;`, `&&`, `||`, a subshell and a group; all of them count as direct. The pipeline member and the
`sudo` form are found too and tagged wrapped. `-var x=-auto-approve` passes: `has` matches a whole word, not text
inside one.
