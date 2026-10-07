"""Typed ast-grep rules for a guardrails rule: its match with every atom (command, wrapper, assignment, statement,
redirect, discards, via, flag, capture) expanded and its single-command patterns made tolerant of how a command is spelled. All
of it is data for ast-grep; nothing here reads shell syntax."""

from __future__ import annotations

import re
import string
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypedDict

import policy
import redirects

if TYPE_CHECKING:
    from ast_grep_py import Config, Relation, Rule, SgNode

PLAIN_NAME = frozenset(string.ascii_letters + string.digits + "_.+-")
SUBSTITUTIONS = ("command_substitution", "process_substitution")
ASSIGNMENT_OR_REDIRECT = ("variable_assignment", "file_redirect", "herestring_redirect", "heredoc_redirect")
ASSIGNMENTS = 3
WRAPPERS = ("sudo", "doas", "env", "timeout", "nice", "nohup", "time", "command", "exec", "builtin", "stdbuf", "setsid",
            "ionice", "xargs")
SHELLS = ("bash", "sh", "zsh", "dash", "ksh", "script", "su")
JOINED_RUNNERS = ("eval", "ssh", "watch")
RUNNERS = (*SHELLS, *JOINED_RUNNERS)
VIA_CLASSES = ("wrapper", "runner")
MATCHER = "matcher"
MAX_EXPANDED_NODES = 2000
ATOMS = ("command", "wrapper", "assignment", "statement", "redirect", "discards", "via", "flag")
ALWAYS: Rule = {"regex": ""}
NEVER: Rule = {"not": ALWAYS}
SHORT_FLAG = re.compile(r"[A-Za-z]")
LONG_FLAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]+")
CAPTURE_KEYS = frozenset({"capture", "name", "field"})
CAPTURE_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*")
METAVARIABLE: Rule = {"kind": "variable_name", "inside": {"kind": "simple_expansion"},
                      "not": {"inside": {"kind": "heredoc_body", "stopBy": "end"}}}
RELATIONS = ("not", "has", "inside", "follows", "precedes", "stopBy", "statement", "capture")
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


class Marker(TypedDict):
    """A `via` atom left in the rule for the scanner to settle per unit."""

    via: list[str]


def via(value: object, where: str) -> Marker:
    names = [value] if isinstance(value, str) else value
    known = (*WRAPPERS, *RUNNERS, *VIA_CLASSES)
    if not (isinstance(names, list) and names and all(name in known for name in names)):
        raise policy.Invalid(f"'{where}.via' must be a wrapper or runner name, or a non-empty list of them "
                             f"({', '.join(known)})")
    return {"via": names}


CLASSES = {"wrapper": WRAPPERS, "runner": RUNNERS}


def through(names: Sequence[str], chain: Sequence[str]) -> bool:
    """Whether a unit reached through this chain of wrapper and runner names is covered by the `via` names."""
    return any(name in chain or any(step in CLASSES.get(name, ()) for step in chain) for name in names)


def via_names(rule: object) -> frozenset[str]:
    """Every name the `via` atoms of a rule mention."""
    if isinstance(rule, list):
        return frozenset().union(*(via_names(item) for item in rule))
    if not isinstance(rule, dict):
        return frozenset()
    mentioned = frozenset([rule["via"]] if isinstance(rule["via"], str) else rule["via"]) if "via" in rule else frozenset()
    return mentioned.union(*(via_names(value) for value in rule.values()))


def facts(watched: frozenset[str], chain: Sequence[str]) -> frozenset[str]:
    """What the watched names can tell of a chain: the names it holds, and the classes it crosses."""
    return frozenset(name for name in watched if through([name], chain))


def resolve_via(rule: Any, chain: Sequence[str]) -> Any:
    """The rule with each `via` placeholder made always true or always false for a unit reached through this chain."""
    if isinstance(rule, list):
        return [resolve_via(item, chain) for item in rule]
    if not isinstance(rule, dict):
        return rule
    if "via" in rule:
        return ALWAYS if through(rule["via"], chain) else NEVER
    return {key: resolve_via(value, chain) for key, value in rule.items()}


def flag(value: object, where: str) -> Rule:
    """A command with a word after its name that is one of these flags: a short name inside any `-abc` cluster (digits
    are skipped), a long name as `--name` or `--name=...`, the word optionally quoted; nothing after a `--` word counts."""
    names = [value] if isinstance(value, str) else value
    if not (isinstance(names, list) and names and all(isinstance(name, str) for name in names)):
        raise policy.Invalid(f"'{where}.flag' must be a flag name or a non-empty list of them")
    for name in names:
        if name.startswith("-"):
            raise policy.Invalid(f"'{where}.flag' names are written without the dash: {name.lstrip('-')!r}, not "
                                 f"{name!r}")
        if not (SHORT_FLAG.fullmatch(name) or LONG_FLAG.fullmatch(name)):
            raise policy.Invalid(f"'{where}.flag' name {name!r} is neither a short flag (one ASCII letter) nor a long "
                                 "one (letters, digits and _ . + -)")
    shorts = "".join(name for name in names if len(name) == 1)
    longs = "|".join(re.escape(name) for name in names if len(name) > 1)
    forms = ([f"-[A-Za-z0-9]*[{shorts}][A-Za-z0-9]*"] if shorts else []) + (
        [f"--(?:{longs})(?:=(?s:.*))?"] if longs else [])
    word: Relation = {"regex": f"^[\"']?(?:{'|'.join(forms)})[\"']?$", "follows": {"kind": "command_name", "stopBy": "end"},
                  "not": {"follows": {"regex": "^--$", "stopBy": "end"}}}
    return {"kind": "command", "has": word}


def atom(key: str, node: dict[str, Any], where: str) -> Rule | Marker:
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
    if key == "via":
        return via(value, where)
    if key == "flag":
        return flag(value, where)
    return assignment(value, f"{where}.assignment")


def stance(rule: object) -> set[str]:
    """What the rule asks of its own node: "position" (relations to siblings and ancestors) and "content"."""
    if isinstance(rule, list):
        return set().union(*(stance(item) for item in rule))
    if not isinstance(rule, dict):
        return {"content"}
    if "capture" in rule:
        return stance(rule["capture"])
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
    if statement:
        inner: dict[str, Any] = {**kept, "all": [*atoms, *kept.get("all", [])]}
        return {"any": [core, {"kind": redirects.WRAPPER, **moved, "has": {"field": "body", "all": [inner]}}]}
    placed: dict[str, Any] = {"any": [moved, {"inside": {"kind": redirects.WRAPPER, **moved}}]}
    return {**kept, "all": [*atoms, *kept.get("all", []), placed]}


def binding(name: str, field: str | None) -> Any:
    """The rule that binds `$NAME` to the node it is tried on, or to its `field` child (through the body of a redirect
    wrapper, which has none of its own)."""
    if field is None:
        return {"pattern": f"${name}"}
    child: dict[str, Any] = {"field": field, "pattern": f"${name}"}
    wrapped: dict[str, Any] = {"kind": redirects.WRAPPER, "has": {"field": "body", "has": child}}
    return {"any": [{"has": child}, wrapped]}


def captured(node: dict[str, Any], where: str, at: str) -> Rule:
    """A `capture` atom: the sub-rule, and the same node (or its field) bound to the name for message placeholders."""
    if unknown := set(node) - CAPTURE_KEYS:
        raise policy.Invalid(f"unknown field {where}.{min(unknown)}: a capture has 'capture', 'name' and optional "
                             "'field'")
    name, field, inner = node.get("name"), node.get("field"), node["capture"]
    if name == "found":
        raise policy.Invalid(f"'{where}.name' cannot be 'found': {{found}} is reserved for the binary that was found")
    if not (isinstance(name, str) and CAPTURE_NAME.fullmatch(name)):
        raise policy.Invalid(f"'{where}.name' is required and must be an upper-case name such as LAST (letters, digits "
                             "and _, starting with a letter)")
    if "field" in node and not (isinstance(field, str) and FIELD_NAME.fullmatch(field)):
        raise policy.Invalid(f"'{where}.field' must be a lower-case ast-grep field name such as name")
    if not isinstance(inner, dict) or not inner:
        raise policy.Invalid(f"'{where}.capture' must be a non-empty rule object")
    return {"all": [expanded(inner, f"{where}.capture", at), binding(name, field)]}


def binds(node: object, where: str = "match", taken: frozenset[str] = frozenset(), negated: bool = False) -> frozenset[str]:
    """The names the captures in a rule bind; raises policy.Invalid for a capture under `not` and for a name bound
    twice on one path (ast-grep would demand both be the same node). The branches of an `any` are alternatives."""
    if not isinstance(node, dict):
        return frozenset()
    found: set[str] = set()
    for key, value in node.items():
        at = f"{where}.{key}"
        if key == "any" and isinstance(value, list):
            before = frozenset(taken | found)
            for i, member in enumerate(value):
                found |= binds(member, f"{at}[{i}]", before, negated)
        elif key == "all" and isinstance(value, list):
            for i, member in enumerate(value):
                found |= binds(member, f"{at}[{i}]", frozenset(taken | found), negated)
        elif key in RELATIONS:
            found |= binds(value, at, frozenset(taken | found), negated or key == "not")
    if "capture" in node:
        name = node["name"]
        if negated:
            raise policy.Invalid(f"'{where}': a capture under 'not' binds nothing, since a negated match has no node")
        if name in taken | found:
            raise policy.Invalid(f"'{where}.name': {name} is bound twice on one path; use a different name (the same "
                                 "name is fine in different branches of an 'any')")
        found.add(name)
    return frozenset(found)


def pattern_texts(node: object) -> Iterator[str]:
    """The pattern texts of a rule that can bind a name: none from under `not`, whose match binds nothing."""
    if isinstance(node, list):
        for item in node:
            yield from pattern_texts(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern":
                text = value.get("context") if isinstance(value, dict) else value
                if isinstance(text, str):
                    yield text
            elif key != "not":
                yield from pattern_texts(value)


def metavariables(node: object) -> set[str]:
    """The names the patterns of a rule bind, read from the pattern parsed as bash: a `$NAME` that is a simple
    expansion outside a heredoc body (`'$X'`, `${X}` and `$_X` bind nothing)."""
    from ast_grep_py import SgRoot

    found: set[str] = set()
    for text in pattern_texts(node):
        for variable in SgRoot(text, "bash").root().find_all({"rule": METAVARIABLE}):
            if CAPTURE_NAME.fullmatch(variable.text()):
                found.add(variable.text())
    return found


def bound_names(match: dict[str, Any]) -> frozenset[str] | None:
    """Every name a message placeholder can read from this match: its captures and its pattern metavariables. None
    when the matching library is missing, so the patterns cannot be read."""
    try:
        return binds(match) | metavariables(match)
    except ImportError:
        return None


def expanded(node: object, where: str = "match", at: str = "node") -> Any:
    """The rule object with every atom replaced by the ast-grep rule it stands for; raises policy.Invalid on a key that
    is neither ast-grep's nor an atom, or on a malformed atom. `at` is what the node is for its parent: "sibling" (the
    target of precedes or follows), "child" (the target of has) or "node" (the same node, or an ancestor)."""
    if isinstance(node, list):
        return [expanded(item, f"{where}[{i}]", at) for i, item in enumerate(node)]
    if not isinstance(node, dict):
        return node
    if "capture" in node:
        return captured(node, where, at)
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


class Inliner:
    """Expands each `{"matcher": name}` reference into the named fragment of one config layer, within a node budget;
    `used` collects the names reached, the ones inside other matchers included."""

    def __init__(self, matchers: Mapping[str, Any]) -> None:
        self.matchers = matchers
        self.used: set[str] = set()
        self.nodes = 0

    def rule(self, node: object, where: str = "match", path: tuple[str, ...] = ()) -> Any:
        self.nodes += 1
        if self.nodes > MAX_EXPANDED_NODES:
            raise policy.Invalid(f"matchers expand to more than {MAX_EXPANDED_NODES} nodes in one rule; a matcher that "
                                 "repeats another several times grows exponentially, so reuse smaller ones")
        if isinstance(node, list):
            return [self.rule(item, f"{where}[{i}]", path) for i, item in enumerate(node)]
        if not isinstance(node, dict):
            return node
        out = {key: self.rule(value, f"{where}.{key}", path) if key in (*NESTED, "statement", "capture") else value
               for key, value in node.items() if key != MATCHER}
        if MATCHER not in node:
            return out
        fragment = self.fragment(node[MATCHER], f"{where}.{MATCHER}", path)
        if not out:
            return fragment
        return {**fragment, **out} if not fragment.keys() & out.keys() else {"all": [fragment, out]}

    def fragment(self, name: object, where: str, path: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self.matchers:
            known = ", ".join(sorted(self.matchers)) or "none"
            raise policy.Invalid(f"{where.removesuffix('.matcher')} refers to no matcher {name!r} (this config defines: "
                                 f"{known})")
        if name in path:
            raise policy.Invalid(f"matcher cycle: {' -> '.join((*path, name))}")
        body = self.matchers[name]
        if not isinstance(body, dict) or not body:
            raise policy.Invalid(f"matcher '{name}' must be a non-empty rule object")
        self.used.add(name)
        inner: dict[str, Any] = self.rule(body, f"matcher '{name}'", (*path, name))
        expanded(inner, f"matcher '{name}'")
        return inner


def matcher_problems(matchers: Mapping[str, Any]) -> dict[str, str]:
    """Matcher name -> what is wrong with it, for the matchers of a layer that fail on their own or sit in a cycle; one
    that only fails through another matcher is left to the rule that uses it."""
    found: dict[str, str] = {}
    failing: dict[str, str] = {}
    for name in matchers:
        try:
            if not policy.NAME.fullmatch(name):
                raise policy.Invalid(f"name must match {policy.NAME.pattern}")
            check_regexes(Inliner(matchers).fragment(name, "matcher", ()))
        except policy.Invalid as exc:
            failing[name] = str(exc)
    stubs: dict[str, Any] = {name: {"kind": "word"} for name in matchers}
    for name, why in failing.items():
        try:
            check_regexes(Inliner({**stubs, name: matchers[name]}).fragment(name, "matcher", ()))
        except policy.Invalid as exc:
            found[name] = str(exc)
        else:
            if why.startswith("matcher cycle"):
                found[name] = why
    return found


def checked(match: object) -> dict[str, Any]:
    """The match object of a stored rule, validated: one ast-grep rule object built from its keys and the atoms."""
    if isinstance(match, dict) and (old := next((key for key in REMOVED if key in match), None)) \
            and (old != "regex" or set(match) == {"regex"}):
        raise policy.Invalid(f"{REMOVED[old]}; see references/matching.md")
    if not isinstance(match, dict) or not match:
        raise policy.Invalid("'match' must be a non-empty rule object")
    expanded(match)
    binds(match)
    return match


def regexes(node: object, where: str) -> Iterator[tuple[str, str]]:
    """(path, regex) for every regex a rule is written with: `regex` keys (also inside the assignment and redirect
    atoms) and the `args` of a command."""
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from regexes(item, f"{where}[{i}]")
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in ("regex", "args") and isinstance(value, str):
                yield f"{where}.{key}", value
            else:
                yield from regexes(value, f"{where}.{key}")


def check_regexes(match: object) -> None:
    """Raise policy.Invalid for a regex ast-grep cannot compile (its Rust dialect, which Python's `re` does not
    share); without the library nothing can be checked here and `rule add` / `status` compile-check later."""
    found = list(regexes(match, "match"))
    if not found:
        return
    try:
        from ast_grep_py import SgRoot
    except ImportError:
        return
    root = SgRoot("x", "bash").root()
    for where, text in found:
        try:
            root.find({"rule": {"regex": text}})
        except RuntimeError as exc:
            reason = next((line.strip() for line in reversed(str(exc).splitlines()) if line.strip()), "invalid regex")
            raise policy.Invalid(f"'{where}' is not a valid regex ({reason}); ast-grep uses the Rust regex syntax: "
                                 "no look-around or back-references") from None


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
    if "capture" in node:
        inner = loosened(node["capture"])
        return {**node, "capture": inner} if inner is not None else None
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
            leading = [{"has": {"kind": "variable_assignment", "nthChild": i}} for i in range(1, skipped + 1)]
            variants.append({**out, "pattern": {**pattern, "context": shaped} if in_context else shaped,
                             "all": [*leading, *checked]})
        out = {"any": variants}
    return out if found and out.get("any", True) else None


def built(match: dict[str, Any]) -> Rule:
    """The ast-grep rule for a match object: its single-command patterns spelling-tolerant, then its atoms expanded."""
    return expanded(widen(loosened(match) or match))


def config_of(rule: policy.Rule) -> Config:
    """The ast-grep config that finds this rule's matches."""
    return {"rule": built(rule.match)}
