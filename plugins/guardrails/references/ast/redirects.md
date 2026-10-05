# AST cookbook: redirects

A command with a redirect (`>/dev/null`, `2>&1`, `&>file`, `<in`) is not a bare `command` node: the parser wraps it in a
`redirected_statement` whose `body` is the command and whose other children are the redirects, left to right. Its
siblings (`&&`, `||`, `;`, the other stages of a pipeline) are then the wrapper's. The parser attaches the redirect to
the whole list or pipeline before it (`a && b >f` wraps `a && b`, `a | b 2>&1 | c` wraps `a | b`), so run
`guardrails rule ast '<command>'` when the shape matters. Every block is checked by `tests/test_ast_examples.py` against the
real engine: each `catch` command matches, each `pass` command does not. Atom semantics: [matching.md](../matching.md).

A `command` atom written next to `precedes` or `follows` in the same object also matches the wrapped command, with the
sibling relations judged from the wrapper. The `statement` atom does the same for any node, the `redirect` atom reads one
redirect, and `discards` tells which stream ends at `/dev/null`.

## A status that does not mean what it says, with or without a redirect

```rule-example
{
  "id": "no-pgrep-status",
  "title": "pgrep's exit status used in a condition",
  "rule": {
    "any": [
      {"command": "pgrep", "inside": {"kind": "list"}, "precedes": {"regex": "^(&&|\\|\\|)$"}},
      {"command": "pgrep", "inside": {"statement": {"kind": "list"}, "precedes": {"regex": "^(&&|\\|\\|)$"},
                                      "stopBy": "end"}},
      {"command": "pgrep", "inside": {"kind": "negated_command", "stopBy": "end"}},
      {"command": "pgrep", "inside": {"kind": "if_statement", "field": "condition", "stopBy": "end"}},
      {"command": "pgrep", "inside": {"kind": "while_statement", "field": "condition", "stopBy": "end"}}
    ]
  },
  "action": "deny",
  "catch": [
    "pgrep -f X >/dev/null || echo gone", "pgrep -f X || echo gone", "pgrep -x X && echo up",
    "pgrep -x X 2>/dev/null && echo up", "pgrep -f X >/dev/null 2>&1 && echo up", "pgrep -f X &>/dev/null || echo gone",
    "if pgrep -f x >/dev/null; then echo up; fi", "while pgrep -f x >/dev/null 2>&1; do sleep 1; done",
    "for i in 1 2; do pgrep -f x >/dev/null || { echo ended; break; }; done", "! pgrep -f x >/dev/null",
    "sudo pgrep -f X >/dev/null || echo gone"
  ],
  "pass": [
    "pgrep -fl X >/dev/null 2>&1; echo done", "pgrep -f X > out.txt; cat out.txt", "pgrep -xl X", "echo gone || echo pgrep",
    "pgrep -f X >/dev/null", "man pgrep"
  ],
  "tree": "list redirected_statement file_redirect"
}
```

The first alternative is the bare case: `pgrep` is a child of the list and precedes `&&`. With `>/dev/null` the list's
child is the wrapper, and the `command` atom, written with its `precedes` in one object, also matches the wrapper that
precedes the `&&`. The second alternative reaches a list further up (`a && pgrep x || b`); there the redirect may wrap the
whole inner list, so the list is written as a `statement`. The `if` and `while` conditions and `!` need nothing: they
cross the wrapper with `stopBy: end`. `;` or a redirect to a file followed by `;` is not a status, so it passes.

## Which stream a redirect discards

```rule-example
{
  "id": "silent-build",
  "title": "a build whose output is thrown away",
  "rule": {"statement": {"command": "cargo"}, "discards": "all"},
  "action": "warn",
  "catch": [
    "cargo build >/dev/null 2>&1", "cargo build &>/dev/null", "cargo build >/dev/null 2>/dev/null",
    "cargo build 2>/dev/null 1>&2", "cargo build 2>&1 >/dev/null 2>&1", "cargo build >&/dev/null && echo ok",
    "cargo test >/dev/null 2>&1; echo $?", "sudo cargo build >/dev/null 2>&1"
  ],
  "pass": [
    "cargo build 2>&1 >/dev/null", "cargo build >/dev/null", "cargo build 2>/dev/null", "cargo build >out.log 2>&1",
    "cargo build >/dev/null 2>&1 >out.log", "cargo build", "echo cargo >/dev/null 2>&1"
  ],
  "tree": "redirected_statement command file_redirect"
}
```

The redirects are applied from left to right, as the shell does. `>/dev/null 2>&1` sends stdout to the null device and then
stderr to wherever stdout goes, so both are discarded. `2>&1 >/dev/null` copies stderr onto the terminal first, then
discards only stdout: stderr is still visible, so it passes. `{"discards": "stdout"}` and `{"discards": "stderr"}`
are the one-stream forms; `>/dev/null 2>&1 >out.log` leaves stdout in a file, so only stderr is discarded. The rule is
static ast-grep: the order is a `follows` relation between the redirect nodes. Two limits: a chain of `N>&M` copies is
followed three levels deep and no further, and a stream moved through another descriptor (`3>/dev/null 2>/dev/null
1>&3`, which discards stdout via fd 3) is not followed: that command is not seen as discarding stdout.

## A redirect to a place

```rule-example
{
  "id": "tmp-output",
  "title": "output written into /tmp",
  "rule": {"kind": "redirected_statement",
           "has": {"redirect": {"fd": 1, "to": {"regex": "^[\"']?/tmp/"}}}},
  "action": "warn",
  "catch": [
    "make >/tmp/build.log", "make >>/tmp/build.log", "make 2>&1 >/tmp/out", "make &>/tmp/build.log",
    "make >\"/tmp/build.log\"", "make > /tmp/build.log 2>&1 && echo ok", "a | b >/tmp/x", "bash -c 'make >/tmp/build.log'"
  ],
  "pass": [
    "make >build.log", "make >/dev/null", "make 2>/tmp/err", "make </tmp/in", "make", "echo >/var/tmp/x",
    "echo /tmp/x"
  ],
  "tree": "redirected_statement file_redirect"
}
```

`fd` names the stream: `1` also covers `&>` and `>&file`, which write stdout too, while `2>/tmp/err` and `</tmp/in` do not.
`to` is the target as written, quotes included, so the regex allows one. Redirects are children of the wrapper, never of
the command, so a `redirect` goes under `has` of a `statement` or of a `redirected_statement`.

## A pipeline as one statement

```rule-example
{
  "id": "pipeline-status",
  "title": "a pipeline whose status is read",
  "rule": {"statement": {"kind": "pipeline"}, "precedes": {"regex": "^(&&|\\|\\|)$"}},
  "action": "warn",
  "catch": [
    "make | tail >/dev/null && echo ok", "make | tail && echo ok", "make | tail 2>&1 || echo failed",
    "make | head 2>/dev/null && echo ok", "bash -c 'make | tail >log && echo ok'"
  ],
  "pass": [
    "make | tail; echo done", "make | tail >/dev/null; echo done", "make && tail f", "make | tail", "make >log && tail log",
    "echo make | tail"
  ],
  "tree": "list redirected_statement pipeline"
}
```

`a | b >f` is a `redirected_statement` whose body is the whole pipeline, so a `precedes` written on the pipeline never
saw the `&&` after it. The `statement` atom is the pipeline when it has no redirect and the wrapper when it has one, and
the relation next to it is judged from there. `make >log && tail log` has no pipeline at all.
