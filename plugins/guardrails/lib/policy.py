"""Rule and mode schema: validation, managed/global/project layering, matching and message rendering."""

from __future__ import annotations

import re
import shutil
from typing import Any, Callable

from shellwords import SimpleCommand

Rule = dict[str, Any]
Mode = dict[str, Any]

ACTIONS = ("deny", "warn")
RETRIES = ("none", "same-command")
MATCH_KEYS = ("program", "args", "builtin", "regex")
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


def validate_rule(rule: object) -> None:
    if not isinstance(rule, dict):
        raise Invalid("a rule must be a JSON object")
    message = rule.get("message")
    if not isinstance(message, str) or not message.strip():
        raise Invalid("'message' is required")
    if "messageShort" in rule and not isinstance(rule["messageShort"], str):
        raise Invalid("'messageShort' must be a string")
    match = rule.get("match")
    if not isinstance(match, dict) or not any(match.get(k) for k in ("program", "builtin", "regex")):
        raise Invalid("'match' needs at least one of 'program', 'builtin', 'regex'")
    unknown = set(match) - set(MATCH_KEYS)
    if unknown:
        raise Invalid(f"unknown match keys: {', '.join(sorted(unknown))}")
    program = match.get("program")
    if program is not None and not (isinstance(program, str) and program) and not _str_list(program):
        raise Invalid("'match.program' must be a string or a list of strings")
    builtin = match.get("builtin")
    if builtin is not None and builtin not in BUILTINS:
        raise Invalid(f"unknown builtin '{builtin}' (known: {', '.join(sorted(BUILTINS))})")
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


def merge_rule(base: Rule, override: Rule) -> Rule:
    """Layer a lower-precedence entry onto a defaulted rule from a higher layer; only tightening changes apply.

    An override can never change what the rule matches: 'match' and 'requires' are ignored, and a
    merge that fails validation falls back to the base rule unchanged.
    """
    out = dict(base)
    for key in ("message", "messageShort", "description"):
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
    """Fold the layers in precedence order; each later layer may only tighten what an earlier one defined."""
    layers = [managed, global_state]
    if not isinstance(project_state, dict) or project_state.get("enabled", True) is not False:
        layers.append(project_state)
    rules: dict[str, Rule] = {}
    for state in layers:
        for rid, r in _entries(state, "rules").items():
            rules[rid] = merge_rule(rules[rid], r) if rid in rules else with_defaults(r)
    return rules


def _mode(m: dict[str, Any]) -> Mode:
    return {"description": str(m.get("description", "")), "agentMayEnable": m.get("agentMayEnable") is True,
            "active": m.get("active") is True}


def merge_mode(base: Mode, override: dict[str, Any]) -> Mode:
    return {
        "description": str(override.get("description") or base["description"]),
        "agentMayEnable": base["agentMayEnable"] and override.get("agentMayEnable", True) is not False,
        "active": base["active"] or override.get("active") is True,
    }


def effective_modes(managed: object, global_state: object, project_state: object) -> dict[str, Mode]:
    modes: dict[str, Mode] = {}
    for state in (managed, global_state, project_state):
        for name, m in _entries(state, "modes").items():
            modes[name] = merge_mode(modes[name], m) if name in modes else _mode(m)
    return modes


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


def rule_matches(rule: Rule, command: str, cmds: list[SimpleCommand] | None) -> bool:
    """cmds is None when the command could not be lexed (unbalanced quotes)."""
    match = view(rule, "match")
    if cmds is not None:
        if any(_command_matches(rule, c) for c in cmds):
            return True
    elif (programs_of(rule) and not match.get("builtin")
          and any(_fallback(p).search(command) for p in programs_of(rule))
          and (not match.get("args") or re.search(match["args"], command))):
        return True
    regex = match.get("regex")
    return bool(regex) and re.search(regex, command) is not None


def requirements_met(rule: Rule) -> bool:
    required = rule.get("requires")
    return not required or any(shutil.which(b) for b in required)


def render(text: str) -> str:
    """Substitute {which:a|b} with the first candidate found on PATH, else the first candidate."""

    def pick(m: re.Match[str]) -> str:
        candidates = [c.strip() for c in m.group(1).split("|") if c.strip()]
        return next((c for c in candidates if shutil.which(c)), candidates[0] if candidates else "")

    return PLACEHOLDER.sub(pick, text)
