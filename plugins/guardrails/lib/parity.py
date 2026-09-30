"""Compile the program/args/builtin matchers into ast-grep rules (used by the parity study, GUARDRAILS_PARITY=ast)."""

from __future__ import annotations

import re
from typing import Any

import policy

META = re.compile(r"([\\.+*?()|\[\]{}^$])")
GREPS = "^(.*/)?(grep|egrep|fgrep)$"
RECURSIVE_WORD = "^(-[A-Za-z&&[^efmABCdD]]*[rR][A-Za-z]*|--recursive|--dereference-recursive|--directories=recurse)$"


def command_named(names: list[str]) -> dict[str, Any]:
    body = "|".join(META.sub(r"\\\1", n) for n in names)
    return {"kind": "command", "has": {"field": "name", "regex": f"^(.*/)?({body})$"}}


def grep_recursive() -> dict[str, Any]:
    before_dashdash = {"not": {"follows": {"regex": "^--$", "stopBy": "end"}}}
    return {"all": [
        command_named(["grep", "egrep", "fgrep"]),
        {"any": [
            {"has": {"kind": "word", "regex": RECURSIVE_WORD, **before_dashdash}},
            {"has": {"kind": "word", "regex": "^-d$", "precedes": {"kind": "word", "regex": "^recurse$"},
                     **before_dashdash}},
        ]},
    ]}


def compile_rule(rule: policy.Rule) -> dict[str, Any] | None:
    """An ast rule for the program/args/builtin part of a rule, or None when it has none."""
    match = policy.view(rule, "match")
    parts: list[dict[str, Any]] = []
    programs = policy.programs_of(rule)
    if programs:
        parts.append(command_named(programs))
    if match.get("args"):
        parts.append({"kind": "command", "regex": match["args"]})
    if match.get("builtin") == "grep-recursive":
        parts.append(grep_recursive())
    if not programs and not match.get("builtin"):
        return None
    return parts[0] if len(parts) == 1 else {"all": parts}
