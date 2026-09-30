"""Evaluate rules against a command with ast-grep: one scan per level of shell-string nesting.

The command is scanned as written. The script of every `bash -c '...'` and the arguments of every `eval` are unquoted
and scanned as units of their own, to a bounded depth; a hit inside one of them counts as wrapped.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, NamedTuple

import astrules
import policy
from astcli import Cli, Hit, Node, RuleError, Src, Unavailable

MAX_DEPTH = 8
MAX_UNITS = 64
MAX_SCRIPT_BYTES = 256 << 10
MAX_RULES_BYTES = 256 * 1024
LEAF_KINDS = ("word", "command_name", "raw_string", "number")
ESCAPED_IN_DOUBLE_QUOTES = re.compile(r'\\([\\"$`])')
WORD_PIECE = re.compile(r"""'([^']*)'|"((?:\\.|[^"\\])*)"|\\(.)|([^'"\\]+)""", re.DOTALL)

Rule = dict[str, Any]


class Limit(Exception):
    """Unpacking shell strings hit a cap: "depth", "units" or "size"."""


class Unit(NamedTuple):
    depth: int
    src: Src
    hits: list[Hit]


def unquote(word: str) -> str:
    """The text one shell word stands for: its single-quoted, double-quoted, backslashed and bare pieces joined."""

    def piece(found: re.Match[str]) -> str:
        single, double, escaped, bare = found.groups()
        if double is not None:
            return ESCAPED_IN_DOUBLE_QUOTES.sub(r"\1", double)
        return next(text for text in (single, escaped, bare) if text is not None)

    return WORD_PIECE.sub(piece, word)


def eval_words(src: Src, hits: list[Hit]) -> list[list[str]]:
    """The unquoted arguments of each `eval` command in a unit, each argument filed under its innermost command."""
    commands = sorted((hit for hit in hits if hit.rule == astrules.EVAL_COMMAND), key=lambda hit: hit.lo)
    args = sorted((hit for hit in hits if hit.rule == astrules.EVAL_ARG), key=lambda hit: hit.lo)
    found: dict[int, list[str]] = {command.lo: [] for command in commands}
    open_commands: list[Hit] = []
    upcoming = iter(commands)
    pending = next(upcoming, None)
    for arg in args:
        while pending is not None and pending.lo <= arg.lo:
            open_commands.append(pending)
            pending = next(upcoming, None)
        while open_commands and open_commands[-1].hi < arg.hi:
            open_commands.pop()
        if open_commands:
            found[open_commands[-1].lo].append(unquote(src.text[arg.lo:arg.hi]))
    return list(found.values())


def scripts_in(src: Src, hits: list[Hit]) -> list[str]:
    """The text run by each `bash -c` string and `eval` in a unit; `eval` joins its arguments like the shell does."""
    scripts = [unquote(src.text[hit.lo:hit.hi]) for hit in hits if hit.rule == astrules.SHELL_SCRIPT]
    scripts += [" ".join(words) for words in eval_words(src, hits)]
    return [script for script in scripts if script.strip()]


def walk(command: str, bodies: dict[str, dict[str, Any]], cli: Cli) -> Iterator[Unit]:
    """The command, then the shell strings it unpacks, level by level, each scanned with every rule in `bodies`."""
    seen = {command}
    level = [command]
    spent = 0
    depth = 0
    while level:
        sources = [Src(text) for text in level]
        found = cli.scan(bodies, sources)
        level = []
        for src, hits in zip(sources, found):
            yield Unit(depth, src, hits)
            for script in scripts_in(src, hits):
                if script in seen:
                    continue
                spent += len(script)
                if depth >= MAX_DEPTH:
                    raise Limit("depth")
                if len(seen) >= MAX_UNITS:
                    raise Limit("units")
                if spent > MAX_SCRIPT_BYTES:
                    raise Limit("size")
                seen.add(script)
                level.append(script)
        depth += 1


def admissible(rules: dict[str, policy.Rule]) -> tuple[dict[str, policy.Rule], dict[str, str]]:
    """The rules small enough to run, in order, and the reason for each one left out."""
    ok: dict[str, policy.Rule] = {}
    errors: dict[str, str] = {}
    total = 0
    for rid, rule in rules.items():
        ast = policy.ast_of(rule)
        size = policy.ast_size(ast) if ast else 0
        if size > policy.MAX_AST_BYTES:
            errors[rid] = f"match.ast is larger than {policy.MAX_AST_BYTES // 1024} KiB ({size} bytes)"
        elif total + size > MAX_RULES_BYTES:
            errors[rid] = f"the enabled rules together exceed {MAX_RULES_BYTES // 1024} KiB of match.ast; this one is skipped"
        else:
            total += size
            ok[rid] = rule
    return ok, errors


def compiling(rules: dict[str, policy.Rule], wrappers: list[str], cli: Cli) -> tuple[dict[str, policy.Rule], dict[str, str]]:
    """Split rules into those ast-grep accepts and the reason for each it rejects."""
    ok: dict[str, policy.Rule] = {}
    errors: dict[str, str] = {}
    for rid, rule in rules.items():
        try:
            cli.check(astrules.documents({rid: rule}, wrappers)[0])
        except RuleError as exc:
            errors[rid] = str(exc)
        else:
            ok[rid] = rule
    return ok, errors


def kind_of(branch: str, depth: int) -> str:
    return "direct" if branch == astrules.DIRECT and depth == 0 else "wrapped"


def verdicts_of(command: str, rules: dict[str, policy.Rule], wrappers: list[str], cli: Cli
                ) -> tuple[dict[str, str | None], str | None]:
    """(how each rule matched: "direct", "wrapped" or None; the reason unpacking stopped early, if it did)."""
    bodies, owners = astrules.documents(rules, wrappers)
    verdicts: dict[str, str | None] = {rid: None for rid in rules}
    try:
        for unit in walk(command, bodies, cli):
            for hit in unit.hits:
                if hit.rule in owners:
                    rid, branch = owners[hit.rule]
                    kinds = (verdicts[rid], kind_of(branch, unit.depth))
                    verdicts[rid] = "direct" if "direct" in kinds else "wrapped"
    except Limit as exc:
        return verdicts, str(exc)
    return verdicts, None


def evaluate(request: dict[str, Any], cli: Cli) -> dict[str, Any]:
    wrappers: list[str] = list(request.get("wrappers") or ())
    rules, errors = admissible(request.get("rules") or {})
    command = request["command"].replace("\x00", " ")
    try:
        verdicts, limit = verdicts_of(command, rules, wrappers, cli)
    except RuleError:
        rules, rejected = compiling(rules, wrappers, cli)
        errors.update(rejected)
        verdicts, limit = verdicts_of(command, rules, wrappers, cli)
    return {"ok": True, "verdicts": verdicts, "errors": errors, "limit": limit, "version": cli.version}


def with_depth(root: Node) -> list[tuple[Node, int]]:
    out: list[tuple[Node, int]] = []
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        out.append((node, depth))
        stack.extend((child, depth + 1) for child in reversed(node.children))
    return out


def broken(cst: Node) -> bool:
    """ERROR nodes, or zero-width nodes the parser invented to recover (MISSING)."""
    return any(n.kind == "ERROR" or (n.lo == n.hi and n is not cst and n.kind not in ("program", "heredoc_body", "heredoc_content"))
               for n in cst.walk())


def rows(ast: Node, cst: Node) -> list[list[Any]]:
    """[depth, kind, text or None] per named node; the complete dump says which ones have no child at all."""
    flat = cst.walk()
    at = 0
    out: list[list[Any]] = []
    for node, depth in with_depth(ast):
        key = (node.kind, node.lo, node.hi, node.missing)
        while at < len(flat) and (flat[at].kind, flat[at].lo, flat[at].hi, flat[at].missing) != key:
            at += 1
        if at >= len(flat):
            raise Unavailable("ast-grep printed two parse trees that disagree")
        partner = flat[at]
        at += 1
        out.append([depth, node.kind, node.text() if not partner.children or node.kind in LEAF_KINDS else None])
    return out


def tree(request: dict[str, Any], cli: Cli) -> dict[str, Any]:
    wrappers: list[str] = list(request.get("wrappers") or ())
    limit: str | None = None
    units: list[Src] = []
    try:
        units.extend(unit.src for unit in walk(request["command"].replace("\x00", " "),
                                               astrules.shell_string_rules(wrappers), cli))
    except Limit as exc:
        limit = str(exc)
    simple = cli.dump(units)
    complete = cli.dump(units, "cst")
    shown = [{"label": "shell string" if i else "", "src": src.text, "nodes": rows(simple[i], complete[i]),
              "broken": broken(complete[i])} for i, src in enumerate(units)]
    return {"ok": True, "units": shown, "limit": limit, "version": cli.version}


def handle(request: dict[str, Any], cli: Cli) -> dict[str, Any]:
    op = request.get("op")
    if op == "eval":
        return evaluate(request, cli)
    if op == "tree":
        return tree(request, cli)
    if op == "check":
        rules, errors = admissible(request.get("rules") or {})
        errors.update(compiling(rules, list(request.get("wrappers") or ()), cli)[1])
        return {"ok": True, "errors": errors, "version": cli.version}
    if op == "ping":
        return {"ok": True, "version": cli.version}
    return {"ok": False, "error": f"unknown op {op!r}"}
