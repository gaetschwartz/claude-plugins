"""The one place a command is matched against rules: the hook and every CLI path call evaluate().

Anything that needs the parser is matched in a forked child with a hard deadline, because a call into ast-grep cannot
be interrupted and a hook that runs out of time lets the command through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, assert_never

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


@dataclass(frozen=True, slots=True)
class Computed:
    """What the checker child sends back: every rule's verdict on one command."""

    kinds: dict[str, Kind | None]
    invalid: dict[str, str] = field(default_factory=dict)
    limit: Limit | None = None
    failure: str | None = None


def probe() -> bool:
    """In a fresh child: does the library handle a trivial command?"""
    import scanner

    return scanner.self_test() is None


def parseable(command: str) -> str:
    """The text the parser gets: lone surrogates replaced and NULs blanked, which it cannot take."""
    return command.encode("utf-8", "replace").decode().replace("\x00", " ")


def regexes_of(rules: dict[str, policy.Rule]) -> dict[str, Config]:
    import rulebuilder

    return {rid: config for rid, rule in rules.items() if (config := rulebuilder.regex_config(rule)) is not None}


def compute(command: str, rules: dict[str, policy.Rule], names: wrapper_table.Names) -> Computed:
    """Every rule's verdict on one command; runs in the checker child."""
    try:
        import rulebuilder
        import scanner
    except ImportError as exc:
        return Computed(dict.fromkeys(rules), failure=f"the ast-grep-py library cannot be imported ({type(exc).__name__})")
    if (broken := scanner.self_test()) is not None:
        return Computed(dict.fromkeys(rules), failure=f"the ast-grep-py self-test failed: {broken}")
    try:
        result = scanner.Scanner({rid: rulebuilder.configs_of(rule) for rid, rule in rules.items()}, names,
                                 regexes_of(rules)).run(parseable(command))
    except Exception as exc:  # noqa: BLE001
        return Computed(dict.fromkeys(rules), failure=f"unexpected error: {type(exc).__name__}")
    return Computed(result.kinds(list(rules)), result.invalid, result.limit)


def evaluate(command: str, rules: dict[str, policy.Rule], wrappers: wrapper_table.Names | None = None) -> Evaluation:
    """How each rule's matcher selects the command: "direct", "wrapped" or None.

    What could not be judged (`unevaluated`) and why (`failure`, `refusal`) is kept for the caller to report. Hits
    already found always stand. Never raises.
    """
    ev = Evaluation(kinds=dict.fromkeys(rules))
    names = wrappers if wrappers is not None else wrapper_table.DEFAULTS
    parsed = {rid: rule for rid, rule in rules.items() if policy.needs_parse(rule)}
    if not parsed:
        return ev
    size = len(command.encode("utf-8", "replace"))
    if size > MAX_COMMAND:
        ev.refusal = f"command too large to check ({size} bytes, the limit is {MAX_COMMAND // 1024} KiB)"
        ev.refusal_kind = Refusal.OVERSIZE
        ev.unevaluated = set(parsed)
        return ev
    try:
        result = bounded.call(lambda: compute(command, parsed, names), DEADLINE_SECONDS)
        if result.outcome is bounded.Outcome.CRASHED and bounded.call(probe, PROBE_SECONDS).payload is not True:
            ev.runtime_broken = True
    except OSError as exc:
        ev.failure = f"the checker could not be started ({type(exc).__name__})"
    else:
        match result.outcome:
            case bounded.Outcome.DONE:
                done = result.payload
                assert done is not None
                ev.kinds |= done.kinds
                ev.invalid = dict(done.invalid)
                ev.failure = done.failure
                if done.limit is not None:
                    ev.refusal = f"command too complex to check (it {limit_reason(done.limit)})"
                    ev.refusal_kind = Refusal.COMPLEX
            case bounded.Outcome.TIMEOUT:
                ev.refusal = (f"command too complex to check (it did not finish within {DEADLINE_SECONDS:g} seconds); "
                              "split it up or write it to a script file and run that")
                ev.refusal_kind = Refusal.TIMEOUT
            case bounded.Outcome.CRASHED if ev.runtime_broken:
                ev.failure = "the ast-grep-py library crashes even on a trivial command; the runtime is being rebuilt"
            case bounded.Outcome.CRASHED:
                ev.refusal = "this command crashes the parser"
                ev.refusal_kind = Refusal.CRASH
            case bounded.Outcome.GARBLED:
                ev.failure = "the checker answered with something unreadable"
            case _:
                assert_never(result.outcome)
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
