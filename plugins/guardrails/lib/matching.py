"""The one place a command is matched against rules: the hook and every CLI path call evaluate()."""

from __future__ import annotations

import re
from typing import Any

import policy
import wrappers as wrapper_table
from shellwords import SimpleCommand, simple_commands

PARITY: bool = False
COMPILED = "#compiled"
DEGRADED_KEY = "ast-unavailable"
LIMITED_KEY = "ast-limited"
OVERSIZE_KEY = "command-oversize"
LIMIT_TEXT = {"depth": "nests wrappers or shells too deeply to analyse completely",
              "units": "expands into too many distinct variants to analyse completely",
              "size": "is too large once its wrappers and shell strings are unwrapped to analyse completely"}
MAX_PARSE = 16384


class Evaluation:
    def __init__(self, degraded: str | None = None, oversize: bool = False) -> None:
        self.kinds: dict[str, str | None] = {}
        self.degraded = degraded
        self.missing = False
        self.unsupported = False
        self.wheel_ok = True
        self.invalid: dict[str, str] = {}
        self.approx: dict[str, list[str]] = {}
        self.approx_reason = ""
        self.limited = False
        self.limit_reason: str | None = None
        self.oversize = oversize
        self.rejected: list[str] = []

    def warnings(self) -> list[tuple[str, str]]:
        """(stable key, text) per problem, so a session reports each only once."""
        out = [(text, text) for text in self.rejected]
        out += [(f"ast-invalid:{rid}", f"guardrails: match.ast rule {rid} does not compile ({why}) and is skipped")
               for rid, why in sorted(self.invalid.items())]
        if self.degraded and self.missing:
            import astbin

            out.append((DEGRADED_KEY, astbin.notice(self.degraded, self.unsupported, self.wheel_ok)))
        elif self.degraded:
            text = (f"guardrails: the AST matcher is unavailable ({self.degraded}). Rules with match.ast are applied "
                    "only when the command mentions one of their command names; every other rule is unaffected.")
            out.append((DEGRADED_KEY, text))
        if self.limited:
            text = (f"guardrails: a command {LIMIT_TEXT.get(self.limit_reason or '', LIMIT_TEXT['depth'])}, so rules "
                    "with match.ast are applied only when it mentions their command names.")
            out.append((LIMITED_KEY, text))
        if self.oversize:
            text = (f"guardrails: a command larger than {MAX_PARSE // 1024} KiB is not parsed, so rules are applied "
                    "only when it mentions their command names (regex rules still run).")
            out.append((OVERSIZE_KEY, text))
        return out


def lex(command: str, table: wrapper_table.Table | None = None) -> list[SimpleCommand] | None:
    try:
        return simple_commands(command, 0, table)
    except ValueError:
        return None


def synthesized(cmds: list[SimpleCommand] | None) -> list[dict[str, Any]]:
    """The lexer's distinct commands as clean sources, so ast rules also see what the lexer sees."""
    import shlex

    found = {(" ".join([*c.assigns, shlex.join([c.name, *c.args])]), c.wrapped) for c in cmds or ()}
    return [{"src": src, "wrapped": wrapped} for src, wrapped in sorted(found)]


def best(a: str | None, b: str | None) -> str | None:
    return "direct" if "direct" in (a, b) else (a or b)


def _regex_only(rule: policy.Rule) -> policy.Rule:
    match = policy.view(rule, "match")
    return {**rule, "match": {"regex": match["regex"]} if match.get("regex") else {}}


def clean_error(text: str) -> str:
    return re.sub(r"^\d+:\s*", "", text)


def apply_mentions(ev: Evaluation, command: str, rules: dict[str, policy.Rule], rids: list[str], reason: str) -> None:
    """Treat a rule as matching when the command names one of its commands, for rules the matcher could not judge."""
    normal = policy.normalized(command)
    for rid in rids:
        names = policy.mentions_of(rules[rid])
        if ev.kinds.get(rid) is None and names and policy.mentioned(command, names, normal):
            ev.kinds[rid] = "direct"
            ev.approx[rid] = names
            ev.approx_reason = ev.approx_reason or reason


def oversize(command: str, rules: dict[str, policy.Rule]) -> Evaluation:
    ev = Evaluation(oversize=True)
    for rid, rule in rules.items():
        regex = policy.view(rule, "match").get("regex")
        ev.kinds[rid] = "direct" if regex and re.search(regex, command) is not None else None
    apply_mentions(ev, command, rules, list(rules), f"the command is larger than {MAX_PARSE // 1024} KiB")
    return ev


def plain(command: str, rules: dict[str, policy.Rule], reason: str, fast: bool = False) -> Evaluation:
    """Stdlib matchers plus command-name matching for ast rules, for when nothing else can run."""
    ev = Evaluation(degraded=reason)
    cmds = lex(command) if len(command) <= MAX_PARSE and not fast else None
    for rid, rule in rules.items():
        ev.kinds[rid] = policy.match_kind(rule, command, cmds)
    apply_mentions(ev, command, rules, list(rules), reason)
    return ev


def evaluate(command: str, rules: dict[str, policy.Rule], table: wrapper_table.Table | None = None,
             state_dir: str | None = None) -> Evaluation:
    """How each rule's matcher selects the command: "direct", "wrapped" or None; ast rules need the AST worker.

    Never raises: an unexpected failure degrades to matching by command name and says so.
    """
    if len(command) > MAX_PARSE:
        return oversize(command, rules)
    try:
        return _evaluate(command, rules, table, state_dir)
    except Exception as exc:  # noqa: BLE001
        ev = Evaluation(degraded=f"unexpected error: {type(exc).__name__}")
        for rid, rule in rules.items():
            regex = policy.view(rule, "match").get("regex")
            ev.kinds[rid] = "direct" if regex and re.search(regex, command) is not None else None
        apply_mentions(ev, command, rules, list(rules), ev.degraded or "")
        return ev


def _evaluate(command: str, rules: dict[str, policy.Rule], table: wrapper_table.Table | None,
              state_dir: str | None) -> Evaluation:
    table = table if table is not None else wrapper_table.effective()
    cmds = lex(command, table)
    in_ast = PARITY
    if in_ast:
        import parity
    ev = Evaluation()
    asts: dict[str, dict[str, Any]] = {}
    for rid, rule in rules.items():
        compiled = parity.compile_rule(rule) if in_ast else None
        ev.kinds[rid] = policy.match_kind(_regex_only(rule) if compiled else rule, command, cmds)
        if compiled:
            asts[rid + COMPILED] = compiled
        ast = policy.ast_of(rule)
        if ast:
            asts[rid] = ast
    if not asts:
        return ev
    import astrun

    ast_rids = [rid for rid in rules if policy.ast_of(rules[rid])]
    request = {"op": "eval", "command": command, "rules": asts, "wrappers": table, "lexed": synthesized(cmds)}
    try:
        response = astrun.call(request, state_dir)
    except astrun.Unavailable as exc:
        ev.degraded = str(exc)
        ev.missing = isinstance(exc, astrun.Missing)
        ev.unsupported = isinstance(exc, astrun.Missing) and exc.unsupported
        ev.wheel_ok = not isinstance(exc, astrun.Missing) or exc.wheel
        ev.rejected = astrun.take_rejected()
        apply_mentions(ev, command, rules, ast_rids, f"the AST matcher is unavailable: {exc}")
        return ev
    except Exception as exc:  # noqa: BLE001
        ev.degraded = f"unexpected error: {type(exc).__name__}"
        apply_mentions(ev, command, rules, ast_rids, ev.degraded)
        return ev
    ev.rejected = astrun.take_rejected()
    for rid, kind in response["verdicts"].items():
        base = rid.removesuffix(COMPILED) if rid.endswith(COMPILED) else rid
        if base in ev.kinds and kind in ("direct", "wrapped"):
            ev.kinds[base] = best(ev.kinds[base], kind)
    for rid, why in response["errors"].items():
        ev.invalid[rid.removesuffix(COMPILED)] = clean_error(str(why))
    if response.get("limited"):
        ev.limited = True
        ev.limit_reason = response.get("reason")
        apply_mentions(ev, command, rules, ast_rids,
                       f"the command {LIMIT_TEXT.get(ev.limit_reason or '', LIMIT_TEXT['depth'])}")
    return ev


def check(asts: dict[str, dict[str, Any]], state_dir: str | None = None) -> dict[str, str]:
    """Compile errors per rule id; raises astrun.Unavailable when ast-grep cannot run."""
    if not asts:
        return {}
    import astrun

    response = astrun.call({"op": "check", "rules": asts}, state_dir)
    return {rid: clean_error(str(why)) for rid, why in response["errors"].items()}


def tree(command: str, table: wrapper_table.Table | None = None, state_dir: str | None = None) -> dict[str, Any]:
    import astrun

    table = table if table is not None else wrapper_table.effective()
    return astrun.call({"op": "tree", "command": command, "wrappers": table}, state_dir)
