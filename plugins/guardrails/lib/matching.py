"""The one place a command is matched against rules: the hook and every CLI path call evaluate()."""

from __future__ import annotations

import re
from typing import Any

import policy
import wrappers as wrapper_table

ENGINE_MISSING_KEY = "engine-missing"
MAX_COMMAND = 256 << 10
LIMIT_REASONS = {"depth": "nests shell strings too deeply", "units": "unpacks into too many shell strings",
                 "size": "unpacks into too much shell text"}


class Evaluation:
    """How each rule's matcher selects one command, and what kept a rule from being judged."""

    def __init__(self) -> None:
        self.kinds: dict[str, str | None] = {}
        self.unevaluated: set[str] = set()
        self.invalid: dict[str, str] = {}
        self.outage: Exception | None = None
        self.refusal: str | None = None
        self.rejected: list[str] = []

    def warnings(self) -> list[tuple[str, str]]:
        """(stable key, text) per problem, so a session reports each only once."""
        import astbin
        import astrun

        out = [(text, text) for text in self.rejected]
        out += [(f"rule-invalid:{rid}", f"guardrails: rule {rid} does not compile ({why}) and is skipped")
                for rid, why in sorted(self.invalid.items())]
        affected = sorted(self.unevaluated)
        if isinstance(self.outage, astrun.Missing):
            out.append((ENGINE_MISSING_KEY, astbin.notice(str(self.outage), self.outage.unsupported,
                                                          self.outage.wheel, affected)))
        elif self.outage is not None:
            reason = astbin.sanitised(str(self.outage), 200)
            out.append((f"engine-failed:{reason}", astbin.failure_notice(reason, affected)))
        return out


def best(a: str | None, b: str | None) -> str | None:
    return "direct" if "direct" in (a, b) else (a or b)


def clean_error(text: str) -> str:
    return re.sub(r"^\d+:\s*", "", text)


def evaluate(command: str, rules: dict[str, policy.Rule], wrappers: wrapper_table.Names | None = None,
             state_dir: str | None = None) -> Evaluation:
    """How each rule's matcher selects the command: "direct", "wrapped" or None.

    Regex rules read the raw text. Rules using program, builtin or match.ast need ast-grep; when it cannot run the
    command is left unjudged by them (`unevaluated`) and the reason is kept for the caller to report. Never raises.
    """
    ev = Evaluation()
    for rid, rule in rules.items():
        ev.kinds[rid] = policy.regex_kind(rule, command)
    parsed = {rid: rule for rid, rule in rules.items() if policy.needs_parse(rule)}
    if not parsed:
        return ev
    size = len(command.encode("utf-8", "replace"))
    if size > MAX_COMMAND:
        ev.refusal = f"command too large to check ({size} bytes, the limit is {MAX_COMMAND // 1024} KiB)"
        ev.unevaluated = {rid for rid in parsed if ev.kinds[rid] is None}
        return ev
    import astrun

    names = wrappers if wrappers is not None else wrapper_table.DEFAULTS
    try:
        response = astrun.call({"op": "eval", "command": command, "rules": parsed, "wrappers": list(names)}, state_dir)
    except Exception as exc:  # noqa: BLE001
        ev.outage = exc if isinstance(exc, astrun.Unavailable) else astrun.Unavailable(
            f"unexpected error: {type(exc).__name__}")
        ev.unevaluated = {rid for rid in parsed if ev.kinds[rid] is None}
    else:
        for rid, kind in response["verdicts"].items():
            if rid in ev.kinds and kind in ("direct", "wrapped"):
                ev.kinds[rid] = best(ev.kinds[rid], kind)
        ev.invalid = {rid: clean_error(str(why)) for rid, why in response["errors"].items()}
        ev.unevaluated = {rid for rid in ev.invalid if ev.kinds.get(rid) is None}
        if response.get("limit"):
            ev.refusal = f"command too complex to check (it {LIMIT_REASONS[response['limit']]})"
    ev.rejected = astrun.take_rejected()
    return ev


def check(rules: dict[str, policy.Rule], wrappers: wrapper_table.Names | None = None,
          state_dir: str | None = None) -> dict[str, str]:
    """Compile errors per rule id; raises astrun.Unavailable when ast-grep cannot run."""
    parsed = {rid: rule for rid, rule in rules.items() if policy.needs_parse(rule)}
    if not parsed:
        return {}
    import astrun

    names = wrappers if wrappers is not None else wrapper_table.DEFAULTS
    response = astrun.call({"op": "check", "rules": parsed, "wrappers": list(names)}, state_dir)
    return {rid: clean_error(str(why)) for rid, why in response["errors"].items()}


def tree(command: str, wrappers: wrapper_table.Names | None = None, state_dir: str | None = None) -> dict[str, Any]:
    import astrun

    names = wrappers if wrappers is not None else wrapper_table.DEFAULTS
    return astrun.call({"op": "tree", "command": command, "wrappers": list(names)}, state_dir)
