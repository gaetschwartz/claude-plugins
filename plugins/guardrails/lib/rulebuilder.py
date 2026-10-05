"""Typed ast-grep rules for a guardrails rule: its match with the command, assignment and wrapper atoms expanded and
its single-command patterns made tolerant of how a command is spelled. All of it is data for ast-grep; nothing here
reads shell syntax."""

from __future__ import annotations

import re
import string
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import policy
import redirects

if TYPE_CHECKING:
    from ast_grep_py import Config, Rule, SgNode

PLAIN_NAME = frozenset(string.ascii_letters + string.digits + "_.+-")
SUBSTITUTIONS = ("command_substitution", "process_substitution")
ASSIGNMENT_OR_REDIRECT = ("variable_assignment", "file_redirect", "herestring_redirect", "heredoc_redirect")
ASSIGNMENTS = 3
WRAPPERS = ("sudo", "doas", "env", "timeout", "nice", "nohup", "time", "command", "exec", "builtin", "stdbuf", "setsid",
            "ionice", "xargs", "watch")
ATOMS = ("command", "wrapper", "assignment", "statement", "redirect", "discards")
NESTED = ("not", "all", "any", "stopBy", "inside", "has", "follows", "precedes")
SIBLINGS = ("precedes", "follows")
POSITIONS = (*SIBLINGS, "inside", "nthChild")
PARAMS = ("stopBy", "field")
SAME_NODE = ("not", "all", "any")
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
    if key == "statement":
        if not isinstance(value, dict) or not value:
            raise policy.Invalid(f"'{where}.statement' must be a non-empty rule object")
        return redirects.statement(expanded(value, f"{where}.statement"))
    if key == "redirect":
        return redirects.redirect(value, where)
    if key == "discards":
        return redirects.discards(value, where)
    return assignment(value, f"{where}.assignment")


def stance(rule: object) -> set[str]:
    """What the rule asks of its own node: "position" (relations to siblings and ancestors) and "content"."""
    if isinstance(rule, list):
        return set().union(*(stance(item) for item in rule))
    if not isinstance(rule, dict):
        return {"content"}
    found: set[str] = set()
    for key, value in rule.items():
        if key in PARAMS:
            continue
        if key in POSITIONS:
            found.add("position")
        elif key in SAME_NODE:
            found |= stance(value)
        else:
            found.add("content")
    return found


def split_positions(node: dict[str, Any], out: dict[str, Any], where: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The expanded keys that are about where the statement sits, and the rest, which are about the command."""
    moved: dict[str, Any] = {}
    kept: dict[str, Any] = {}
    for key, value in out.items():
        kind = stance(node[key]) if key in SAME_NODE else {"position"} if key in POSITIONS else {"content"}
        if kind == {"position"}:
            moved[key] = value
        elif "position" in kind:
            raise policy.Invalid(f"'{where}.{key}' mixes relations (precedes, follows, inside, nthChild) with other "
                                 f"conditions next to a 'command': split it into two objects")
        else:
            kept[key] = value
    return moved, kept


def transparent(node: dict[str, Any], out: dict[str, Any], atoms: Sequence[Any], core: dict[str, Any], where: str,
                statement: bool) -> dict[str, Any]:
    """The rule for a `command` that is also matched as the body of a redirect wrapper, the relations that say where
    the statement sits (precedes, follows, inside, nthChild) being judged from the wrapper. It matches the wrapper
    when `statement` (the command is looked for among siblings or children), else the command."""
    moved, kept = split_positions(node, out, where)
    inner: dict[str, Any] = {**kept, "all": [*atoms, *kept.get("all", [])]}
    if statement:
        return {"any": [core, {"kind": redirects.WRAPPER, **moved, "has": {"field": "body", "all": [inner]}}]}
    return {"any": [core, {**inner, "inside": {"kind": redirects.WRAPPER, **moved}}]}


def expanded(node: object, where: str = "match", at: str = "node") -> Any:
    """The rule object with every atom replaced by the ast-grep rule it stands for; raises policy.Invalid on a key that
    is neither ast-grep's nor an atom, or on a malformed atom. `at` is what the node is for its parent: "sibling" (the
    target of precedes or follows), "child" (the target of has) or "node" (the same node, or an ancestor)."""
    if isinstance(node, list):
        return [expanded(item, f"{where}[{i}]", at) for i, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    if unknown := set(node) - GRAMMAR_KEYS - set(ATOMS) - {"args"}:
        raise policy.Invalid(f"unknown field {where}.{min(unknown)}: {policy.UNKNOWN_FIELD_HINT}")
    if "args" in node and "command" not in node:
        raise policy.Invalid(f"'{where}.args' only narrows a 'command' atom next to it")
    about_command = "command" not in node and "statement" not in node

    def child(key: str) -> str:
        if key in SIBLINGS:
            return "sibling"
        if key == "has":
            return "child"
        return at if key == "stopBy" or (key in SAME_NODE and about_command) else "node"

    out = {key: expanded(value, f"{where}.{key}", child(key)) if key in NESTED else value
           for key, value in node.items() if key not in ATOMS and key not in ("args", *PARAMS)}
    params = {key: expanded(node[key], f"{where}.{key}", child(key)) if key in NESTED else node[key]
              for key in PARAMS if key in node}
    atoms = [atom(key, node, where) for key in ATOMS if key in node]
    if not isinstance(out.get("all", []), list):
        raise policy.Invalid(f"'{where}.all' must be a list of rules")
    core: dict[str, Any] = {**out, "all": [*atoms, *out.get("all", [])]} if atoms else dict(out)
    if "command" in node:
        beside = any(key in node for key in SIBLINGS)
        if at == "sibling" or (at == "child" and beside):
            return {**transparent(node, out, atoms, core, where, True), **params}
        if beside:
            return {**transparent(node, out, atoms, core, where, False), **params}
    return {**core, **params}


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
