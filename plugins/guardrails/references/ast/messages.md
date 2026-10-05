# AST cookbook: conditions and message cases

A rule's `when` decides whether the rule applies at all on this machine and for this call, before any command is read.
Its `messages` cases pick the text from the shape of the command that was caught, and placeholders put the binary that
was found and the words the pattern captured into the text. Semantics (every atom, the matched node, the caps) are in
[matching.md](../matching.md#conditions-when) and [matching.md](../matching.md#messages-cases-and-placeholders).
Every block is checked by `tests/test_ast_examples.py` against the real engine: each `catch` command matches, each
`pass` command does not, each `cases` entry picks that case (`null`: the rule's own `message`) and each `says` entry
renders exactly that text.

## Advice per command shape

```rule-example
{
  "id": "force-push-advice-per-shape",
  "title": "git push --force, a different text for main and for a wrapped push",
  "rule": {"all": [{"pattern": "git push $$$"}, {"has": {"regex": "^(-f|--force)$"}}]},
  "action": "deny",
  "message": "Do not force-push: use --force-with-lease, which refuses to overwrite commits you have not seen.",
  "messages": [
    {"when": {"matches": {"has": {"regex": "^(main|master)$"}}},
     "text": "Never force-push main or master: push a revert commit instead."},
    {"when": {"wrapped": true},
     "text": "A force-push behind a wrapper or in a shell string is still a force-push: run git push --force-with-lease directly."}
  ],
  "catch": ["git push -f origin main", "git push --force origin feat", "sudo git push --force", "bash -c 'git push -f'"],
  "pass": ["git push origin main", "git push --force-with-lease", "echo git push -f", "man git-push"],
  "cases": {"git push -f origin main": 1, "sudo git push --force": 2, "git push --force origin feat": null,
            "bash -c 'git push -f origin master'": 1}
}
```

Cases are tried in order and the first whose `when` holds wins, so the `main` case beats the wrapped one for `bash -c
'git push -f origin master'`. `matches` tests the node the rule matched (here the `git push` command) against one more
rule, which may use the `command`, `assignment` and `wrapper` atoms like `match`; `wrapped` is the `wrapped` tag of
`rule test`. A command no case fits gets the rule's own `message`.

## Captured words in the text

```rule-example
{
  "id": "chmod-777-names-the-path",
  "title": "chmod 777, naming the path in the advice",
  "rule": {"pattern": "chmod 777 $TARGET $$$"},
  "action": "deny",
  "message": "Do not make {TARGET} world-writable: chmod 755 {TARGET} (or 644 for a plain file) is enough.",
  "catch": ["chmod 777 /srv/www", "sudo chmod 777 run.sh", "bash -c 'chmod 777 x y'"],
  "pass": ["chmod 755 /srv/www", "chmod 777", "echo chmod 777 x", "chmod -R 755 ."],
  "says": {"chmod 777 /srv/www": "Do not make /srv/www world-writable: chmod 755 /srv/www (or 644 for a plain file) is enough.",
           "chmod 777 a b": "Do not make a world-writable: chmod 755 a (or 644 for a plain file) is enough."}
}
```

`$TARGET` in the pattern is `{TARGET}` in the text; a `$$$REST` capture is its words joined by one space. A capture
the matched node did not bind renders as nothing. Write `{{` and `}}` for a literal brace (`find -exec cmd {{}}`).

## Only where a tool is installed

```rule-example
{
  "id": "find-when-fd-is-installed",
  "title": "find, only where fd is installed, naming the installed binary",
  "rule": {"command": "find"},
  "when": {"bin": ["fd", "fdfind"]},
  "action": "deny",
  "message": "Use {found}: {found} -e py replaces find . -name '*.py'; find . -exec cmd {{}} \\; is {found} -x cmd {{}}.",
  "catch": ["find . -name '*.py'", "sudo find / -type f", "xargs find"],
  "pass": ["fd -e py", "echo find", "man find"]
}
```

The rule is skipped (no telemetry, `inactive here` in `status`) on a machine with neither binary. `{found}` is the first
name of a `bin` atom of the rule's `when` that is on PATH, so a Debian machine that installs fd as `fdfind` is told
`fdfind -e py`; a text that uses `{found}` needs a `bin` atom in the rule's `when`.
