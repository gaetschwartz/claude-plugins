"""Rule and mode schema: validation, managed/global/project layering, matching and message rendering."""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from typing import Any, Callable

import wrappers as wrapper_table
from shellwords import SimpleCommand

Rule = dict[str, Any]
Mode = dict[str, Any]

ACTIONS = ("deny", "warn")
RETRIES = ("none", "same-command")
MATCH_KEYS = ("program", "args", "builtin", "regex", "ast")
AST_KEYS = ("pattern", "kind", "regex", "inside", "has", "follows", "precedes", "not", "any", "all", "stopBy", "field")
AST_RELATIONS = ("inside", "has", "follows", "precedes")
AST_PATTERN_KEYS = ("context", "selector", "strictness")
AST_MAX_DEPTH = 12
PLACEHOLDER = re.compile(r"\{which:([^{}]+)\}")

GREPS = {"grep", "egrep", "fgrep"}
# grep short options whose argument may be glued on (-e r ≠ -r)
GREP_OPTS_WITH_ARG = "efmABCdD"


class Invalid(Exception):
    """A rule, mode or CLI argument is malformed."""


def grep_is_recursive(args: list[str]) -> bool:
    it = iter(args)
    for a in it:
        if a == "--":
            return False
        if a in ("--recursive", "--dereference-recursive", "--directories=recurse"):
            return True
        if a == "--directories":
            return next(it, "") == "recurse"
        if a.startswith("--") or not a.startswith("-") or a == "-":
            continue
        for i, c in enumerate(a[1:]):
            if c in "rR":
                return True
            if c in GREP_OPTS_WITH_ARG:
                if c == "d" and (a[i + 2:] or next(it, "")) == "recurse":
                    return True
                break
    return False


def _grep_recursive(cmd: SimpleCommand) -> bool:
    return cmd.name in GREPS and grep_is_recursive(cmd.args)


BUILTINS: dict[str, Callable[[SimpleCommand], bool]] = {"grep-recursive": _grep_recursive}


def view(mapping: object, key: str) -> dict[str, Any]:
    """mapping[key] if it is a dict (the live object, so mutations stick), else a detached {}."""
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, dict) else {}


def programs_of(rule: Rule) -> list[str]:
    program = view(rule, "match").get("program")
    if isinstance(program, str):
        return [program]
    return [p for p in program if isinstance(p, str)] if isinstance(program, list) else []


def modes_of(rule: Rule) -> list[str]:
    modes = rule.get("modes")
    return [m for m in modes if isinstance(m, str)] if isinstance(modes, list) else []


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x for x in value)


def validate_ast(node: object, path: str = "match.ast", depth: int = 0) -> None:
    """Structural check of an ast-grep rule object (supported keys only); compiling it needs ast-grep itself."""
    if depth > AST_MAX_DEPTH:
        raise Invalid(f"'{path}' is nested too deeply")
    if not isinstance(node, dict) or not node:
        raise Invalid(f"'{path}' must be a non-empty object")
    unknown = set(node) - set(AST_KEYS)
    if unknown:
        raise Invalid(f"'{path}' has unsupported keys: {', '.join(sorted(unknown))} (supported: {', '.join(AST_KEYS)})")
    pattern = node.get("pattern")
    if "pattern" in node:
        if isinstance(pattern, dict):
            if not isinstance(pattern.get("context"), str) or set(pattern) - set(AST_PATTERN_KEYS) \
                    or not all(isinstance(v, str) for v in pattern.values()):
                raise Invalid(f"'{path}.pattern' object needs a string 'context' and only "
                              f"{', '.join(AST_PATTERN_KEYS)}")
        elif not isinstance(pattern, str) or not pattern.strip():
            raise Invalid(f"'{path}.pattern' must be a non-empty string or a {{context, selector}} object")
    for key in ("kind", "regex", "field"):
        if key in node and not (isinstance(node[key], str) and node[key]):
            raise Invalid(f"'{path}.{key}' must be a non-empty string")
    for key in AST_RELATIONS + ("not",):
        if key in node:
            validate_ast(node[key], f"{path}.{key}", depth + 1)
    for key in ("any", "all"):
        if key in node:
            items = node[key]
            if not isinstance(items, list) or not items:
                raise Invalid(f"'{path}.{key}' must be a non-empty list of rules")
            for i, item in enumerate(items):
                validate_ast(item, f"{path}.{key}[{i}]", depth + 1)
    if "stopBy" in node:
        stop = node["stopBy"]
        if isinstance(stop, dict):
            validate_ast(stop, f"{path}.stopBy", depth + 1)
        elif stop not in ("neighbor", "end"):
            raise Invalid(f"'{path}.stopBy' must be \"neighbor\", \"end\" or a rule")


def ast_of(rule: Rule) -> dict[str, Any] | None:
    ast = view(rule, "match").get("ast")
    return ast if isinstance(ast, dict) else None


def ast_patterns(node: object) -> list[str]:
    """Every pattern string in an ast rule, in document order."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern":
                out.append(value if isinstance(value, str) else str(view(value, "context")))
            else:
                out += ast_patterns(value)
    elif isinstance(node, list):
        for item in node:
            out += ast_patterns(item)
    return out


def validate_rule(rule: object) -> None:
    if not isinstance(rule, dict):
        raise Invalid("a rule must be a JSON object")
    message = rule.get("message")
    if not isinstance(message, str) or not message.strip():
        raise Invalid("'message' is required")
    if "messageShort" in rule and not isinstance(rule["messageShort"], str):
        raise Invalid("'messageShort' must be a string")
    match = rule.get("match")
    if not isinstance(match, dict) or not any(match.get(k) for k in ("program", "builtin", "regex", "ast")):
        raise Invalid("'match' needs at least one of 'program', 'builtin', 'regex', 'ast'")
    unknown = set(match) - set(MATCH_KEYS)
    if unknown:
        raise Invalid(f"unknown match keys: {', '.join(sorted(unknown))}")
    program = match.get("program")
    if program is not None and not (isinstance(program, str) and program) and not _str_list(program):
        raise Invalid("'match.program' must be a string or a list of strings")
    builtin = match.get("builtin")
    if builtin is not None and builtin not in BUILTINS:
        raise Invalid(f"unknown builtin '{builtin}' (known: {', '.join(sorted(BUILTINS))})")
    if "ast" in match:
        validate_ast(match["ast"])
    for key in ("args", "regex"):
        if key in match:
            if not isinstance(match[key], str):
                raise Invalid(f"'match.{key}' must be a string")
            try:
                re.compile(match[key])
            except re.error as exc:
                raise Invalid(f"'match.{key}' is not a valid regex: {exc}") from exc
    if rule.get("action", "deny") not in ACTIONS:
        raise Invalid(f"'action' must be one of {', '.join(ACTIONS)}")
    if rule.get("retry", "none") not in RETRIES:
        raise Invalid(f"'retry' must be one of {', '.join(RETRIES)}")
    if not _str_list(rule.get("modes", [])):
        raise Invalid("'modes' must be a list of mode names")
    if "requires" in rule and not (_str_list(rule["requires"]) and rule["requires"]):
        raise Invalid("'requires' must be a non-empty list of binary names")
    if "enabled" in rule and not isinstance(rule["enabled"], bool):
        raise Invalid("'enabled' must be true or false")
    if not isinstance(rule.get("tool", "Bash"), str):
        raise Invalid("'tool' must be a tool name")


def with_defaults(rule: Rule) -> Rule:
    out = dict(rule)
    out.setdefault("enabled", True)
    out.setdefault("tool", "Bash")
    out.setdefault("action", "deny")
    out.setdefault("retry", "none")
    out.setdefault("modes", [])
    return out


def merge_rule(base: Rule, override: Rule, reword: bool = True) -> Rule:
    """Layer a lower-precedence entry onto a defaulted rule from a higher layer; only tightening changes apply.

    An override can never change what the rule matches: 'match' and 'requires' are ignored, and a
    merge that fails validation falls back to the base rule unchanged. With reword=False the texts
    are ignored too.
    """
    out = dict(base)
    for key in ("message", "messageShort", "description") if reword else ():
        if key in override:
            out[key] = override[key]
    if override.get("action") == "deny":
        out["action"] = "deny"
    if override.get("retry") == "none":
        out["retry"] = "none"
    if override.get("enabled") is True:
        out["enabled"] = True
    if isinstance(override.get("modes"), list):
        out["modes"] = [m for m in modes_of(base) if m in override["modes"]]
    try:
        validate_rule(out)
    except Invalid:
        return base
    return out


def _entries(state: object, key: str) -> dict[str, dict[str, Any]]:
    return {str(k): v for k, v in view(state, key).items() if isinstance(v, dict)}


LAYERS = ("managed", "global", "project")


def origins(key: str, managed: object, global_state: object, project_state: object) -> dict[str, list[str]]:
    """Entry id (of 'rules' or 'modes') → the layers that define it, highest precedence first."""
    out: dict[str, list[str]] = {}
    for layer, state in zip(LAYERS, (managed, global_state, project_state)):
        for name in _entries(state, key):
            out.setdefault(name, []).append(layer)
    return out


def effective_rules(managed: object, global_state: object, project_state: object) -> dict[str, Rule]:
    """Fold the layers in precedence order; each later layer may only tighten what an earlier one defined.

    A managed rule keeps its own texts, and only suspends for modes the managed layer declares.
    """
    managed_rules = _entries(managed, "rules")
    declared = set(_entries(managed, "modes"))
    rules = {rid: with_defaults(r) for rid, r in managed_rules.items()}
    for rule in rules.values():
        rule["modes"] = [m for m in modes_of(rule) if m in declared]
    layers = [global_state]
    if not isinstance(project_state, dict) or project_state.get("enabled", True) is not False:
        layers.append(project_state)
    for state in layers:
        for rid, r in _entries(state, "rules").items():
            rules[rid] = merge_rule(rules[rid], r, rid not in managed_rules) if rid in rules else with_defaults(r)
    return rules


def effective_wrappers(managed: object, global_state: object, project_state: object) -> wrapper_table.Table:
    """Built-in wrappers plus every layer's additions; a layer can only add look-through, never remove it."""
    layers = [managed, global_state]
    if not isinstance(project_state, dict) or project_state.get("enabled", True) is not False:
        layers.append(project_state)
    return wrapper_table.effective(*layers)


def _mode(m: dict[str, Any]) -> Mode:
    return {"description": str(m.get("description", "")), "agentMayEnable": m.get("agentMayEnable") is True,
            "active": m.get("active") is True}


def merge_mode(base: Mode, override: dict[str, Any], trust_active: bool = True) -> Mode:
    return {
        "description": str(override.get("description") or base["description"]),
        "agentMayEnable": base["agentMayEnable"] and override.get("agentMayEnable", True) is not False,
        "active": base["active"] or (trust_active and override.get("active") is True),
    }


def effective_modes(managed: object, global_state: object, project_state: object) -> dict[str, Mode]:
    """A project (repo-controlled) cannot switch on a mode the managed layer declares."""
    modes: dict[str, Mode] = {}
    declared = set(_entries(managed, "modes"))
    for layer, state in enumerate((managed, global_state, project_state)):
        for name, m in _entries(state, "modes").items():
            if name not in modes:
                modes[name] = _mode(m)
            else:
                modes[name] = merge_mode(modes[name], m, layer < 2 or name not in declared)
    return modes


def _shape_problems(path: str, state: object) -> list[str]:
    problems = []
    for key in ("rules", "modes"):
        if not isinstance(state, dict) or key not in state:
            continue
        table = state[key]
        if not isinstance(table, dict):
            problems.append(f"managed state {path}: '{key}' must be an object, so all its entries are ignored")
            continue
        problems += [f"managed state {path}: {key} entry '{name}' is not an object and is ignored"
                     for name, entry in table.items() if not isinstance(entry, dict)]
    return problems


def managed_layer(sources: Sequence[tuple[str, object]]) -> tuple[dict[str, Any], list[str]]:
    """Combine managed files (highest ranked first; later ones can only tighten) and list what is wrong with them."""
    problems: list[str] = []
    rules: dict[str, Rule] = {}
    modes: dict[str, Mode] = {}
    extra: dict[str, dict[str, Any]] = {}
    for path, state in sources:
        problems += _shape_problems(path, state)
        problems += wrapper_table.problems(f"managed state {path}", state)
        for name, entry in _entries(state, "wrappers").items():
            try:
                wrapper_table.validate(name, entry)
            except ValueError:
                continue
            extra[name] = wrapper_table.merge(extra.get(name, {}), entry)
        for name, m in _entries(state, "modes").items():
            modes[name] = merge_mode(modes[name], m, False) if name in modes else _mode(m)
        for rid, r in _entries(state, "rules").items():
            try:
                validate_rule(r)
            except Invalid as exc:
                problems.append(f"managed rule {rid} is invalid and ignored: {exc}")
            if rid in rules:
                rules[rid] = merge_rule(rules[rid], r, False)
                continue
            rule = with_defaults(r)
            problems += [f"managed rule {rid} lists mode '{m}', which the managed file does not declare, so it cannot "
                         "suspend the rule" for m in modes_of(rule) if m not in modes]
            rule["modes"] = [m for m in modes_of(rule) if m in modes]
            rules[rid] = rule
    layer: dict[str, Any] = {"rules": rules, "modes": modes}
    if extra:
        layer["wrappers"] = extra
    return layer, problems


def active_modes(modes: dict[str, Mode], session: object) -> dict[str, dict[str, Any]]:
    """Mode name → activation record for every mode that is currently on."""
    active: dict[str, dict[str, Any]] = {
        name: {"by": "user", "reason": "persistently active"} for name, m in modes.items() if m["active"]
    }
    for name, record in view(session, "modes").items():
        if name not in modes or name in active or not isinstance(record, dict):
            continue
        if record.get("by") == "agent" and not modes[name]["agentMayEnable"]:
            continue
        active[name] = record
    return active


def _fallback(program: str) -> re.Pattern[str]:
    return re.compile(r"(^|[^A-Za-z0-9_.-])([^\s]*/)?" + re.escape(program) + r"([^A-Za-z0-9_-]|$)")


def _command_matches(rule: Rule, cmd: SimpleCommand) -> bool:
    match = view(rule, "match")
    programs = programs_of(rule)
    builtin = match.get("builtin")
    if not programs and not builtin:
        return False
    if programs and cmd.name not in programs:
        return False
    if match.get("args") and not re.search(match["args"], " ".join(cmd.args)):
        return False
    return not builtin or BUILTINS[builtin](cmd)


def match_kind(rule: Rule, command: str, cmds: list[SimpleCommand] | None) -> str | None:
    """None when the rule's matcher does not select the command, else "direct" or "wrapped".

    "wrapped" means only the look-through (wrapper, shell string, substitution, pipeline member) made a
    program/args/builtin match; a regex match reads the raw text and never counts as wrapped.
    """
    match = view(rule, "match")
    kind: str | None = None
    if cmds is not None:
        hits = [c for c in cmds if _command_matches(rule, c)]
        if hits:
            kind = "direct" if any(not c.wrapped for c in hits) else "wrapped"
    elif (programs_of(rule) and not match.get("builtin")
          and any(_fallback(p).search(command) for p in programs_of(rule))
          and (not match.get("args") or re.search(match["args"], command))):
        kind = "direct"
    regex = match.get("regex")
    if regex and re.search(regex, command) is not None:
        return "direct"
    return kind


def rule_matches(rule: Rule, command: str, cmds: list[SimpleCommand] | None) -> bool:
    """cmds is None when the command could not be lexed (unbalanced quotes)."""
    return match_kind(rule, command, cmds) is not None


def requirements_met(rule: Rule) -> bool:
    required = rule.get("requires")
    return not required or any(shutil.which(b) for b in required)


def render(text: str) -> str:
    """Substitute {which:a|b} with the first candidate found on PATH, else the first candidate."""

    def pick(m: re.Match[str]) -> str:
        candidates = [c.strip() for c in m.group(1).split("|") if c.strip()]
        return next((c for c in candidates if shutil.which(c)), candidates[0] if candidates else "")

    return PLACEHOLDER.sub(pick, text)
