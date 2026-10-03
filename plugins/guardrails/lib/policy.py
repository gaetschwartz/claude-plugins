"""Rule and mode schema: validation, managed/global/project layering, matching and message rendering."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import wrappers as wrapper_table

Rule = dict[str, Any]
Mode = dict[str, Any]

ACTIONS = ("deny", "warn")
RETRIES = ("none", "same-command")
MAX_AST_BYTES = 16384  # bounds nesting too: ast-grep overflows its stack past about 4000 levels
MATCH_KEYS = ("program", "args", "regex", "ast")
PLACEHOLDER = re.compile(r"\{which:([^{}]+)\}")


class Invalid(Exception):
    """A rule, mode or CLI argument is malformed."""


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


def ast_size(ast: object) -> int:
    return len(json.dumps(ast))


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
    if not isinstance(match, dict) or not any(match.get(k) for k in ("program", "regex", "ast")):
        raise Invalid("'match' needs at least one of 'program', 'regex', 'ast'")
    unknown = set(match) - set(MATCH_KEYS)
    if unknown:
        raise Invalid(f"unknown match keys: {', '.join(sorted(unknown))}")
    program = match.get("program")
    names = [program] if isinstance(program, str) else program
    if program is not None and not (isinstance(names, list) and names
                                    and all(wrapper_table.is_command_name(n) for n in names)):
        raise Invalid("'match.program' must be a command name or a list of them (no spaces or '/')")
    if "ast" in match:
        if not isinstance(match["ast"], dict) or not match["ast"]:
            raise Invalid("'match.ast' must be a non-empty object")
        size = ast_size(match["ast"])
        if size > MAX_AST_BYTES:
            raise Invalid(f"'match.ast' is {size} bytes, over the {MAX_AST_BYTES // 1024} KiB limit; split it into rules")
    for key in ("args", "regex"):
        if key in match and not isinstance(match[key], str):
            raise Invalid(f"'match.{key}' must be a string")
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


def with_defaults(rule: Rule) -> Rule:
    out = dict(rule)
    out.setdefault("enabled", True)
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


def wrapper_layers(managed: object, global_state: object, project_state: object) -> list[tuple[str, object]]:
    layers = [("managed state", managed), ("global state", global_state)]
    if not isinstance(project_state, dict) or project_state.get("enabled", True) is not False:
        layers.append(("project state", project_state))
    return layers


def effective_wrappers(managed: object, global_state: object, project_state: object) -> wrapper_table.Names:
    """Built-in wrapper names plus those added by each layer."""
    return wrapper_table.resolve(wrapper_layers(managed, global_state, project_state))[0]


def wrapper_problems(managed: object, global_state: object, project_state: object) -> list[str]:
    return wrapper_table.resolve(wrapper_layers(managed, global_state, project_state))[1]


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
        if isinstance(state, dict) and "wrappers" in state and not isinstance(state["wrappers"], dict):
            problems.append(f"managed state {path}: 'wrappers' must be an object, so all its entries are ignored")
        for name in view(state, "wrappers"):
            try:
                wrapper_table.check_name(name)
            except ValueError as exc:
                problems.append(f"managed state {path}: {exc}, so it is ignored")
                continue
            extra.setdefault(name, {})
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


def needs_parse(rule: Rule) -> bool:
    """True when the rule is judged by the ast-grep library (program, ast and regex all are)."""
    match = view(rule, "match")
    return any(match.get(key) for key in ("program", "ast", "regex"))


def requirements_met(rule: Rule) -> bool:
    required = rule.get("requires")
    import shutil

    return not required or any(shutil.which(b) for b in required)


def render(text: str) -> str:
    """Substitute {which:a|b} with the first candidate found on PATH, else the first candidate."""

    def pick(m: re.Match[str]) -> str:
        candidates = [c.strip() for c in m.group(1).split("|") if c.strip()]
        import shutil

        return next((c for c in candidates if shutil.which(c)), candidates[0] if candidates else "")

    return PLACEHOLDER.sub(pick, text)
