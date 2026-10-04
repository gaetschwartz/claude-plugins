"""Typed ast-grep rules for a guardrails rule: the program/args shorthand and the user's own match.ast, made
tolerant of how a command is spelled. All of it is data for ast-grep; nothing here reads shell syntax."""

from __future__ import annotations

import re
import string
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import policy

if TYPE_CHECKING:
    from ast_grep_py import Config, Rule, SgNode

PLAIN_NAME = frozenset(string.ascii_letters + string.digits + "_.+-")
SUBSTITUTIONS = ("command_substitution", "process_substitution")
ASSIGNMENT_OR_REDIRECT = ("variable_assignment", "file_redirect", "herestring_redirect", "heredoc_redirect")
ASSIGNMENTS = 3


def name_regex(names: Sequence[str]) -> str:
    """A command word that is one of these names, allowing quotes around it and a directory before it."""
    alternatives = "|".join(re.escape(name) for name in names)
    return f"^[\"']?(?:[^\\s]*/)?(?:{alternatives})[\"']?$"


def command_named(names: Sequence[str]) -> Rule:
    return {"kind": "command", "has": {"field": "name", "regex": name_regex(names)}}


def orphan_name(names: Sequence[str]) -> Rule:
    """A command name the parser found but could not build a command around: a `command_name` directly in an ERROR
    node, or a bare word there that is not an argument of a word before it."""
    found = name_regex(names)
    bare: Rule = {"kind": "word", "regex": found, "inside": {"kind": "ERROR"},
                  "not": {"follows": {"kind": "word", "stopBy": "neighbor"}}}
    return {"any": [{"kind": "command_name", "regex": found, "inside": {"kind": "ERROR"}}, bare]}


def shorthand(match: policy.Match) -> Rule | None:
    """The rule for program/args, or None when the rule has no program."""
    if not match.program:
        return None
    extra: list[Rule] = [{"regex": match.args}] if match.args else []
    rule: Rule = {"all": [command_named(match.program), *extra]}
    return rule if extra else {"any": [rule, orphan_name(match.program)]}


def widen(rule: Any) -> Any:
    """A pattern ending in ` $$$` also selects the command when it has no arguments (the bare hole never does)."""
    if isinstance(rule, list):
        return [widen(item) for item in rule]
    if not isinstance(rule, dict):
        return rule
    out = {key: value if key == "pattern" else widen(value) for key, value in rule.items()}
    pattern = rule.get("pattern")
    words = pattern.split() if isinstance(pattern, str) else []
    if len(words) < 2 or words[-1] != "$$$":
        return out
    head = pattern.rstrip()[:-3].rstrip()
    bare = {"context": head, "selector": "command"} if single_command(head) is not None else head
    either = {"any": [{"pattern": pattern}, {"pattern": bare}]}
    keep = {k: v for k, v in out.items() if k in ("stopBy", "field")}
    rest = {k: v for k, v in out.items() if k not in ("pattern", "stopBy", "field")}
    return {"all": [either, rest] if rest else [either], **keep}


def single_command(text: str) -> SgNode | None:
    """The `command` node when the text parses as exactly one command (no list, pipeline, redirect or compound)."""
    from ast_grep_py import SgRoot

    kids = SgRoot(text, "bash").root().children()
    return kids[0] if len(kids) == 1 and kids[0].kind() == "command" else None


def simple_command_name(text: str) -> str | None:
    """The command name when a pattern is exactly one simple command with arguments, else None."""
    text = text.strip()
    command = single_command(text) if " " in text else None
    if command is None or any(child.kind() in ASSIGNMENT_OR_REDIRECT for child in command.children()) \
            or command.find({"rule": {"any": [{"kind": kind} for kind in SUBSTITUTIONS]}}) is not None:
        return None
    name = command.field("name")
    return name.text() if name is not None and name.text() and set(name.text()) <= PLAIN_NAME else None


def loosened(node: Any) -> Any:
    """The rule with the single-command patterns at its top level (through any/all) made tolerant: name in any
    spelling (quotes, a directory) and up to three leading assignments. None when it has none. Anything else is left
    for ast-grep as written."""
    if not isinstance(node, dict):
        return None
    out = dict(node)
    found = False
    for key in ("any", "all"):
        if isinstance(node.get(key), list):
            forms = [loosened(member) for member in node[key]]
            found = found or any(form is not None for form in forms)
            out[key] = [form or member for form, member in zip(forms, node[key], strict=True)]
    pattern = out.pop("pattern", None)
    in_context = isinstance(pattern, dict) and pattern.get("selector") == "command"
    text = pattern.get("context") if in_context else pattern
    name = simple_command_name(text) if isinstance(text, str) else None
    if name is None:
        if pattern is not None:
            out["pattern"] = pattern
    else:
        found = True
        checked = [{"has": {"field": "name", "regex": name_regex([name])}}, *out.get("all", [])]
        rest = text.strip().partition(" ")[2]
        variants = []
        for skipped in range(ASSIGNMENTS + 1):
            shaped = "$_A " * skipped + f"$_N {rest}"
            variants.append({**out, "pattern": {**pattern, "context": shaped} if in_context else shaped,
                             "all": checked})
        out = {"any": variants}
    return out if found and out.get("any", True) else None


def found_rule(rule: policy.Rule) -> Rule | None:
    """Where this rule matches: its program/args shorthand and its ast rule, spelling-tolerant."""
    ast = rule.match.ast
    tolerant = loosened(ast) if ast else None
    parts = [part for part in (shorthand(rule.match), widen(tolerant or ast) if ast else None) if part]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"any": parts}


def config_of(rule: policy.Rule) -> Config | None:
    """The ast-grep config that finds this rule's matches, or None when the rule has no parsed matcher."""
    found = found_rule(rule)
    return {"rule": found} if found else None


def regex_config(rule: policy.Rule) -> Config | None:
    """The config that finds `match.regex` in the whole command's text (ast-grep's regex engine runs in linear time)."""
    pattern = rule.match.regex
    return {"rule": {"kind": "program", "regex": pattern}} if pattern else None
