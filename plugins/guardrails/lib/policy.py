"""The typed rule, mode and session model: parsing, managed/global/project layering and message rendering."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Self

import wrappers as wrapper_table

MAX_AST_BYTES = 16384  # bounds nesting too: ast-grep overflows its stack past about 4000 levels
MATCH_KEYS = ("program", "args", "regex", "ast")
RULE_KEYS = ("match", "message", "messageShort", "action", "retry", "enabled", "modes", "requires", "description",
             "setBy", "id")
UNKNOWN_FIELD_HINT = ("this version does not know it; reinstall the preset that added it (`guardrails preset install "
                      "<name>`) or remove the field")
LAYERS = ("managed", "global", "project")
PLACEHOLDER = re.compile(r"\{which:([^{}]+)\}")


class Invalid(Exception):
    """A rule, mode or CLI argument is malformed."""


class Action(StrEnum):
    DENY = "deny"
    WARN = "warn"


class Retry(StrEnum):
    NONE = "none"
    SAME_COMMAND = "same-command"


class Actor(StrEnum):
    USER = "user"
    AGENT = "agent"


def view(mapping: object, key: str) -> dict[str, Any]:
    """mapping[key] if it is a dict (the live object, so mutations stick), else a detached {}."""
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, dict) else {}


def text_list(value: object, what: str, *, required: bool = False) -> tuple[str, ...]:
    if not (isinstance(value, list) and all(isinstance(x, str) and x for x in value)) or (required and not value):
        raise Invalid(f"'{what}' must be a {'non-empty ' if required else ''}list of "
                      f"{'binary' if what == 'requires' else 'mode'} names")
    return tuple(value)


def json_modes(raw: object) -> list[str]:
    """The mode names a raw rule entry lists (for entries that are not parsed, such as a preset's)."""
    modes = raw.get("modes") if isinstance(raw, dict) else None
    return [m for m in modes if isinstance(m, str)] if isinstance(modes, list) else []


@dataclass(frozen=True, slots=True)
class Match:
    program: tuple[str, ...] = ()
    args: str | None = None
    regex: str | None = None
    ast: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, raw: object) -> Self:
        if isinstance(raw, dict) and (unknown := set(raw) - set(MATCH_KEYS)):
            raise Invalid(f"unknown field match.{sorted(unknown)[0]}: {UNKNOWN_FIELD_HINT}")
        if not isinstance(raw, dict) or not any(raw.get(k) for k in ("program", "regex", "ast")):
            raise Invalid("'match' needs at least one of 'program', 'regex', 'ast'")
        program = raw.get("program")
        names = [program] if isinstance(program, str) else program
        if program is not None and not (isinstance(names, list) and names
                                        and all(wrapper_table.is_command_name(n) for n in names)):
            raise Invalid("'match.program' must be a command name or a list of them (no spaces or '/')")
        ast = raw.get("ast")
        if "ast" in raw:
            if not isinstance(ast, dict) or not ast:
                raise Invalid("'match.ast' must be a non-empty object")
            if (size := ast_size(ast)) > MAX_AST_BYTES:
                raise Invalid(f"'match.ast' is {size} bytes, over the {MAX_AST_BYTES // 1024} KiB limit; split it into rules")
        for key in ("args", "regex"):
            if key in raw and not isinstance(raw[key], str):
                raise Invalid(f"'match.{key}' must be a string")
        return cls(tuple(names or ()), raw.get("args"), raw.get("regex"), ast)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.program:
            out["program"] = list(self.program)
        for key in ("args", "regex", "ast"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


@dataclass(frozen=True, slots=True)
class Rule:
    match: Match
    message: str
    action: Action = Action.DENY
    retry: Retry = Retry.NONE
    enabled: bool = True
    modes: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    message_short: str | None = None
    description: str | None = None

    @classmethod
    def from_json(cls, raw: object) -> Self:
        if not isinstance(raw, dict):
            raise Invalid("a rule must be a JSON object")
        if unknown := set(raw) - set(RULE_KEYS):
            raise Invalid(f"unknown field {sorted(unknown)[0]}: {UNKNOWN_FIELD_HINT}")
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise Invalid("'message' is required")
        if "messageShort" in raw and not isinstance(raw["messageShort"], str):
            raise Invalid("'messageShort' must be a string")
        match = Match.from_json(raw.get("match"))
        try:
            action, retry = Action(raw.get("action", "deny")), Retry(raw.get("retry", "none"))
        except ValueError:
            raise Invalid(f"'action' must be one of {', '.join(Action)} and 'retry' one of "
                          f"{', '.join(Retry)}") from None
        modes = text_list(raw.get("modes", []), "modes")
        requires = text_list(raw["requires"], "requires", required=True) if "requires" in raw else ()
        if "enabled" in raw and not isinstance(raw["enabled"], bool):
            raise Invalid("'enabled' must be true or false")
        description = raw.get("description")
        return cls(match, message, action, retry, raw.get("enabled", True), modes, requires, raw.get("messageShort"),
                   description if isinstance(description, str) else None)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"match": self.match.to_json(), "message": self.message, "action": str(self.action),
                               "retry": str(self.retry), "enabled": self.enabled, "modes": list(self.modes)}
        for key, value in (("requires", list(self.requires)), ("messageShort", self.message_short),
                           ("description", self.description)):
            if value:
                out[key] = value
        return out


@dataclass(frozen=True, slots=True)
class Mode:
    description: str = ""
    agent_may_enable: bool = False
    active: bool = False

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Self:
        return cls(str(raw.get("description", "")), raw.get("agentMayEnable") is True, raw.get("active") is True)

    def tightened_by(self, override: Mapping[str, Any], trust_active: bool = True) -> Self:
        return replace(self, description=str(override.get("description") or self.description),
                       agent_may_enable=self.agent_may_enable and override.get("agentMayEnable", True) is not False,
                       active=self.active or (trust_active and override.get("active") is True))


@dataclass(frozen=True, slots=True)
class Activation:
    """How a mode came to be on: who said so and why."""

    by: Actor
    reason: str | None = None
    at: str | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Self:
        reason, at = raw.get("reason"), raw.get("at")
        return cls(Actor.AGENT if raw.get("by") == "agent" else Actor.USER, reason if isinstance(reason, str) else None,
                   at if isinstance(at, str) else None)

    def to_json(self) -> dict[str, str]:
        return {key: value for key, value in (("by", str(self.by)), ("at", self.at), ("reason", self.reason)) if value}


@dataclass(frozen=True, slots=True)
class EngineFailure:
    kind: str
    at: str
    reason: str


@dataclass(slots=True)
class Session:
    """What the hook remembers about one session; every list is a set of things already said or acknowledged."""

    reported: list[str] = field(default_factory=list)
    shown: list[str] = field(default_factory=list)
    acknowledged: list[str] = field(default_factory=list)
    warned: list[str] = field(default_factory=list)
    reported_at: dict[str, float] = field(default_factory=dict)
    modes: dict[str, Activation] = field(default_factory=dict)
    engine_failure: EngineFailure | None = None

    @classmethod
    def from_json(cls, raw: object) -> Self:
        def strings(key: str) -> list[str]:
            value = raw.get(key) if isinstance(raw, dict) else None
            return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []

        failure = view(raw, "engineFailure")
        return cls(strings("reported"), strings("shown"), strings("acknowledged"), strings("warned"),
                   {k: float(v) for k, v in view(raw, "reportedAt").items() if isinstance(v, (int, float))},
                   {k: Activation.from_json(v) for k, v in view(raw, "modes").items() if isinstance(v, dict)},
                   EngineFailure(str(failure.get("kind", "")), str(failure["at"]), str(failure.get("reason", "")))
                   if failure.get("at") else None)

    def to_json(self, seen_at: str) -> dict[str, Any]:
        out: dict[str, Any] = {"seenAt": seen_at}
        for key, items in (("reported", self.reported), ("shown", self.shown), ("acknowledged", self.acknowledged),
                           ("warned", self.warned), ("reportedAt", self.reported_at)):
            if items:
                out[key] = items
        if self.modes:
            out["modes"] = {name: record.to_json() for name, record in self.modes.items()}
        if self.engine_failure:
            out["engineFailure"] = {"kind": self.engine_failure.kind, "at": self.engine_failure.at,
                                    "reason": self.engine_failure.reason}
        return out


def ast_size(ast: object) -> int:
    return len(json.dumps(ast))


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


def merge_rule(base: Rule, override: Mapping[str, Any], reword: bool = True) -> Rule:
    """Layer a lower-precedence entry onto a rule from a higher layer; only tightening changes apply.

    An override can never change what the rule matches: 'match' and 'requires' are ignored, and an
    override whose texts are malformed leaves the base rule unchanged. With reword=False the texts
    are ignored too.
    """
    changes: dict[str, Any] = {}
    if reword:
        texts = {attribute: override[key] for key, attribute in (("message", "message"), ("messageShort", "message_short"),
                                                                 ("description", "description")) if key in override}
        if not all(isinstance(text, str) for text in texts.values()) or not str(texts.get("message", base.message)).strip():
            return base
        changes.update(texts)
    if override.get("action") == "deny":
        changes["action"] = Action.DENY
    if override.get("retry") == "none":
        changes["retry"] = Retry.NONE
    if override.get("enabled") is True:
        changes["enabled"] = True
    if isinstance(override.get("modes"), list):
        changes["modes"] = tuple(m for m in base.modes if m in override["modes"])
    return replace(base, **changes)


def _entries(state: object, key: str) -> dict[str, dict[str, Any]]:
    return {str(k): v for k, v in view(state, key).items() if isinstance(v, dict)}


def origins(key: str, managed: object, global_state: object, project_state: object) -> dict[str, list[str]]:
    """Entry id (of 'rules' or 'modes') → the layers that define it, highest precedence first."""
    out: dict[str, list[str]] = {}
    for layer, state in zip(LAYERS, (managed, global_state, project_state), strict=True):
        for name in _entries(state, key):
            out.setdefault(name, []).append(layer)
    return out


@dataclass(frozen=True, slots=True)
class Effective:
    rules: dict[str, Rule]
    problems: dict[str, str]


def project_enabled(project_state: object) -> bool:
    return not isinstance(project_state, dict) or project_state.get("enabled", True) is not False


def effective(managed: object, global_state: object, project_state: object) -> Effective:
    """Fold the layers in precedence order; each later layer may only tighten what an earlier one defined.

    A managed rule keeps its own texts, and only suspends for modes the managed layer declares. An entry that does
    not parse is left out and named in `problems`; a lower layer cannot take its place.
    """
    managed_rules = _entries(managed, "rules")
    declared = set(_entries(managed, "modes"))
    rules: dict[str, Rule] = {}
    problems: dict[str, str] = {}

    def add(rid: str, raw: Mapping[str, Any]) -> None:
        try:
            rules[rid] = Rule.from_json(raw)
        except Invalid as exc:
            problems[rid] = str(exc)

    for rid, raw in managed_rules.items():
        add(rid, raw)
        if rid in rules:
            rules[rid] = replace(rules[rid], modes=tuple(m for m in rules[rid].modes if m in declared))
    for state in [global_state, *([project_state] if project_enabled(project_state) else [])]:
        for rid, raw in _entries(state, "rules").items():
            if rid in problems:
                continue
            if rid in rules:
                rules[rid] = merge_rule(rules[rid], raw, rid not in managed_rules)
            else:
                add(rid, raw)
    return Effective(rules, problems)


def effective_rules(managed: object, global_state: object, project_state: object) -> dict[str, Rule]:
    return effective(managed, global_state, project_state).rules


def wrapper_layers(managed: object, global_state: object, project_state: object) -> list[tuple[str, object]]:
    layers = [("managed state", managed), ("global state", global_state)]
    if project_enabled(project_state):
        layers.append(("project state", project_state))
    return layers


def effective_wrappers(managed: object, global_state: object, project_state: object) -> wrapper_table.Names:
    """Built-in wrapper names plus those added by each layer."""
    return wrapper_table.resolve(wrapper_layers(managed, global_state, project_state))[0]


def wrapper_problems(managed: object, global_state: object, project_state: object) -> list[str]:
    return wrapper_table.resolve(wrapper_layers(managed, global_state, project_state))[1]


def effective_modes(managed: object, global_state: object, project_state: object) -> dict[str, Mode]:
    """A project (repo-controlled) cannot switch on a mode the managed layer declares."""
    modes: dict[str, Mode] = {}
    declared = set(_entries(managed, "modes"))
    for layer, state in enumerate((managed, global_state, project_state)):
        for name, raw in _entries(state, "modes").items():
            if name not in modes:
                modes[name] = Mode.from_json(raw)
            else:
                modes[name] = modes[name].tightened_by(raw, layer < 2 or name not in declared)
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
        for name, raw in _entries(state, "modes").items():
            modes[name] = modes[name].tightened_by(raw, False) if name in modes else Mode.from_json(raw)
        for rid, raw in _entries(state, "rules").items():
            if rid in rules:
                rules[rid] = merge_rule(rules[rid], raw, False)
                continue
            try:
                rule = Rule.from_json(raw)
            except Invalid as exc:
                problems.append(f"managed rule {rid} is invalid and ignored: {exc}")
                continue
            problems += [f"managed rule {rid} lists mode '{m}', which the managed file does not declare, so it cannot "
                         "suspend the rule" for m in rule.modes if m not in modes]
            rules[rid] = replace(rule, modes=tuple(m for m in rule.modes if m in modes))
    layer: dict[str, Any] = {"rules": {rid: rule.to_json() for rid, rule in rules.items()},
                    "modes": {name: {"description": m.description, "agentMayEnable": m.agent_may_enable,
                                     "active": m.active} for name, m in modes.items()}}
    if extra:
        layer["wrappers"] = extra
    return layer, problems


def active_modes(modes: Mapping[str, Mode], session: Session) -> dict[str, Activation]:
    """Mode name → activation record for every mode that is currently on."""
    active = {name: Activation(Actor.USER, "persistently active") for name, m in modes.items() if m.active}
    for name, record in session.modes.items():
        if name not in modes or name in active:
            continue
        if record.by is Actor.AGENT and not modes[name].agent_may_enable:
            continue
        active[name] = record
    return active


def requirements_met(rule: Rule) -> bool:
    import shutil

    return not rule.requires or any(shutil.which(b) for b in rule.requires)


def render(text: str) -> str:
    """Substitute {which:a|b} with the first candidate found on PATH, else the first candidate."""

    def pick(m: re.Match[str]) -> str:
        candidates = [c.strip() for c in m.group(1).split("|") if c.strip()]
        import shutil

        return next((c for c in candidates if shutil.which(c)), candidates[0] if candidates else "")

    return PLACEHOLDER.sub(pick, text)
