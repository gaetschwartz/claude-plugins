"""AST evaluation of match.ast rules; needs ast-grep-py, so it runs under `uv run --with` (see astrun.py).

Protocol: one JSON request on stdin, one JSON response on stdout. Wrapper look-through works by rewriting the
source (dropping the wrapper's own words, or replacing a shell string by its content) and re-parsing, so
relations such as `inside` still see the real surroundings.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

MAX_DEPTH = 6
MAX_UNITS = 64
CONTEXT_KINDS = frozenset({"pipeline", "command_substitution", "process_substitution"})
REDIRECTS = frozenset({"file_redirect", "heredoc_redirect", "herestring_redirect"})
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DQ_ESCAPE = re.compile(r'\\([\\"$`])')
WORD_ESCAPE = re.compile(r"\\(.)")
SIMPLE_WORD = re.compile(r"[A-Za-z0-9_.+@%:,=-]+")
TRAILING_HOLE = re.compile(r"^(.*\S)\s+\$\$\$$", re.DOTALL)

Rule = dict[str, Any]
Table = dict[str, dict[str, Any]]


def sg_root(src: str) -> Any:
    from ast_grep_py import SgRoot  # ty: ignore[unresolved-import]

    return SgRoot(src, "bash").root()


def version() -> str:
    from importlib.metadata import version as dist_version

    return dist_version("ast-grep-py")


def span(node: Any) -> tuple[int, int]:
    rng = node.range()
    return rng.start.index, rng.end.index


def has_errors(root: Any) -> bool:
    """ERROR nodes, or zero-width nodes the parser invented to recover (MISSING)."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.kind() == "ERROR":
            return True
        lo, hi = span(node)
        if lo == hi and node.kind() not in ("program", "heredoc_body") and node is not root:
            return True
        stack.extend(node.children())
    return False


def dequote(node: Any) -> str:
    kind, text = node.kind(), node.text()
    if kind == "raw_string":
        return text[1:-1]
    if kind == "string":
        return DQ_ESCAPE.sub(r"\1", text[1:-1])
    if kind == "ansi_c_string":
        return text[2:-1]
    if kind == "word":
        return WORD_ESCAPE.sub(r"\1", text)
    if kind == "concatenation":
        return "".join(dequote(c) for c in node.children() if c.is_named())
    return text


def is_flag(node: Any) -> bool:
    text = node.text()
    return node.kind() in ("word", "number") and text.startswith("-") and len(text) > 1


def shell_flag_matches(word: str, flag: str) -> bool:
    if word == flag:
        return True
    return len(flag) == 2 and re.fullmatch(r"-[A-Za-z]*" + re.escape(flag[1]) + r"[A-Za-z]*", word) is not None


def command_parts(node: Any) -> tuple[Any, list[Any]] | None:
    """(command_name node, argument nodes without redirects) of a `command` node."""
    name = node.field("name")
    if name is None:
        return None
    seen = False
    args = []
    for child in node.children():
        if not child.is_named():
            continue
        if not seen:
            seen = child.range() == name.range() and child.kind() == "command_name"
            continue
        if child.kind() not in REDIRECTS:
            args.append(child)
    return name, args


def resolve(entry: dict[str, Any], args: list[Any]) -> tuple[str, Any] | None:
    """("command", index of the wrapped command's first word) or ("shell", script text); None when nothing runs."""
    with_value = set(entry.get("flagsWithValue", ()))
    inert = set(entry.get("noCommandFlags", ()))
    shell = entry.get("shellString")
    i = 0
    while i < len(args):
        word = args[i]
        text = word.text()
        if text == "--":
            i += 1
            break
        if is_flag(word):
            if shell and shell != "rest" and shell_flag_matches(text, shell):
                return ("shell", dequote(args[i + 1])) if i + 1 < len(args) else None
            if text in inert:
                return None
            i += 2 if text in with_value else 1
            continue
        if entry.get("assignments") and ASSIGN.match(text):
            i += 1
            continue
        break
    i += entry.get("skip", 0)
    if i >= len(args):
        return None
    if shell == "rest":
        return "shell", " ".join(dequote(a) for a in args[i:])
    if shell:
        return None
    return "command", i


def simple_word(value: str) -> str | None:
    """The command name a word would run (directory and quotes dropped), when it is a plain name."""
    base = value.rsplit("/", 1)[-1]
    return base if SIMPLE_WORD.fullmatch(base) else None


def apply_edits(src: str, edits: list[tuple[int, int, str]]) -> str:
    kept: list[tuple[int, int, str]] = []
    for lo, hi, text in sorted(edits, key=lambda e: (e[0], e[1])):
        if not kept or lo >= kept[-1][1]:
            kept.append((lo, hi, text))
    for lo, hi, text in reversed(kept):
        src = src[:lo] + text + src[hi:]
    return src


def normalise(src: str) -> str:
    """Drop what precedes the command name without changing what runs: VAR=value prefixes, directories, quotes."""
    edits: list[tuple[int, int, str]] = []
    for cmd in sg_root(src).find_all({"rule": {"kind": "command"}}):
        parts = command_parts(cmd)
        if parts is None:
            continue
        name = parts[0]
        name_lo, name_hi = span(name)
        assigns = [c for c in cmd.children() if c.kind() == "variable_assignment"]
        if assigns:
            edits.append((span(assigns[0])[0], name_lo, ""))
        inner = [c for c in name.children() if c.is_named()]
        plain = simple_word(dequote(inner[0])) if len(inner) == 1 else None
        if plain is not None and plain != name.text():
            edits.append((name_lo, name_hi, plain))
    return apply_edits(src, edits)


def variants(src: str, root: Any, table: Table) -> list[tuple[str, int, int, str]]:
    """(rewritten source, region start, region end, label) for each wrapper command in the tree."""
    out = []
    for cmd in root.find_all({"rule": {"kind": "command"}}):
        parts = command_parts(cmd)
        if parts is None:
            continue
        name, args = parts
        base = name.text().strip("\"'").rsplit("/", 1)[-1]
        entry = table.get(base)
        if entry is None:
            continue
        found = resolve(entry, args)
        if found is None:
            continue
        cmd_lo, cmd_hi = span(cmd)
        name_lo = span(name)[0]
        if found[0] == "command":
            first = args[found[1]]
            first_hi = span(first)[1]
            word = simple_word(dequote(first)) or first.text()
            out.append((src[:name_lo] + word + src[first_hi:], name_lo, name_lo + len(word) + cmd_hi - first_hi,
                        base))
        else:
            replacement = "{ " + normalise(found[1]) + "\n}"
            out.append((src[:cmd_lo] + replacement + src[cmd_hi:], cmd_lo, cmd_lo + len(replacement), base))
    return out


def unit_trees(command: str, table: Table) -> tuple[list[tuple[Any, int, int, str, str]], bool]:
    """Every tree to evaluate: (root, region lo, region hi, label, source); region -1 for the command itself."""
    root = sg_root(command)
    units = [(root, -1, -1, "", command)]
    seen = {command}
    base_src, base_root = command, root
    plain = normalise(command)
    if plain != command:
        base_src, base_root = plain, sg_root(plain)
        units.append((base_root, -1, -1, "", plain))
        seen.add(plain)
    queue = [(base_src, base_root, 0)]
    limited = False
    while queue:
        src, tree, depth = queue.pop(0)
        for new_src, lo, hi, label in variants(src, tree, table):
            if new_src in seen:
                continue
            if depth + 1 > MAX_DEPTH or len(units) >= MAX_UNITS:
                limited = True
                continue
            seen.add(new_src)
            new_root = sg_root(new_src)
            units.append((new_root, lo, hi, label, new_src))
            queue.append((new_src, new_root, depth + 1))
    return units, limited


def kind_of(node: Any, derived: bool) -> str:
    if derived or any(a.kind() in CONTEXT_KINDS for a in node.ancestors()):
        return "wrapped"
    return "direct"


def widen(rule: Any) -> Any:
    """A pattern ending in ` $$$` also selects the command when it has no arguments (the bare hole never does)."""
    if isinstance(rule, list):
        return [widen(item) for item in rule]
    if not isinstance(rule, dict):
        return rule
    out = {key: value if key == "pattern" else widen(value) for key, value in rule.items()}
    pattern = rule.get("pattern")
    found = TRAILING_HOLE.match(pattern.strip()) if isinstance(pattern, str) else None
    if not found:
        return out
    either = {"any": [{"pattern": pattern}, {"pattern": {"context": found.group(1), "selector": "command"}}]}
    keep = {k: v for k, v in out.items() if k in ("stopBy", "field")}
    rest = {k: v for k, v in out.items() if k not in ("pattern", "stopBy", "field")}
    return {"all": [either, rest] if rest else [either], **keep}


def prepare(rules: dict[str, Rule]) -> tuple[dict[str, Rule], dict[str, str]]:
    """(rules ready to run, compile error per rule id)."""
    probe = sg_root("true")
    ready: dict[str, Rule] = {}
    errors: dict[str, str] = {}
    for rid, rule in rules.items():
        for candidate in (widen(rule), rule):
            try:
                probe.find({"rule": candidate})
            except Exception as exc:  # noqa: BLE001
                lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()] or [type(exc).__name__]
                errors[rid] = lines[-1] if lines[0].startswith("cannot get matcher") else lines[0]
                continue
            ready[rid] = candidate
            errors.pop(rid, None)
            break
    return ready, errors


def best(current: str | None, new: str) -> str:
    return "direct" if "direct" in (current, new) else new


def evaluate(request: dict[str, Any]) -> dict[str, Any]:
    rules: dict[str, Rule] = request.get("rules") or {}
    live, errors = prepare(rules)
    verdicts: dict[str, str | None] = {rid: None for rid in live}
    units, limited = unit_trees(request["command"], request.get("wrappers") or {})
    broken = limited or any(has_errors(u[0]) for u in units)
    for root, lo, hi, _label, _src in units:
        for rid, rule in live.items():
            for node in root.find_all({"rule": rule}):
                if lo >= 0:
                    n_lo, n_hi = span(node)
                    if not (n_lo < hi and n_hi > lo):
                        continue
                verdicts[rid] = best(verdicts[rid], kind_of(node, lo >= 0))
    if broken:
        for item in request.get("lexed") or ():
            root = sg_root(item["src"])
            for rid, rule in live.items():
                for node in root.find_all({"rule": rule}):
                    verdicts[rid] = best(verdicts[rid], "wrapped" if item.get("wrapped") else kind_of(node, False))
    return {"ok": True, "verdicts": verdicts, "errors": errors, "broken": broken, "version": version()}


def dump(node: Any, depth: int, out: list[list[Any]]) -> None:
    if node.is_named():
        leaf = node.is_leaf() or node.kind() in ("word", "command_name", "raw_string", "number")
        out.append([depth, node.kind(), node.text() if leaf else None])
    for child in node.children():
        dump(child, depth + 1 if node.is_named() else depth, out)


def tree(request: dict[str, Any]) -> dict[str, Any]:
    units, limited = unit_trees(request["command"], request.get("wrappers") or {})
    shown = []
    for root, lo, _hi, label, src in units:
        nodes: list[list[Any]] = []
        dump(root, 0, nodes)
        shown.append({"label": label if lo >= 0 else "", "src": src, "nodes": nodes, "broken": has_errors(root)})
    return {"ok": True, "units": shown, "limited": limited, "version": version()}


def handle(request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    if op == "eval":
        return evaluate(request)
    if op == "tree":
        return tree(request)
    if op == "check":
        return {"ok": True, "errors": prepare(request.get("rules") or {})[1], "version": version()}
    if op == "ping":
        return {"ok": True, "version": version()}
    return {"ok": False, "error": f"unknown op {op!r}"}


def main() -> int:
    try:
        response = handle(json.load(sys.stdin))
    except Exception as exc:  # noqa: BLE001
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    json.dump(response, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
