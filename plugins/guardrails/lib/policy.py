"""The typed rule, mode and session model: parsing, managed/global/project layering and message rendering."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, Self

if TYPE_CHECKING:
    from messages import Case

MAX_AST_BYTES = 16384  # bounds nesting too: ast-grep overflows its stack past about 4000 levels
RULE_KEYS = ("match", "wrappers", "message", "messageShort", "messages", "action", "retry", "enabled", "modes", "when",
             "description", "setBy", "id")
REMOVED_FIELDS = {
    "requires": ("'requires' was replaced by 'when': write \"when\": {\"bin\": [<names>]} (true when any of them is on "
                 "PATH), or reinstall the preset that added it (`guardrails preset install <name>`)"),
}
UNKNOWN_FIELD_HINT = ("this version does not know it; reinstall the preset that added it (`guardrails preset install "
                      "<name>`) or remove the field")
LAYERS = ("managed", "global", "project")


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


def command_name(name: object) -> bool:
    return isinstance(name, str) and bool(name) and "/" not in name and not any(c.isspace() for c in name)


def view(mapping: object, key: str) -> dict[str, Any]:
    """mapping[key] if it is a dict (the live object, so mutations stick), else a detached {}."""
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, dict) else {}


def mode_list(value: object) -> tuple[str, ...]:
    if not (isinstance(value, list) and all(isinstance(x, str) and x for x in value)):
        raise Invalid("'modes' must be a list of mode names")
    return tuple(value)


def json_modes(raw: object) -> list[str]:
    """The mode names a raw rule entry lists (for entries that are not parsed, such as a preset's)."""
    modes = raw.get("modes") if isinstance(raw, dict) else None
    return [m for m in modes if isinstance(m, str)] if isinstance(modes, list) else []


def match_of(raw: object) -> dict[str, Any]:
    import rulebuilder

    match = rulebuilder.checked(raw)
    if (size := ast_size(match)) > MAX_AST_BYTES:
        raise Invalid(f"'match' is {size} bytes, over the {MAX_AST_BYTES // 1024} KiB limit; split it into rules")
    rulebuilder.check_regexes(match)
    return match


class Rule(NamedTuple):
    match: dict[str, Any]
    message: str
    action: Action = Action.DENY
    retry: Retry = Retry.NONE
    enabled: bool = True
    modes: tuple[str, ...] = ()
    message_short: str | None = None
    description: str | None = None
    wrappers: bool = True
    when: dict[str, Any] | None = None
    messages: tuple[Case, ...] = ()

    @classmethod
    def from_json(cls, raw: object) -> Self:
        import conditions
        import messages as texts
        import rulebuilder

        if not isinstance(raw, dict):
            raise Invalid("a rule must be a JSON object")
        if removed := next((key for key in REMOVED_FIELDS if key in raw), None):
            raise Invalid(REMOVED_FIELDS[removed])
        if unknown := set(raw) - set(RULE_KEYS):
            raise Invalid(f"unknown field {min(unknown)}: {UNKNOWN_FIELD_HINT}")
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise Invalid("'message' is required")
        if "messageShort" in raw and not isinstance(raw["messageShort"], str):
            raise Invalid("'messageShort' must be a string")
        match = match_of(raw.get("match"))
        try:
            action, retry = Action(raw.get("action", "deny")), Retry(raw.get("retry", "none"))
        except ValueError:
            raise Invalid(f"'action' must be one of {', '.join(Action)} and 'retry' one of "
                          f"{', '.join(Retry)}") from None
        modes = mode_list(raw.get("modes", []))
        for key in ("enabled", "wrappers"):
            if key in raw and not isinstance(raw[key], bool):
                raise Invalid(f"'{key}' must be true or false")
        when = raw.get("when")
        if "when" in raw:
            conditions.check(when, "when")
        cases = texts.cases_of(raw["messages"]) if "messages" in raw else ()
        description = raw.get("description")
        rule = cls(match, message, action, retry, raw.get("enabled", True), modes, raw.get("messageShort"),
                   description if isinstance(description, str) else None, raw.get("wrappers", True), when, cases)
        texts.check_texts(texts.rule_texts(rule), any(conditions.bin_atoms(when)),
                           lambda: rulebuilder.bound_names(match))
        return rule


class Mode(NamedTuple):
    description: str = ""
    agent_may_enable: bool = False
    active: bool = False

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Self:
        return cls(str(raw.get("description", "")), raw.get("agentMayEnable") is True, raw.get("active") is True)

    def tightened_by(self, override: Mapping[str, Any], trust_active: bool = True) -> Self:
        return self._replace(description=str(override.get("description") or self.description),
                             agent_may_enable=self.agent_may_enable and override.get("agentMayEnable", True) is not False,
                             active=self.active or (trust_active and override.get("active") is True))


class Activation(NamedTuple):
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


class Session:
    """What the hook remembers about one session; every list is a set of things already said or acknowledged."""

    __slots__ = ("acknowledged", "modes", "reported", "reported_at", "shown", "warned")

    def __init__(self) -> None:
        self.reported: list[str] = []
        self.shown: list[str] = []
        self.acknowledged: list[str] = []
        self.warned: list[str] = []
        self.reported_at: dict[str, float] = {}
        self.modes: dict[str, Activation] = {}

    @classmethod
    def from_json(cls, raw: object) -> Self:
        def strings(key: str) -> list[str]:
            value = raw.get(key) if isinstance(raw, dict) else None
            return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []

        session = cls()
        session.reported, session.shown = strings("reported"), strings("shown")
        session.acknowledged, session.warned = strings("acknowledged"), strings("warned")
        session.reported_at = {k: float(v) for k, v in view(raw, "reportedAt").items() if isinstance(v, (int, float))}
        session.modes = {k: Activation.from_json(v) for k, v in view(raw, "modes").items() if isinstance(v, dict)}
        return session

    def to_json(self, seen_at: str) -> dict[str, Any]:
        out: dict[str, Any] = {"seenAt": seen_at}
        for key, items in (("reported", self.reported), ("shown", self.shown), ("acknowledged", self.acknowledged),
                           ("warned", self.warned), ("reportedAt", self.reported_at)):
            if items:
                out[key] = items
        if self.modes:
            out["modes"] = {name: record.to_json() for name, record in self.modes.items()}
        return out


def ast_size(ast: object) -> int:
    return len(json.dumps(ast))


def rule_hash(rule: Rule) -> str:
    """Eight hex digits identifying everything about an effective rule that shapes a denial (never its bookkeeping)."""
    shaping = {"match": rule.match, "wrappers": rule.wrappers, "when": rule.when, "action": rule.action.value,
               "retry": rule.retry.value, "message": rule.message, "messageShort": rule.message_short,
               "messages": [{"when": c.when, "text": c.text, "messageShort": c.message_short} for c in rule.messages]}
    canonical = json.dumps(shaping, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def merge_rule(base: Rule, override: Mapping[str, Any], reword: bool = True) -> Rule:
    """Layer a lower-precedence entry onto a rule from a higher layer; only tightening changes apply.

    An override can never change what the rule matches or when it applies: 'match', 'wrappers', 'when' and 'messages'
    are ignored, and an override whose texts are malformed (placeholders included) leaves the base rule unchanged.
    With reword=False the texts are ignored too.
    """
    import conditions
    import messages
    import rulebuilder

    changes: dict[str, Any] = {}
    if reword:
        texts = {attribute: override[key] for key, attribute in (("message", "message"), ("messageShort", "message_short"),
                                                                 ("description", "description")) if key in override}
        if not all(isinstance(text, str) for text in texts.values()) or not str(texts.get("message", base.message)).strip():
            return base
        try:
            messages.check_texts(messages.rule_texts(base._replace(**texts)), any(conditions.bin_atoms(base.when)),
                                 lambda: rulebuilder.bound_names(base.match))
        except Invalid:
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
    return base._replace(**changes)


def _entries(doc: object, key: str) -> dict[str, dict[str, Any]]:
    return {str(k): v for k, v in view(doc, key).items() if isinstance(v, dict)}


def origins(key: str, managed: object, global_config: object, project_config: object) -> dict[str, list[str]]:
    """Entry id (of 'rules' or 'modes') → the layers that define it, highest precedence first."""
    out: dict[str, list[str]] = {}
    for layer, doc in zip(LAYERS, (managed, global_config, project_config), strict=True):
        for name in _entries(doc, key):
            out.setdefault(name, []).append(layer)
    return out


class Effective(NamedTuple):
    rules: dict[str, Rule]
    problems: dict[str, str]


def project_enabled(project_config: object) -> bool:
    return not isinstance(project_config, dict) or project_config.get("enabled", True) is not False


def effective(managed: object, global_config: object, project_config: object) -> Effective:
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
            if "#" in rid:
                raise Invalid(f"rule id {rid!r} must not contain '#': it separates the id from the rule hash in denials")
            rules[rid] = Rule.from_json(raw)
        except Invalid as exc:
            problems[rid] = str(exc)

    for rid, raw in managed_rules.items():
        add(rid, raw)
        if rid in rules:
            rules[rid] = rules[rid]._replace(modes=tuple(m for m in rules[rid].modes if m in declared))
    for doc in [global_config, *([project_config] if project_enabled(project_config) else [])]:
        for rid, raw in _entries(doc, "rules").items():
            if rid in problems:
                continue
            if rid in rules:
                rules[rid] = merge_rule(rules[rid], raw, rid not in managed_rules)
            else:
                add(rid, raw)
    return Effective(rules, problems)


def effective_rules(managed: object, global_config: object, project_config: object) -> dict[str, Rule]:
    return effective(managed, global_config, project_config).rules


def removed_key_problems(*layers: tuple[str, object]) -> list[str]:
    """One problem per config that still has the `wrappers` key, which is no longer read."""
    return [f"{label} config has a 'wrappers' key: user-defined wrappers are no longer supported, so it is ignored "
            "(remove it)" for label, doc in layers if isinstance(doc, dict) and "wrappers" in doc]


def effective_modes(managed: object, global_config: object, project_config: object) -> dict[str, Mode]:
    """A project (repo-controlled) cannot switch on a mode the managed layer declares."""
    modes: dict[str, Mode] = {}
    declared = set(_entries(managed, "modes"))
    for layer, doc in enumerate((managed, global_config, project_config)):
        for name, raw in _entries(doc, "modes").items():
            if name not in modes:
                modes[name] = Mode.from_json(raw)
            else:
                modes[name] = modes[name].tightened_by(raw, layer < 2 or name not in declared)
    return modes


def _shape_problems(path: str, doc: object) -> list[str]:
    problems = []
    for key in ("rules", "modes"):
        if not isinstance(doc, dict) or key not in doc:
            continue
        table = doc[key]
        if not isinstance(table, dict):
            problems.append(f"managed file {path}: '{key}' must be an object, so all its entries are ignored")
            continue
        problems += [f"managed file {path}: {key} entry '{name}' is not an object and is ignored"
                     for name, entry in table.items() if not isinstance(entry, dict)]
    return problems


def managed_layer(doc: object, path: str) -> tuple[dict[str, Any], list[str]]:
    """The managed file as a layer, and what is wrong with it."""
    problems = _shape_problems(path, doc)
    modes = _entries(doc, "modes")
    rules: dict[str, dict[str, Any]] = {}
    for rid, raw in _entries(doc, "rules").items():
        try:
            rule = Rule.from_json(raw)
        except Invalid as exc:
            problems.append(f"managed rule {rid} is invalid and ignored: {exc}")
            continue
        problems += [f"managed rule {rid} lists mode '{m}', which the managed file does not declare, so it cannot "
                     "suspend the rule" for m in rule.modes if m not in modes]
        rules[rid] = {**raw, "modes": [m for m in rule.modes if m in modes]}
    return {"rules": rules, "modes": modes}, problems


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
