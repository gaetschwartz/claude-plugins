"""The one place a command is matched against rules: the hook and every CLI path call evaluate().

Anything that needs the parser is matched in a forked child with a hard deadline, because a call into ast-grep cannot
be interrupted and a hook that runs out of time lets the command through.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict

import bounded
import policy
import wrappers as wrapper_table
from verdict import Evaluation, Kind, Limit, Refusal, UnitTree, limit_reason

if TYPE_CHECKING:
    from ast_grep_py import Config

MAX_COMMAND = 256 << 10
DEADLINE_SECONDS = 5.0
PROBE_SECONDS = 3.0


class EngineError(Exception):
    """The ast-grep library is not usable here."""


class Wire(TypedDict):
    kinds: dict[str, str | None]
    invalid: dict[str, str]
    limit: str | None
    failure: str | None


def probe() -> str:
    """In a fresh child: does the library handle a trivial command? ("ok" or "bad")"""
    import scanner

    return "ok" if scanner.self_test() is None else "bad"


def report_broken() -> None:
    """The library crashes even on a trivial command: mark the runtime broken and rebuild it in the background."""
    import bootstrap
    import hostcli

    bootstrap.mark_broken(bootstrap.data_dir())
    hostcli.spawn_ensure()


REPORT_BROKEN = report_broken


def parseable(command: str) -> str:
    """The text the parser gets: lone surrogates replaced and NULs blanked, which it cannot take."""
    return command.encode("utf-8", "replace").decode().replace("\x00", " ")


def regexes_of(rules: dict[str, policy.Rule]) -> dict[str, Config]:
    import rulebuilder

    return {rid: config for rid, rule in rules.items() if (config := rulebuilder.regex_config(rule)) is not None}


def compute(command: str, rules: dict[str, policy.Rule], names: wrapper_table.Names, parse: bool) -> Wire:
    """Every rule's verdict on one command."""
    kinds: dict[str, str | None] = dict.fromkeys(rules)
    wire: Wire = {"kinds": kinds, "invalid": {}, "limit": None, "failure": None}
    parsed = {rid: rule for rid, rule in rules.items() if policy.needs_parse(rule)}
    if not parsed or not parse:
        return wire
    try:
        import rulebuilder
        import scanner
    except ImportError as exc:
        wire["failure"] = f"the ast-grep-py library cannot be imported ({type(exc).__name__})"
        return wire
    broken = scanner.self_test()
    if broken is not None:
        wire["failure"] = f"the ast-grep-py self-test failed: {broken}"
        return wire
    try:
        result = scanner.Scanner({rid: rulebuilder.configs_of(rule) for rid, rule in parsed.items()}, names,
                                 regexes_of(parsed)).run(parseable(command))
    except Exception as exc:  # noqa: BLE001
        wire["failure"] = f"unexpected error: {type(exc).__name__}"
        return wire
    for rid, kind in result.kinds(list(parsed)).items():
        kinds[rid] = kind
    wire["invalid"] = result.invalid
    wire["limit"] = str(result.limit) if result.limit else None
    return wire


def evaluate(command: str, rules: dict[str, policy.Rule], wrappers: wrapper_table.Names | None = None) -> Evaluation:
    """How each rule's matcher selects the command: "direct", "wrapped" or None.

    What could not be judged (`unevaluated`) and why (`failure`, `refusal`) is kept for the caller to report. Hits
    already found always stand. Never raises.
    """
    ev = Evaluation()
    names = wrappers if wrappers is not None else wrapper_table.DEFAULTS
    parsed = {rid for rid, rule in rules.items() if policy.needs_parse(rule)}
    size = len(command.encode("utf-8", "replace"))
    oversize = size > MAX_COMMAND and bool(parsed)
    wire: Wire | None = None
    ev.kinds = dict.fromkeys(rules)
    if not parsed:
        return ev
    try:
        result = bounded.call(lambda: json.dumps(compute(command, rules, names, not oversize)), DEADLINE_SECONDS)
        if result.outcome is bounded.Outcome.CRASHED:
            healthy = bounded.call(probe, PROBE_SECONDS) == bounded.Result(bounded.Outcome.DONE, "ok")
            result = result if healthy else bounded.Result(bounded.Outcome.BROKEN)
    except OSError as exc:
        ev.failure = f"the checker could not be started ({type(exc).__name__})"
        ev.unevaluated = set(parsed)
        return ev
    match result.outcome:
        case bounded.Outcome.DONE:
            wire = json.loads(result.payload)
        case bounded.Outcome.TIMEOUT:
            ev.refusal = (f"command too complex to check (it did not finish within {DEADLINE_SECONDS:g} seconds); "
                          "split it up or write it to a script file and run that")
            ev.refusal_kind = Refusal.TIMEOUT
        case bounded.Outcome.CRASHED:
            ev.refusal = "this command crashes the parser"
            ev.refusal_kind = Refusal.CRASH
        case bounded.Outcome.BROKEN:
            REPORT_BROKEN()
            ev.failure = "the ast-grep-py library crashes even on a trivial command; the runtime is being rebuilt"
    if wire is not None:
        ev.kinds = {rid: Kind(kind) if kind else None for rid, kind in wire["kinds"].items()}
        ev.invalid = dict(wire["invalid"])
        ev.failure = wire["failure"]
        if wire["limit"]:
            ev.refusal = f"command too complex to check (it {limit_reason(Limit(wire['limit']))})"
            ev.refusal_kind = Refusal.COMPLEX
    if oversize:
        ev.refusal = f"command too large to check ({size} bytes, the limit is {MAX_COMMAND // 1024} KiB)"
        ev.refusal_kind = Refusal.OVERSIZE
    if ev.failure is not None or ev.refusal:
        ev.unevaluated = {rid for rid in parsed if ev.kinds.get(rid) is None}
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
    """Compile errors per rule id; raises EngineError when the library cannot run."""
    parsed = {rid: rule for rid, rule in rules.items() if policy.needs_parse(rule)}
    if not parsed:
        return {}
    ensure_engine()
    import rulebuilder
    import scanner

    return scanner.compile_errors({rid: rulebuilder.configs_of(rule) for rid, rule in parsed.items()},
                                  regexes_of(parsed))


def tree(command: str) -> tuple[list[UnitTree], Limit | None]:
    """The parse tree of the command and of each shell-string script it hands to a shell."""
    ensure_engine()
    import scanner

    units, limit = scanner.units_of(parseable(command))
    return [scanner.tree_of(unit) for unit in units], limit
