# AST cookbook: captures in messages

A `capture` atom binds the node a rule matched to a name, and the message quotes it as `{NAME}`. It is how a rule built
from atoms (`command`, `statement`, ...) puts what the agent typed into its advice, as a `pattern` rule does with `$X`
metavariables. Semantics (the expansion, the validation, the caps) are in [matching.md](../matching.md#the-atoms) and
[matching.md](../matching.md#messages-cases-and-placeholders). Every block is checked by `tests/test_ast_examples.py`
against the real engine: each `catch` command matches, each `pass` command does not, each `says` entry renders exactly
that text.

## The command that ends a pipeline

```rule-example
{
  "id": "capture-the-last-pipeline-stage",
  "title": "a pipeline ending in tail or head, naming that last command",
  "rule": {"kind": "pipeline",
           "has": {"capture": {"command": ["tail", "head"], "nthChild": {"position": 1, "reverse": true}},
                   "name": "LAST", "field": "name"}},
  "action": "warn",
  "message": "A pipeline's status is its last command's (`{LAST}`), so a failure of the first command goes unseen.",
  "catch": ["make | tail -3", "make 2>&1 | grep x | head -5", "bash -c 'make | tail'", "make | tail -2 >log"],
  "pass": ["tail -f x", "make | grep x", "tail x | make", "make"],
  "says": {"make | foo | tail -3 && x": "A pipeline's status is its last command's (`tail`), so a failure of the first command goes unseen.",
           "make | tail -3": "A pipeline's status is its last command's (`tail`), so a failure of the first command goes unseen.",
           "a | b | c | head": "A pipeline's status is its last command's (`head`), so a failure of the first command goes unseen.",
           "make | tail -2 >log": "A pipeline's status is its last command's (`tail`), so a failure of the first command goes unseen."}
}
```

The capture sits under `has` of the pipeline, around the last stage (`nthChild` 1 counted from the end). `field:
"name"` binds the stage's name instead of the whole stage, so `{LAST}` is `tail`, not `tail -3`. A redirect on the
last stage (`>log`) does not change the name. When `&&`, `||` or `;` follows a pipeline of three or more stages,
tree-sitter nests every stage after the first, and what follows, in a `list` inside the first stage's pipeline; the
pipeline that matches is then that inner one, and its last stage is still the real last stage. A rule that must judge
the first stage (`pipe-status` exempts display-only first commands) goes up from the inner pipeline through those
lists: `{"inside": {"kind": "pipeline", "stopBy": {"not": {"kind": "list"}}}}`.

`pipe-status` also leaves a status alone when the consumer is not a verdict. Two consumers are exempt, judged on the
node after the `&&`/`||` that follows the pipeline (the exemption is written twice: from the pipeline, and from a
pipeline that ends a list, subshell or `{ }` group that the operator follows):

- A display echo: an `echo` or `printf` with no expansion (`simple_expansion`, `expansion`, `command_substitution`,
  `arithmetic_expansion`, none below it), whose arguments are a separator or a label, by regex on the command's text. A
  separator is two or more of `-=*_~`, or one to six `#`. The argument is empty (`echo`, `echo ""`, `printf '\n'`), or
  starts (after `\n` escapes and spaces) with a separator and continues with any text that has no quote, backslash
  (other than `\n`, `\t`) or backtick, quoted or not: `echo ---`, `echo "====="`, `echo "=====PYPROJECT====="`,
  `echo "--- listing ---"`, `echo "### Section"`, `printf '=== %s ===\n' x` (extra `printf` arguments must be plain
  words). Words with no separator in front (`echo done`, `echo "ok ---"`) report a result and stay denied, as does a
  `$?` anywhere in the command.
- `|| true` and `|| :`: the status is discarded on purpose.

`pipe-status` is two rules over the same pipelines. `pipe-status` denies when the consumer is `||`, an adjacent `$?`, an
`if`/`while`/`until`/`elif` condition, `!`, or an `&&` whose next command is consequential. `pipe-status-chain` warns on
every other `&&` whose next command is not consequential and not a wrapper; it has `wrappers: false`, because the
wrapper variants read the words of `sudo ls` as commands of their own (`pipe-status` judges `sudo systemctl restart x`
through its variant). The exemptions above apply to both. A command is consequential when it changes state or reports
success, judged by a regex on the text of the node after `&&` (a pipeline there is judged by its first command), anchored
at its start after any `VAR=x` assignments, an optional directory and quote:

- `echo` or `printf`: it claims success after a masked failure (a separator echo is exempt above).
- `git` that changes the repository: `add`, `commit`, `push`, `pull`, `merge`, `rebase`, `reset`, `checkout`, `switch`,
  `cherry-pick`, `revert`, `restore`, `clean`, `rm`, `mv`, `am`, `apply`, `tag <name>`, `stash` (bare, `push`, `pop`,
  `apply`, `drop`, `save`, `clear`). `status`, `log`, `diff`, `show`, `branch`, `fetch`, `stash list` and `tag` alone read
  or only update remote refs, so they warn.
- `docker`, `podman`, `docker-compose`, `podman-compose` (with `compose`, `container`, `image`, `buildx`): `up`, `run`,
  `restart`, `start`, `stop`, `rm`, `push`, `build`, `down`, `kill`, `rmi` change containers or images; `ps`, `logs` and
  `inspect` read.
- `systemctl` `start`, `restart`, `stop`, `reload`, `enable`, `disable` (and `mask`, `unmask`, `kill`, `try-restart`,
  `reload-or-restart`): service state.
- `kill`, `rm`, `mv`, `cp`, `install`, `ln`: files and processes; `ssh`, `scp`, `rsync`: they act on another host.
- `npm`, `pnpm`, `yarn`, `bun`, `cargo`, `twine`, `gem` `publish`; `kubectl`, `terraform`, `tofu` `apply`, `delete`,
  `destroy`; `fly`, `flyctl`, `kamal`, `wrangler`, `vercel`, `netlify`, `firebase`, `serverless`, `sls`, `cdk`, `just`,
  `make`, `npm`, `pnpm`, `yarn`, `bun` with a `deploy` subcommand, and a command named `deploy`: they release.
- Running what was just built: `cargo run`, a path starting `./` or `../` or through `target/debug/` or
  `target/release/`, and `bash`, `sh`, `zsh`, `python`, `python3`, `node`, `ruby`, `perl` with a script argument (not
  `-c` or `-m`).

Everything else after `&&` (another pipeline, `grep`, `rg`, `sed`, `cat`, `ls`, `find`, `cargo build`, `cargo test`,
`just`, `cd`, `git status`) only warns. In a longer chain the status gates every later command: `pipe-status` also denies
when the pipeline is the first of its list and a consequential command comes after the second `&&`.

`$?` counts as a consumer only when it reads the pipeline's own status: it appears in the next command, separated by
`;` or a newline (a comment may sit between), or chained directly with `&&`/`||`. The pipeline may also be the last
command of a `( )` or `{ }` group that the next command follows. A `$?` after another command in between
(`make | tail; echo x; echo $?`), after `&`, or in `just test > log; echo "exit=$?"` (no pipeline) is not the
pipeline's status and does not fire. A pipeline that ends a list (`x && make | tail; echo $?`) is not followed.

## A field of the matched node

```rule-example
{
  "id": "capture-a-command-name",
  "title": "any of several commands, quoted by the name it was written with",
  "rule": {"capture": {"command": ["pkill", "killall"]}, "name": "KILLER", "field": "name"},
  "action": "deny",
  "message": "Do not run {KILLER}: it matches by name and takes unrelated processes with it. Find the PID first.",
  "catch": ["pkill node", "sudo killall -9 node", "/usr/bin/pkill -f vite", "bash -c 'pkill x'"],
  "pass": ["pgrep node", "echo pkill", "man killall", "kill 123"],
  "says": {"pkill node": "Do not run pkill: it matches by name and takes unrelated processes with it. Find the PID first.",
           "/usr/bin/killall x": "Do not run /usr/bin/killall: it matches by name and takes unrelated processes with it. Find the PID first."}
}
```

Without `field` the capture binds the node itself (the whole command, `pkill node`); with it, the child of that field.
The name is quoted as written, directory and quotes included.

A `field` capture adds a condition: the node must have a child in that field, or the sub-rule cannot match at all (a
`pipeline` has no `name`, so `{"capture": {"kind": "pipeline"}, "name": "N", "field": "name"}` never fires). Run
`guardrails rule ast` to see which kinds have the field, or drop `field` to bind the node itself. A redirect wrapper has
no `name` of its own and is read through its `body`.

## The same name in several branches

```rule-example
{
  "id": "capture-through-any",
  "title": "du with or without a path: the path is quoted when there is one",
  "rule": {"any": [{"command": "du",
                    "has": {"capture": {"kind": "word", "regex": "^[^-]", "not": {"regex": "^[0-9]+[KMGTB]?$"}},
                            "name": "PATH"}},
                   {"command": "du"}]},
  "action": "warn",
  "message": "Prefer `dust` over `du`: `dust -d 1`.",
  "messages": [
    {"when": {"matches": {"has": {"kind": "word", "regex": "^[^-]", "not": {"regex": "^[0-9]+[KMGTB]?$"}}}},
     "text": "Prefer `dust` over `du`: `dust -d 1 {PATH}`."}
  ],
  "catch": ["du -sh /tmp/x", "du", "sudo du -d 1 ~", "du -sh a b"],
  "pass": ["dust -d 1 .", "df -h", "echo du", "man du"],
  "cases": {"du -sh /tmp/x": 1, "du -sh": null},
  "says": {"du -sh /tmp/x": "Prefer `dust` over `du`: `dust -d 1 /tmp/x`.",
           "du -d 1 ~": "Prefer `dust` over `du`: `dust -d 1 ~`.",
           "du -sh a b": "Prefer `dust` over `du`: `dust -d 1 a`.",
           "du -sh": "Prefer `dust` over `du`: `dust -d 1`."}
}
```

`any` tries its branches in order and the bindings are those of the branch that matched, so the more specific branch
(with the capture) goes first and the plain `du` is the fallback. A name bound in one branch only renders empty when
another branch matched, which is why the text that quotes `{PATH}` is a case picked by the shape that binds it: the
rule's own `message` reads fine without it. A name may repeat across branches of an `any`; twice on one path
(`all`, a nested relation) is an error, and so is a capture under `not` or a placeholder nothing binds.

## A command that a redirect wrapped

```rule-example
{
  "id": "capture-a-redirect-transparent-command",
  "title": "a make step that a tail follows, quoted even when it has a redirect",
  "rule": {"kind": "program",
           "has": {"capture": {"command": "make", "precedes": {"command": "tail"}}, "name": "STEP"}},
  "action": "warn",
  "message": "Read the log, not the pipe: `{STEP}` is followed by a tail.",
  "catch": ["make\ntail -3", "make -j4 >o 2>&1\ntail -3", "bash -c 'make\ntail'", "make all\nls\nmake\ntail"],
  "pass": ["make", "tail -3\nmake", "make; ls", "ls\ntail"],
  "says": {"make\ntail -3": "Read the log, not the pipe: `make` is followed by a tail.",
           "make -j4 >o 2>&1\ntail -3": "Read the log, not the pipe: `make -j4 >o 2>&1` is followed by a tail."}
}
```

A `command` next to `precedes` or `follows` also matches when a redirect wrapped it, and the capture around it binds
whichever node matched: the command, or the statement (`make -j4 >o 2>&1`, the wrapper with its redirects) when the
command is the rule of a sibling relation. Add `"field": "name"` to bind `make` in both shapes.
