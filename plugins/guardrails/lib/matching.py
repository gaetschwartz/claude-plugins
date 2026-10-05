"""The one place a command is matched against rules: the hook and every CLI path call evaluate().

Anything that needs the parser is matched in a forked child with a hard deadline, because a call into ast-grep cannot
be interrupted and a hook that runs out of time lets the command through.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, assert_never

import bounded
import conditions
import messages
import policy
from verdict import MAX_COMMAND_BYTES, Detail, Evaluation, Fault, Kind, Limit, UnitTree

if TYPE_CHECKING:
    from ast_grep_py import Config, SgNode
    from scanner import Describe

HOOK_SECONDS = 10.0
HEADROOM_SECONDS = 2.0
DEADLINE_SECONDS = HOOK_SECONDS / 2
PROBE_SECONDS = 3.0
REPROBE_SECONDS = 6.0


class EngineError(Exception):
    """The ast-grep library is not usable here."""


class Computed(NamedTuple):
    """What the checker child sends back: every rule's verdict on one command."""

    kinds: dict[str, Kind | None]
    invalid: dict[str, str]
    limit: Limit | None
    failure: str | None
    micros: dict[str, int]
    parse_us: int
    details: dict[str, Detail]

    @classmethod
    def from_json(cls, raw: object) -> Computed:
        """Rebuild what `_asdict` sent; raises KeyError, TypeError or ValueError on anything else."""
        if not isinstance(raw, dict):
            raise TypeError("not an object")
        return cls({rid: Kind(kind) if kind else None for rid, kind in raw["kinds"].items()}, dict(raw["invalid"]),
                   Limit(raw["limit"]) if raw["limit"] else None, raw["failure"], dict(raw["micros"]),
                   int(raw["parse_us"]), {rid: detail_of(value) for rid, value in raw["details"].items()})


def detail_of(raw: object) -> Detail:
    """A Detail from the checker's JSON, held to the caps on captures."""
    if not isinstance(raw, list | tuple) or len(raw) != 2:
        raise TypeError("not a detail")
    case, captures = raw
    if not (case is None or (isinstance(case, int) and not isinstance(case, bool))) or not isinstance(captures, dict) \
            or len(captures) > messages.MAX_CAPTURES:
        raise ValueError("malformed detail")
    for name, text in captures.items():
        if not (isinstance(name, str) and isinstance(text, str) and len(text) <= messages.MAX_CAPTURE_CHARS + 1):
            raise ValueError("malformed capture")
    return Detail(case, dict(captures))


class NodeHit:
    """The node that decided a rule's verdict, for the hit atoms of message cases."""

    __slots__ = ("node", "wrapped")

    def __init__(self, node: SgNode, wrapped: bool) -> None:
        self.node = node
        self.wrapped = wrapped

    def matches(self, rule: Mapping[str, Any]) -> bool:
        import rulebuilder

        return self.node.matches(**rulebuilder.built(dict(rule)))


def describer(rules: dict[str, policy.Rule], env: conditions.Env) -> Describe | None:
    """What the checker reads from a hit for a rule's message: the case that holds and the captures its texts name.
    None when no rule has cases or capture placeholders, so the common path does no extra work."""
    wanted = {rid: rule for rid, rule in rules.items()
              if rule.messages or messages.captures_wanted((rule.message, rule.message_short))}
    if not wanted:
        return None

    def describe(rid: str, node: SgNode, kind: Kind) -> Detail | None:
        rule = wanted.get(rid)
        if rule is None:
            return None
        hit = NodeHit(node, kind is Kind.WRAPPED)
        case = next((i for i, c in enumerate(rule.messages) if conditions.holds(c.when, env, hit)), None)
        names = messages.captures_wanted(messages.chosen(rule, case))
        captures = {name: text for name in names if (text := messages.capture(node, name)) is not None}
        return Detail(case, captures)

    return describe


class Health(StrEnum):
    HEALTHY = "healthy"
    BROKEN = "broken"
    UNVERIFIED = "unverified"


def probe() -> bool:
    """In a fresh child: does the library handle a trivial command?"""
    import scanner

    return scanner.self_test() is None


def health(started: float) -> Health:
    """Probe the library after a crash, within what is left of the hook's time. A probe that does not answer is
    retried once with longer; a library that never answers is unverified, not broken."""
    for seconds in (PROBE_SECONDS, REPROBE_SECONDS):
        left = HOOK_SECONDS - HEADROOM_SECONDS - (time.monotonic() - started)
        if left < 0.5:
            break
        result = bounded.call(probe, min(seconds, left))
        if result.outcome is not bounded.Outcome.TIMEOUT:
            return Health.HEALTHY if result.payload is True else Health.BROKEN
    return Health.UNVERIFIED


def parseable(command: str) -> str:
    """The text the parser gets: lone surrogates replaced and NULs blanked, which it cannot take."""
    return command.encode("utf-8", "replace").decode().replace("\x00", " ")


def configs_of(rules: dict[str, policy.Rule]) -> dict[str, Config]:
    import rulebuilder

    return {rid: rulebuilder.config_of(rule) for rid, rule in rules.items()}


def direct_only(rules: dict[str, policy.Rule]) -> frozenset[str]:
    return frozenset(rid for rid, rule in rules.items() if not rule.wrappers)


def compute(command: str, rules: dict[str, policy.Rule], env: conditions.Env = conditions.DEFAULT) -> Computed:
    """Every rule's verdict on one command; runs in the checker child."""
    try:
        import scanner
    except ImportError as exc:
        return Computed(dict.fromkeys(rules), {}, None,
                        f"the ast-grep-py library cannot be imported ({type(exc).__name__})", {}, 0, {})
    try:
        result = scanner.Scanner(configs_of(rules), direct_only(rules), describer(rules, env)).run(parseable(command))
    except Exception as exc:  # noqa: BLE001
        return Computed(dict.fromkeys(rules), {}, None, f"unexpected error: {type(exc).__name__}", {}, 0, {})
    return Computed(result.kinds(list(rules)), result.invalid, result.limit, None, result.micros, result.parse_us,
                    result.details)


def evaluate(command: str, rules: dict[str, policy.Rule], env: conditions.Env = conditions.DEFAULT,
             after_fork: Callable[[], None] | None = None) -> Evaluation:
    """How each rule's matcher selects the command: "direct", "wrapped" or None, and for a hit what its message
    needs (`details`).

    What could not be judged (`unevaluated`) and why (`failure`, `refusal`) is kept for the caller to report. Hits
    already found always stand. Never raises. `after_fork` runs in this process once the checker child exists.
    """
    ev = Evaluation(kinds=dict.fromkeys(rules))
    if not rules:
        return ev
    size = len(command.encode("utf-8", "replace"))
    if size > MAX_COMMAND_BYTES:
        ev.refusal = f"command too large to check ({size} bytes, the limit is {MAX_COMMAND_BYTES // 1024} KiB)"
        ev.unevaluated = set(rules)
        ev.fault = Fault.OVERSIZE
        return ev
    started = time.monotonic()
    state = Health.HEALTHY
    try:
        result = bounded.call(lambda: compute(command, rules, env)._asdict(), DEADLINE_SECONDS, after_fork)
        if result.outcome is bounded.Outcome.CRASHED:
            state = health(started)
            ev.runtime_broken = state is Health.BROKEN
    except OSError as exc:
        ev.failure = f"the checker could not be started ({type(exc).__name__})"
    else:
        outcome, done = result.outcome, None
        if outcome is bounded.Outcome.DONE:
            try:
                done = Computed.from_json(result.payload)
            except (KeyError, TypeError, ValueError, AttributeError):
                outcome = bounded.Outcome.GARBLED
        match outcome:
            case bounded.Outcome.DONE:
                assert done is not None
                ev.kinds |= done.kinds
                ev.details = done.details
                ev.invalid = dict(done.invalid)
                ev.failure = done.failure
                ev.micros, ev.parse_us = done.micros, done.parse_us
                if done.limit is not None:
                    ev.refusal = f"command too complex to check (it {done.limit})"
                    ev.fault = Fault.COMPLEXITY
            case bounded.Outcome.TIMEOUT:
                ev.refusal = (f"command too complex to check (it did not finish within {DEADLINE_SECONDS:g} seconds); "
                              "split it up or write it to a script file and run that")
                ev.fault = Fault.TIMEOUT
            case bounded.Outcome.CRASHED:
                match state:
                    case Health.HEALTHY:
                        ev.refusal = "this command crashes the parser"
                        ev.fault = Fault.CRASH
                    case Health.BROKEN:
                        ev.failure = ("the ast-grep-py library crashes even on a trivial command; the runtime is being "
                                      "rebuilt")
                    case Health.UNVERIFIED:
                        ev.failure = "could not verify the matcher (timed out)"
                    case _:
                        assert_never(state)
            case bounded.Outcome.GARBLED:
                ev.failure = "the checker answered with something unreadable"
            case _:
                assert_never(outcome)
    if ev.failure is not None:
        ev.fault = ev.fault or Fault.ENGINE
    if ev.failure is not None or ev.refusal:
        ev.unevaluated = {rid for rid in rules if ev.kinds.get(rid) is None}
    ev.unevaluated |= {rid for rid in ev.invalid if ev.kinds.get(rid) is None}
    return ev


def ensure_engine() -> None:
    """Raise EngineError unless the library imports and passes its self-test."""
    try:
        import scanner
    except ImportError as exc:
        raise EngineError(f"the ast-grep-py library cannot be imported ({type(exc).__name__})") from exc
    broken = scanner.self_test()
    if broken is not None:
        raise EngineError(f"the ast-grep-py self-test failed: {broken}")


def check(rules: dict[str, policy.Rule]) -> dict[str, str]:
    """Compile errors per rule id, of its match and of each `matches` in its message cases; raises EngineError when
    the library cannot run."""
    if not rules:
        return {}
    ensure_engine()
    import rulebuilder
    import scanner

    errors = scanner.compile_errors(configs_of(rules))
    for rid, rule in rules.items():
        subrules = [sub for case in rule.messages for sub in conditions.match_atoms(case.when)]
        if rid in errors or not subrules:
            continue
        broken = scanner.compile_errors({str(i): {"rule": rulebuilder.built(sub)} for i, sub in enumerate(subrules)})
        if broken:
            at, why = min(broken.items(), key=lambda item: int(item[0]))
            errors[rid] = f"a matches in messages, {json_compact(subrules[int(at)])}: {why}"
    return errors


def json_compact(value: object) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def tree(command: str) -> tuple[list[UnitTree], Limit | None]:
    """The parse tree of the command and of each shell-string script it hands to a shell."""
    ensure_engine()
    import scanner

    trees: list[UnitTree] = []
    limit = scanner.walk(parseable(command), False, lambda unit, root: trees.append(scanner.tree_of(unit, root)))
    return trees, limit
