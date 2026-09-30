"""Run the stdlib matcher and the ast-compiled program/args/builtin matcher side by side and list disagreements.

    uv run --quiet --no-project --with ast-grep-py==<pin> python tests/parity_study.py [--fuzz N] [--seed S]
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "tests")]

import astrun
import matching
import policy
from test_corpus import ALLOW, DENY, WARN

RULES = {rid: policy.with_defaults({"match": match, "message": "m"}) for rid, match in {
    "find-fd": {"program": "find"},
    "grep-rg": {"builtin": "grep-recursive"},
    "no-pkill": {"program": ["pkill", "killall"]},
    "kill-9": {"program": "kill", "args": "(^|\\s)-(9|KILL|SIGKILL)(\\s|$)|(^|\\s)-s\\s+(9|KILL|SIGKILL)(\\s|$)"},
    "strings": {"program": "strings"},
    "binary": {"program": ["otool", "nm", "objdump"]},
    "grep-args": {"program": "grep", "args": "-r"},
    "echo-anchored": {"program": "echo", "args": "^x"},
}.items()}

PROGRAMS = ["pkill", "find", "grep", "kill", "strings", "echo", "nm"]
PREFIXES = ["", "sudo ", "sudo -u bob ", "env A=1 ", "timeout 5 ", "nice -n 5 ", "nohup ", "command ", "command -v ",
            "xargs ", "xargs -0 ", "time ", "/usr/bin/", "FOO=1 ", "exec ", "ionice -c 3 "]
TAILS = ["", " x", " -r foo .", " -9 1", " -rn x", " -e r f", " 'quoted arg'", ' "a b"', " -s KILL 1", " -d recurse x",
         " -- -r", " x y z", " -er f", " -A3 -r x", " -m1 -r x", " --recursive x", " -R .", " -e foo -r .",
         " -d skip -r x", " --directories=recurse x", " -nr pat dir", " -s 9 -d recurse x", " ${X}", " $(z)"]
CONTEXTS = ["{c}", "a; {c}", "a && {c}", "a | {c}", "{c} | b", "echo $({c})", 'echo "$({c})"', "echo '{c}'",
            "bash -c '{c}'", 'bash -c "{c}"', "if {c}; then x; fi", "for f in $({c}); do y; done",
            "cat <<EOF\n{c}\nEOF", "echo `{c}`", "({c})", "{{ {c}; }}", "x=$({c})", "echo {c} > out", "{c} > out 2>&1",
            "eval '{c}'", "watch -n 1 '{c}'", "cat <(echo) <({c})", "sh -c 'a; {c}'", "while {c}; do :; done",
            "echo hi\n{c}"]


def verdicts(command: str) -> dict[str, str]:
    matching.PARITY = False
    stdlib = matching.evaluate(command, RULES)
    matching.PARITY = True
    try:
        compiled = matching.evaluate(command, RULES)
    finally:
        matching.PARITY = False
    if stdlib.degraded or compiled.degraded or compiled.invalid:
        raise SystemExit(f"ast engine unavailable or rules invalid: {compiled.degraded} {compiled.invalid}")
    return {rid: f"{stdlib.kinds[rid]}/{compiled.kinds[rid]}" for rid in RULES}


def corpus() -> list[str]:
    return [*DENY, *ALLOW, *WARN]


def fuzz(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [rng.choice(CONTEXTS).format(c=f"{rng.choice(PREFIXES)}{rng.choice(PROGRAMS)}{rng.choice(TAILS)}")
            for _ in range(count)]


def run(commands: list[str]) -> list[tuple[str, str, str, bool]]:
    """(command, rule, "stdlib/ast", matched differs) for every rule whose two engines disagree."""
    out = []
    for command in commands:
        for rid, pair in verdicts(command).items():
            old, new = pair.split("/")
            if old != new:
                out.append((command, rid, pair, (old == "None") != (new == "None")))
    return out


def main() -> int:
    astrun.INPROCESS = True
    count, seed = 3000, 1
    args = sys.argv[1:]
    if "--fuzz" in args:
        count = int(args[args.index("--fuzz") + 1])
    if "--seed" in args:
        seed = int(args[args.index("--seed") + 1])
    for title, commands in (("corpus", corpus()), ("fuzz", fuzz(count, seed))):
        found = run(commands)
        hard = [d for d in found if d[3]]
        print(f"## {title}: {len(commands)} commands x {len(RULES)} rules, {len(hard)} verdict differences, "
              f"{len(found) - len(hard)} direct/wrapped differences")
        for command, rid, pair, hard_diff in found:
            print(f"{'VERDICT' if hard_diff else 'KIND   '} {rid:14} {pair:16} {command!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
