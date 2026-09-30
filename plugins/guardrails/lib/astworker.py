"""AST evaluation of match.ast rules through the ast-grep binary.

Wrapper look-through works by rewriting the source (dropping the wrapper's own words, or replacing a shell string by
its content) and re-parsing, so relations such as `inside` still see the real surroundings.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import ansic
from astcli import Cli, Node, RuleError, Src, Unavailable

MAX_DEPTH = 16
MAX_UNITS = 512
EXPANSION_BUDGET = 0.5
CONTEXT = {"any": [{"kind": "pipeline"}, {"kind": "command_substitution"}, {"kind": "process_substitution"}],
           "stopBy": "end"}
REDIRECTS = frozenset({"file_redirect", "heredoc_redirect", "herestring_redirect"})
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DQ_ESCAPE = re.compile(r'\\([\\"$`])')
WORD_ESCAPE = re.compile(r"\\(.)")
SIMPLE_WORD = re.compile(r"[A-Za-z0-9_.+@%:,=-]+")
TRAILING_HOLE = re.compile(r"^(.*\S)\s+\$\$\$$", re.DOTALL)
REWRITABLE = re.compile(r"['\"\\/=]")
YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff]")
LEAF_KINDS = ("word", "command_name", "raw_string", "number")

Rule = dict[str, Any]
Table = dict[str, dict[str, Any]]


class Unit:
    """One tree to evaluate: the command as written, its normalised form, or a rewrite exposed by a wrapper."""

    __slots__ = ("hi", "label", "lo", "src", "tree")

    def __init__(self, src: str, lo: int = -1, hi: int = -1, label: str = "", tree: Node | None = None) -> None:
        self.src, self.lo, self.hi, self.label, self.tree = src, lo, hi, label, tree

    @property
    def derived(self) -> bool:
        return self.lo >= 0


def dequote(node: Node) -> str:
    kind, text = node.kind, node.text()
    if kind == "raw_string":
        return text[1:-1]
    if kind == "string":
        return DQ_ESCAPE.sub(r"\1", text[1:-1])
    if kind == "ansi_c_string":
        return ansic.decode(text[2:-1])
    if kind == "word":
        return WORD_ESCAPE.sub(r"\1", text)
    if kind == "concatenation":
        return "".join(dequote(c) for c in node.named_children())
    return text


def is_flag(node: Node) -> bool:
    return node.kind in ("word", "number", "concatenation") and node.text().startswith("-")


def cluster_takes_value(word: str, flags: set[str]) -> bool:
    if len(word) < 3 or word[1] == "-" or not word[1:].isalpha():
        return False
    for i, letter in enumerate(word[1:]):
        if f"-{letter}" in flags:
            return i == len(word) - 2
    return False


def shell_flag_matches(word: str, flag: str) -> bool:
    if word == flag:
        return True
    return len(flag) == 2 and re.fullmatch(r"-[A-Za-z]*" + re.escape(flag[1]) + r"[A-Za-z]*", word) is not None


def command_parts(node: Node) -> tuple[Node, list[Node]] | None:
    """(command_name node, argument nodes without redirects) of a `command` node."""
    name = next((c for c in node.children if c.field == "name"), None)
    if name is None:
        return None
    seen = False
    args = []
    for child in node.named_children():
        if not seen:
            seen = child is name and child.kind == "command_name"
            continue
        if child.kind not in REDIRECTS:
            args.append(child)
    return name, args


def resolve(entry: dict[str, Any], args: list[Node]) -> tuple[str, Any] | None:
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
                at = i + 1 + (1 if i + 1 < len(args) and args[i + 1].text() == "--" else 0)
                return ("shell", dequote(args[at])) if at < len(args) else None
            if text in inert:
                return None
            i += 2 if text in with_value or cluster_takes_value(text, with_value) else 1
            continue
        if entry.get("assignments") and ASSIGN.match(dequote(word)):
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


def commands_of(tree: Node) -> list[Node]:
    return [n for n in tree.walk() if n.kind == "command"]


def normalise(src: str, tree: Node) -> str:
    """Drop what precedes the command name without changing what runs: VAR=value prefixes, directories, quotes."""
    edits: list[tuple[int, int, str]] = []
    for cmd in commands_of(tree):
        parts = command_parts(cmd)
        if parts is None:
            continue
        name = parts[0]
        assigns = [c for c in cmd.named_children() if c.kind == "variable_assignment"]
        if assigns:
            edits.append((assigns[0].lo, name.lo, ""))
        inner = name.named_children()
        plain = simple_word(dequote(inner[0])) if len(inner) == 1 else None
        if plain is not None and plain != name.text():
            edits.append((name.lo, name.hi, plain))
    return apply_edits(src, edits)


def normalise_shell(script: str, cli: Cli) -> str:
    """normalise() of a shell string; without quotes, slashes or assignments there is nothing to rewrite."""
    if not REWRITABLE.search(script):
        return script
    return normalise(script, cli.dump([Src(script)])[0])


def variants(src: str, tree: Node, table: Table, cli: Cli, within: tuple[int, int] | None = None
             ) -> list[tuple[str, int, int, str]]:
    """(rewritten source, region start, region end, label) for each wrapper command in the tree.

    After the first round only commands inside the region a rewrite exposed are expanded: the others were already
    handled from the original tree, and expanding their combinations would grow without bound.
    """
    out = []
    for cmd in commands_of(tree):
        parts = command_parts(cmd)
        if parts is None:
            continue
        if within is not None and not (cmd.lo < within[1] and cmd.hi > within[0]):
            continue
        name, args = parts
        base = name.text().strip("\"'").rsplit("/", 1)[-1]
        entry = table.get(base)
        if entry is None:
            continue
        found = resolve(entry, args)
        if found is None:
            continue
        if found[0] == "command":
            first = args[found[1]]
            word = simple_word(dequote(first)) or first.text()
            out.append((src[:name.lo] + word + src[first.hi:], name.lo, name.lo + len(word) + cmd.hi - first.hi, base))
        else:
            replacement = "{ " + normalise_shell(found[1], cli) + "\n}"
            out.append((src[:cmd.lo] + replacement + src[cmd.hi:], cmd.lo, cmd.lo + len(replacement), base))
    return out


def mentions_wrapper(src: str, table: Table) -> bool:
    return any(name in src for name in table)


def parse_units(units: list[Unit], cli: Cli, wanted: list[bool], until: float | None = None) -> bool:
    """Parse the wanted units; False when `until` passed first."""
    chosen = [u for u, want in zip(units, wanted) if want]
    trees = cli.dump([Src(u.src) for u in chosen], "ast", until)
    for unit, tree in zip(chosen, trees):
        unit.tree = tree
    return len(trees) == len(chosen)


def unit_trees(command: str, table: Table, cli: Cli, everything: bool = False) -> tuple[list[Unit], bool]:
    """Every tree to evaluate; `everything` parses units even when nothing in them can be rewritten or unwrapped.

    Parsing costs a process, so a unit whose text has no wrapper name (and, for the command itself, nothing that
    normalise() would rewrite) is left without a tree: it can expose no further unit.
    """
    first = Unit(command)
    parse_units([first], cli, [everything or bool(REWRITABLE.search(command)) or mentions_wrapper(command, table)])
    units = [first]
    seen = {command}
    base = first
    plain = normalise(command, first.tree) if first.tree else command
    if plain != command:
        base = Unit(plain)
        parse_units([base], cli, [True])
        units.append(base)
        seen.add(plain)
    until = time.monotonic() + EXPANSION_BUDGET
    frontier = [base]
    limited = False
    level = 0
    while frontier and not limited:
        if time.monotonic() > until and any(u.tree for u in frontier):
            limited = True
            break
        fresh: list[Unit] = []
        for unit in frontier:
            within = (unit.lo, unit.hi) if unit.derived else None
            found = variants(unit.src, unit.tree, table, cli, within) if unit.tree else []
            for new_src, lo, hi, label in found:
                if new_src in seen:
                    continue
                if level + 1 > MAX_DEPTH or len(units) + len(fresh) >= MAX_UNITS:
                    limited = True
                    break
                seen.add(new_src)
                fresh.append(Unit(new_src, lo, hi, label))
            if limited:
                break
        units.extend(fresh)
        complete = parse_units(fresh, cli, [everything or mentions_wrapper(u.src, table) for u in fresh], until)
        limited = limited or not complete
        frontier = fresh
        level += 1
    return units, limited


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


def document(rule_id: str, rule: Any) -> str:
    """One ast-grep rule document; JSON is valid YAML, and the characters YAML treats as line breaks stay escaped."""
    text = json.dumps({"id": rule_id, "language": "bash", "rule": rule}, ensure_ascii=False)
    return YAML_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def rule_text(live: dict[str, Rule]) -> tuple[str, dict[str, str]]:
    """(ast-grep rules for every live rule plus its in-context twin, rule id per generated id)."""
    docs, owners = [], {}
    for i, (rid, rule) in enumerate(live.items()):
        docs.append(document(f"g{i}", rule))
        docs.append(document(f"g{i}c", {"all": [rule, {"inside": CONTEXT}]}))
        owners[f"g{i}"] = rid
    return "\n---\n".join(docs), owners


def prepare(rules: dict[str, Rule], cli: Cli) -> tuple[dict[str, Rule], dict[str, str]]:
    """(rules ready to run, compile error per rule id)."""
    if not rules:
        return {}, {}
    widened = {rid: widen(rule) for rid, rule in rules.items()}
    try:
        cli.check(rule_text(widened)[0])
        return widened, {}
    except RuleError:
        pass
    ready: dict[str, Rule] = {}
    errors: dict[str, str] = {}
    for rid, rule in rules.items():
        for candidate in (widened[rid], rule):
            try:
                cli.check(document("g", candidate))
            except RuleError as exc:
                errors[rid] = str(exc)
                continue
            ready[rid] = candidate
            errors.pop(rid, None)
            break
    return ready, errors


def best(current: str | None, new: str) -> str:
    return "direct" if "direct" in (current, new) else new


def verdicts_of(units: list[Unit], lexed: list[dict[str, Any]], hits: list[list[Any]], owners: dict[str, str],
                live: dict[str, Rule]) -> dict[str, str | None]:
    verdicts: dict[str, str | None] = {rid: None for rid in live}
    for at, found in enumerate(hits):
        in_context = {(h.rule, h.lo, h.hi) for h in found if h.rule.endswith("c")}
        unit = units[at] if at < len(units) else None
        for hit in found:
            if hit.rule.endswith("c"):
                continue
            if unit is None:
                tagged = lexed[at - len(units)].get("wrapped") or (hit.rule + "c", hit.lo, hit.hi) in in_context
                kind = "wrapped" if tagged else "direct"
            elif unit.derived:
                if not (hit.lo < unit.hi and hit.hi > unit.lo):
                    continue
                kind = "wrapped"
            else:
                kind = "wrapped" if (hit.rule + "c", hit.lo, hit.hi) in in_context else "direct"
            rid = owners[hit.rule]
            verdicts[rid] = best(verdicts[rid], kind)
    return verdicts


def evaluate(request: dict[str, Any], cli: Cli) -> dict[str, Any]:
    rules: dict[str, Rule] = request.get("rules") or {}
    command = request["command"].replace("\x00", " ")
    lexed: list[dict[str, Any]] = list(request.get("lexed") or ())
    live: dict[str, Rule] = {rid: widen(rule) for rid, rule in rules.items()}
    errors: dict[str, str] = {}
    units, limited = unit_trees(command, request.get("wrappers") or {}, cli) if rules else ([], False)
    sources = [Src(u.src) for u in units] + [Src(item["src"]) for item in lexed]
    hits: list[list[Any]] = []
    while live:
        text, owners = rule_text(live)
        try:
            hits = cli.scan(text, sources)
            break
        except RuleError:
            live, errors = prepare(rules, cli)
    else:
        owners = {}
        hits = [[] for _ in sources]
    return {"ok": True, "verdicts": verdicts_of(units, lexed, hits, owners, live), "errors": errors,
            "limited": limited, "version": cli.version}


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
    return any(n.kind == "ERROR" or (n.lo == n.hi and n is not cst and n.kind not in ("program", "heredoc_body"))
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
    units, limited = unit_trees(request["command"].replace("\x00", " "), request.get("wrappers") or {}, cli, True)
    sources = [Src(u.src) for u in units]
    complete = cli.dump(sources, "cst")
    shown = [{"label": u.label if u.derived else "", "src": u.src, "nodes": rows(u.tree or complete[i], complete[i]),
              "broken": broken(complete[i])} for i, u in enumerate(units)]
    return {"ok": True, "units": shown, "limited": limited, "version": cli.version}


def handle(request: dict[str, Any], cli: Cli) -> dict[str, Any]:
    op = request.get("op")
    if op == "eval":
        return evaluate(request, cli)
    if op == "tree":
        return tree(request, cli)
    if op == "check":
        return {"ok": True, "errors": prepare(request.get("rules") or {}, cli)[1], "version": cli.version}
    if op == "ping":
        return {"ok": True, "version": cli.version}
    return {"ok": False, "error": f"unknown op {op!r}"}
