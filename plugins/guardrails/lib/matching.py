"""The one place a command is matched against rules: the hook and every CLI path call evaluate()."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

import astrun
import parity
import policy
import wrappers as wrapper_table
from shellwords import SimpleCommand, simple_commands

PARITY_ENV = "GUARDRAILS_PARITY"
COMPILED = "#compiled"
DEGRADED_KEY = "ast-unavailable"
UNCACHED_HINT = f"uv run --with ast-grep-py=={astrun.PIN} python -c pass"


@dataclass
class Evaluation:
    kinds: dict[str, str | None] = field(default_factory=dict)
    degraded: str | None = None
    invalid: dict[str, str] = field(default_factory=dict)

    def warnings(self) -> list[tuple[str, str]]:
        """(stable key, text) per problem, so a session reports each only once."""
        out = [(f"ast-invalid:{rid}", f"guardrails: match.ast rule {rid} does not compile ({why}) and is skipped")
               for rid, why in sorted(self.invalid.items())]
        if self.degraded:
            text = (f"guardrails: the AST matcher is unavailable ({self.degraded}), so rules with match.ast are "
                    "skipped and every other rule still applies. It needs uv and network access once; "
                    f"try `{UNCACHED_HINT}`.")
            out.append((DEGRADED_KEY, text))
        return out


def lex(command: str, table: wrapper_table.Table | None = None) -> list[SimpleCommand] | None:
    try:
        return simple_commands(command, 0, table)
    except ValueError:
        return None


def synthesized(cmds: list[SimpleCommand] | None) -> list[dict[str, Any]]:
    """The lexer's commands as clean sources, so ast rules still see something when the parse tree is broken."""
    return [{"src": " ".join([*c.assigns, shlex.join([c.name, *c.args])]), "wrapped": c.wrapped}
            for c in cmds or ()]


def best(a: str | None, b: str | None) -> str | None:
    return "direct" if "direct" in (a, b) else (a or b)


def _regex_only(rule: policy.Rule) -> policy.Rule:
    match = policy.view(rule, "match")
    return {**rule, "match": {"regex": match["regex"]} if match.get("regex") else {}}


def clean_error(text: str) -> str:
    return re.sub(r"^\d+:\s*", "", text)


def evaluate(command: str, rules: dict[str, policy.Rule], table: wrapper_table.Table | None = None,
             state_dir: str | None = None) -> Evaluation:
    """How each rule's matcher selects the command: "direct", "wrapped" or None; ast rules need the AST worker."""
    table = table if table is not None else wrapper_table.effective()
    cmds = lex(command, table)
    in_ast = os.environ.get(PARITY_ENV) == "ast"
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
    request = {"op": "eval", "command": command, "rules": asts, "wrappers": table, "lexed": synthesized(cmds)}
    try:
        response = astrun.call(request, state_dir)
    except astrun.Unavailable as exc:
        ev.degraded = str(exc)
        return ev
    for rid, kind in response["verdicts"].items():
        base = rid.removesuffix(COMPILED) if rid.endswith(COMPILED) else rid
        ev.kinds[base] = best(ev.kinds[base], kind)
    for rid, why in response["errors"].items():
        ev.invalid[rid.removesuffix(COMPILED)] = clean_error(why)
    return ev


def check(asts: dict[str, dict[str, Any]], state_dir: str | None = None) -> dict[str, str]:
    """Compile errors per rule id; raises astrun.Unavailable when ast-grep cannot run."""
    if not asts:
        return {}
    response = astrun.call({"op": "check", "rules": asts}, state_dir)
    return {rid: clean_error(why) for rid, why in response["errors"].items()}


def tree(command: str, table: wrapper_table.Table | None = None, state_dir: str | None = None) -> dict[str, Any]:
    table = table if table is not None else wrapper_table.effective()
    return astrun.call({"op": "tree", "command": command, "wrappers": table}, state_dir)
