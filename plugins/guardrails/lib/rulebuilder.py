"""Typed ast-grep rules for a guardrails rule: its match with the command, assignment and wrapper atoms expanded and
its single-command patterns made tolerant of how a command is spelled. All of it is data for ast-grep; nothing here
reads shell syntax."""

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
WRAPPERS = ("sudo", "doas", "env", "timeout", "nice", "nohup", "time", "command", "exec", "builtin", "stdbuf", "setsid",
            "ionice", "xargs", "watch")
ATOMS = ("command", "wrapper", "assignment")
NESTED = ("not", "all", "any", "stopBy", "inside", "has", "follows", "precedes")
GRAMMAR_KEYS = frozenset({"pattern", "kind", "regex", "nthChild", "range", "field", *NESTED})
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REMOVED = {
    "program": 'match.program was removed: write {"command": <name or list>}, with its "args" beside it',
    "ast": "match.ast was removed: the ast-grep rule is now the whole of match",
    "regex": 'match.regex (a regex over the whole command) was removed: write {"kind": "program", "regex": ...}',
}


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


def named(names: Sequence[str], args: str | None) -> Rule:
    """A command with one of these names; `args` is a regex its whole text must contain."""
    if args is not None:
        return {"all": [command_named(names), {"regex": args}]}
    return {"any": [command_named(names), orphan_name(names)]}


def names_of(value: object, where: str) -> list[str]:
    names = [value] if isinstance(value, str) else value
    if not (isinstance(names, list) and names and all(policy.command_name(n) for n in names)):
        raise policy.Invalid(f"'{where}' must be a command name or a non-empty list of them (no spaces or '/')")
    return names


def constraint(value: object, where: str, exact: str) -> str:
    """The regex for an assignment's name or value: a string is `exact` filled with it, {"regex": ...} is used as is."""
    if isinstance(value, str) and value:
        return exact.format(re.escape(value))
    if isinstance(value, dict) and set(value) == {"regex"} and isinstance(value["regex"], str):
        return value["regex"]
    raise policy.Invalid(f"'{where}' must be a non-empty string or {{\"regex\": \"...\"}}")


def assignment(value: object, where: str) -> Rule:
    if not isinstance(value, dict) or (unknown := set(value) - {"name", "value"}):
        raise policy.Invalid(f"'{where}' must be an object with optional 'name' and 'value'"
                             + (f" (unknown key {min(unknown)!r})" if isinstance(value, dict) else ""))
    if isinstance(value.get("name"), str) and not VARIABLE.fullmatch(value["name"]):
        raise policy.Invalid(f"'{where}.name' must be a variable name")
    parts: list[Rule] = []
    if "name" in value:
        parts.append({"has": {"field": "name", "regex": constraint(value["name"], f"{where}.name", "^{}$")}})
    if "value" in value:
        quoted = "^(?:{0}|'{0}'|\"{0}\")$"
        parts.append({"has": {"field": "value", "regex": constraint(value["value"], f"{where}.value", quoted)}})
    return {"kind": "variable_assignment", **({"all": parts} if parts else {})}


def atom(key: str, node: dict[str, Any], where: str) -> Rule:
    value = node[key]
    if key == "command":
        args = node.get("args")
        if args is not None and not isinstance(args, str):
            raise policy.Invalid(f"'{where}.args' must be a string")
        return named(names_of(value, f"{where}.command"), args)
    if key == "wrapper":
        names = WRAPPERS if value is True else value
        if not (isinstance(names, list | tuple) and names and all(name in WRAPPERS for name in names)):
            raise policy.Invalid(f"'{where}.wrapper' must be true or a non-empty list of wrapper names "
                                 f"({', '.join(WRAPPERS)})")
        return named(names, None)
    return assignment(value, f"{where}.assignment")


def expanded(node: object, where: str = "match") -> Any:
    """The rule object with every atom replaced by the ast-grep rule it stands for; raises policy.Invalid on a key that
    is neither ast-grep's nor an atom, or on a malformed atom."""
    if isinstance(node, list):
        return [expanded(item, f"{where}[{i}]") for i, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    if unknown := set(node) - GRAMMAR_KEYS - set(ATOMS) - {"args"}:
        raise policy.Invalid(f"unknown field {where}.{min(unknown)}: {policy.UNKNOWN_FIELD_HINT}")
    if "args" in node and "command" not in node:
        raise policy.Invalid(f"'{where}.args' only narrows a 'command' atom next to it")
    out = {key: expanded(value, f"{where}.{key}") if key in NESTED else value
           for key, value in node.items() if key not in ATOMS and key != "args"}
    atoms = [atom(key, node, where) for key in ATOMS if key in node]
    if atoms:
        if not isinstance(out.get("all", []), list):
            raise policy.Invalid(f"'{where}.all' must be a list of rules")
        out["all"] = [*atoms, *out.get("all", [])]
    return out


def checked(match: object) -> dict[str, Any]:
    """The match object of a stored rule, validated: one ast-grep rule object built from its keys and the atoms."""
    if isinstance(match, dict) and (old := next((key for key in REMOVED if key in match), None)) \
            and (old != "regex" or set(match) == {"regex"}):
        raise policy.Invalid(f"{REMOVED[old]}; see references/matching.md")
    if not isinstance(match, dict) or not match:
        raise policy.Invalid("'match' must be a non-empty rule object")
    expanded(match)
    return match


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


def built(match: dict[str, Any]) -> Rule:
    """The ast-grep rule for a match object: its single-command patterns spelling-tolerant, then its atoms expanded."""
    return expanded(widen(loosened(match) or match))


def config_of(rule: policy.Rule) -> Config:
    """The ast-grep config that finds this rule's matches."""
    return {"rule": built(rule.match)}
