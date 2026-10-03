"""Evaluate rules against a command with ast-grep-py, in process.

The command is parsed as written. The commands behind each wrapper (sudo, env, xargs, ...) and the scripts handed to
shells (`bash -c '...'`, `eval`, heredocs) are text variants and units of their own, parsed and matched the same way;
a hit inside one of them counts as wrapped.
"""

from __future__ import annotations

import itertools
import math
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

import rulebuilder
from ast_grep_py import Config, SgNode, SgRoot
from verdict import Kind, Limit, UnitTree

MAX_DEPTH = 8
MAX_UNITS = 64
MAX_SCRIPT_BYTES = 256 << 10
MAX_VARIANTS = 2048
MAX_VARIANT_BYTES = 16 << 20
MAX_COMBINATIONS = 64
SHELLS = ("bash", "sh", "zsh", "dash", "ksh", "script")
CONTEXT_KINDS = frozenset({"pipeline", "command_substitution", "process_substitution"})
ARGUMENT_KINDS = frozenset({"raw_string", "string", "word", "number", "concatenation", "simple_expansion", "expansion",
                            "command_substitution", "arithmetic_expansion", "process_substitution"})
LEAF_KINDS = frozenset({"word", "command_name", "raw_string", "number"})
COMMAND_STRING_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
UNQUOTED_DELIMITER = re.compile(r"[A-Za-z0-9_]+")
ESCAPED_IN_DOUBLE_QUOTES = re.compile(r'\\([\\"$`])')
WORD_PIECE = re.compile(r"""'([^']*)'|"((?:\\.|[^"\\])*)"|\\(.)|([^'"\\]+)""", re.DOTALL)
SHELL_NAME = re.compile(rulebuilder.name_regex(SHELLS))
EVAL_NAME = re.compile(rulebuilder.name_regex(["eval"]))
SELF_TEST_COMMAND = "echo a | cat"


class Script(NamedTuple):
    """A script a unit hands to a shell; `restricted` scripts only count inside their own substitutions."""

    text: str
    restricted: bool


class Origin(StrEnum):
    COMMAND = "command"
    VARIANT = "variant"
    SCRIPT = "script"


@dataclass(frozen=True, slots=True)
class Unit:
    text: str
    origin: Origin
    depth: int
    restricted: bool


@dataclass(frozen=True, slots=True)
class Hit:
    rule: str
    kind: Kind
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Scan:
    hits: tuple[Hit, ...]
    invalid: dict[str, str]
    limit: Limit | None

    def kinds(self, rule_ids: Sequence[str]) -> dict[str, Kind | None]:
        found = {hit.rule: hit.kind for hit in self.hits}
        return {rid: found.get(rid) for rid in rule_ids}


class Span(NamedTuple):
    start: int
    end: int
    words: tuple[int, ...]


def unquote(word: str) -> str:
    """The text one shell word stands for: its single-quoted, double-quoted, backslashed and bare pieces joined."""

    def piece(found: re.Match[str]) -> str:
        single, double, escaped, bare = found.groups()
        if double is not None:
            return ESCAPED_IN_DOUBLE_QUOTES.sub(r"\1", double)
        return next(text for text in (single, escaped, bare) if text is not None)

    return WORD_PIECE.sub(piece, word)


def clean_error(text: str) -> str:
    lines = [re.sub(r"^\d+:\s*", "", line.strip()) for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "invalid rule"


def self_test() -> str | None:
    """Why the library cannot parse and match a trivial pipeline, or None when it can."""
    try:
        found = SgRoot(SELF_TEST_COMMAND, "bash").root().find(kind="pipeline")
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__} while parsing a test command"
    return None if found is not None and found.text() == SELF_TEST_COMMAND else "it misparsed a test command"


def name_of(command: SgNode) -> str:
    name = command.field("name")
    return name.text() if name is not None else ""


def arguments(command: SgNode) -> list[SgNode]:
    """The nodes after the command's name (leading assignments come before it)."""
    nodes = command.named_children()
    at = next((i for i, node in enumerate(nodes) if node.kind() == "command_name"), -1)
    return nodes[at + 1:]


def inside_substitution(node: SgNode) -> bool:
    return any(ancestor.kind() == "command_substitution" for ancestor in node.ancestors())


def script_after_flag(words: list[SgNode]) -> list[str]:
    """The script word after each `-c` style flag (or after a `--` that follows it), unquoted."""
    found = []
    for at, word in enumerate(words):
        if not COMMAND_STRING_FLAG.match(word.text()):
            continue
        following = words[at + 1:at + 3]
        if following and following[0].text() == "--":
            following = following[1:]
        if following and following[0].kind() in ARGUMENT_KINDS:
            found.append(unquote(following[0].text()))
    return found


def shell_scripts(command: SgNode) -> list[str]:
    """The scripts one command named like a shell runs: `-c` strings, a here-string, a heredoc fed to it."""
    words = arguments(command)
    found = script_after_flag(words)
    for child in command.named_children():
        if child.kind() == "herestring_redirect":
            found += [unquote(arg.text()) for arg in child.named_children() if arg.kind() in ARGUMENT_KINDS]
    parent = command.parent()
    if parent is not None and parent.kind() == "redirected_statement":
        for sibling in parent.named_children():
            if sibling.kind() == "heredoc_redirect":
                found += [body.text() for body in sibling.named_children() if body.kind() == "heredoc_body"]
    return found


def scripts_in(root: SgNode, restricted: bool) -> list[Script]:
    """(text, restricted) of each script a unit hands to a shell: `bash -c` strings, `eval` arguments (joined like
    the shell does), heredocs and here-strings fed to a shell, and the bodies of unquoted heredocs that contain a
    substitution, which only count inside their substitutions. A restricted unit only yields scripts inside its own."""
    found: list[Script] = []
    for command in root.find_all(kind="command"):
        if restricted and not inside_substitution(command):
            continue
        name = name_of(command)
        if SHELL_NAME.search(name):
            found += [Script(script, False) for script in shell_scripts(command)]
        elif EVAL_NAME.search(name):
            words = [unquote(arg.text()) for arg in arguments(command) if arg.kind() in ARGUMENT_KINDS]
            found.append(Script(" ".join(words), False))
    for redirect in root.find_all(kind="heredoc_redirect"):
        start = next((n for n in redirect.named_children() if n.kind() == "heredoc_start"), None)
        if start is None or not UNQUOTED_DELIMITER.fullmatch(start.text()):
            continue
        if restricted and not inside_substitution(redirect):
            continue
        for body in (n for n in redirect.named_children() if n.kind() == "heredoc_body"):
            text = body.text()
            if "`" in text or "$(" in text:
                found.append(Script(text, True))
    return [script for script in found if script.text.strip()]


def wrapper_spans(root: SgNode, wrappers: re.Pattern[str]) -> list[Span]:
    """Each command named by a wrapper: its byte range and where each of its words after the first node starts."""
    spans = []
    for command in root.find_all(kind="command"):
        if not wrappers.search(name_of(command)):
            continue
        words = command.named_children()[1:]
        if words:
            where = command.range()
            spans.append(Span(where.start.index, where.end.index, tuple(w.range().start.index for w in words)))
    return spans


def variants_of(text: str, spans: Sequence[Span]) -> list[str]:
    """The command text with one wrapper replaced by the text from each of its words on, and with several outermost
    wrappers replaced together: every combination while there are few, else each k-th word of all of them at once."""

    def replaced(chosen: Sequence[tuple[Span, int]]) -> str:
        out = text
        for span, word in sorted(chosen, key=lambda pair: pair[0].start, reverse=True):
            out = out[:span.start] + text[word:span.end] + out[span.end:]
        return out

    texts = [replaced([(span, word)]) for span in spans for word in span.words]
    outermost: list[Span] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if not outermost or span.start >= outermost[-1].end:
            outermost.append(span)
    if len(outermost) > 1:
        if math.prod(len(span.words) + 1 for span in outermost) <= MAX_COMBINATIONS:
            options = [[(span, word) for word in (*span.words, None)] for span in outermost]
            for combination in itertools.product(*options):
                texts.append(replaced([(span, word) for span, word in combination if word is not None]))
        else:
            for k in range(max(len(span.words) for span in outermost)):
                texts.append(replaced([(span, span.words[k]) for span in outermost if len(span.words) > k]))
    return [t for t in dict.fromkeys(texts) if t != text]


class Scanner:
    """One evaluation: the rules, the wrapper names and what has been found so far."""

    def __init__(self, rules: dict[str, tuple[Config, ...]], wrapper_names: Sequence[str],
                 regexes: dict[str, Config] | None = None) -> None:
        self.active = {rid: list(configs) for rid, configs in rules.items() if configs}
        self.regexes = dict(regexes or {})
        self.invalid: dict[str, str] = {}
        self.found: dict[str, Hit] = {}
        self.wrappers = re.compile(rulebuilder.name_regex(wrapper_names)) if wrapper_names else None

    def matches(self, rid: str, root: SgNode, first_only: bool) -> list[SgNode]:
        """The nodes a rule selects; a rule whose configs all fail to compile is dropped with the reason."""
        configs = self.active[rid]
        reason = "invalid rule"
        while configs:
            try:
                if first_only:
                    one = root.find(configs[0])
                    return [one] if one is not None else []
                return root.find_all(configs[0])
            except Exception as exc:  # noqa: BLE001
                reason = clean_error(str(exc))
                configs.pop(0)
        del self.active[rid]
        self.invalid[rid] = reason
        return []

    def kind_of(self, unit: Unit, node: SgNode) -> Kind | None:
        """None when the hit does not count: outside the substitutions of a restricted unit."""
        kinds = {ancestor.kind() for ancestor in node.ancestors()}
        if unit.restricted and "command_substitution" not in kinds:
            return None
        if unit.origin is not Origin.COMMAND or kinds & CONTEXT_KINDS:
            return Kind.WRAPPED
        return Kind.DIRECT

    def judge(self, unit: Unit, root: SgNode) -> None:
        for rid in list(self.active):
            known = self.found.get(rid)
            if known is not None and known.kind is Kind.DIRECT:
                continue
            first_only = unit.origin is not Origin.COMMAND and not unit.restricted
            for node in self.matches(rid, root, first_only):
                kind = Kind.WRAPPED if first_only else self.kind_of(unit, node)
                if kind is None or (known is not None and kind is Kind.WRAPPED):
                    continue
                where = node.range()
                self.found[rid] = known = Hit(rid, kind, where.start.index, where.end.index)
                if kind is Kind.DIRECT:
                    break

    def judge_regexes(self, root: SgNode) -> None:
        """`match.regex` reads the raw text of the command as written: a hit there is always direct."""
        for rid, config in self.regexes.items():
            try:
                hit = root.find(config)
            except Exception as exc:  # noqa: BLE001
                self.invalid[rid] = "match.regex is not valid Rust regex syntax: " + clean_error(str(exc))
                continue
            if hit is not None:
                where = hit.range()
                self.found[rid] = Hit(rid, Kind.DIRECT, where.start.index, where.end.index)

    def run(self, command: str) -> Scan:
        """Scan the command, then its variants and scripts level by level. Hits found before a cap is reached stand."""
        queue = deque([Unit(command, Origin.COMMAND, 0, False)])
        seen = {(command, False, False)}
        scripts = variants = script_bytes = variant_bytes = 0
        limit: Limit | None = None
        while queue and limit is None:
            unit = queue.popleft()
            root = SgRoot(unit.text, "bash").root()
            if unit.depth == 0 and unit.origin is Origin.COMMAND:
                self.judge_regexes(root)
            self.judge(unit, root)
            found: list[Unit] = []
            if unit.origin is not Origin.VARIANT and self.wrappers is not None:
                found += [Unit(text, Origin.VARIANT, unit.depth, unit.restricted)
                          for text in variants_of(unit.text, wrapper_spans(root, self.wrappers))]
            found += [Unit(text, Origin.SCRIPT, unit.depth + 1, only) for text, only in scripts_in(root, unit.restricted)]
            for new in found:
                key = (new.text, new.restricted, new.origin is Origin.VARIANT)
                if key in seen:
                    continue
                if new.origin is Origin.VARIANT:
                    variants += 1
                    variant_bytes += len(new.text)
                    limit = Limit.VARIANTS if variants > MAX_VARIANTS else (
                        Limit.VARIANT_BYTES if variant_bytes > MAX_VARIANT_BYTES else None)
                else:
                    scripts += 1
                    script_bytes += len(new.text)
                    limit = (Limit.DEPTH if new.depth > MAX_DEPTH else Limit.UNITS if scripts > MAX_UNITS
                             else Limit.SIZE if script_bytes > MAX_SCRIPT_BYTES else None)
                if limit is not None:
                    break
                seen.add(key)
                queue.append(new)
        return Scan(tuple(self.found.values()), dict(self.invalid), limit)


def compile_errors(configs: dict[str, tuple[Config, ...]], regexes: dict[str, Config] | None = None) -> dict[str, str]:
    """The reason per rule id none of whose configs compiles, or whose regex does not."""
    scanner = Scanner(configs, (), regexes)
    root = SgRoot("true", "bash").root()
    for rid in list(scanner.active):
        scanner.matches(rid, root, True)
    scanner.judge_regexes(root)
    return scanner.invalid


def units_of(command: str) -> tuple[list[Unit], Limit | None]:
    """The command and the shell-string scripts it hands to shells (wrapper variants are not units of their own)."""
    units = [Unit(command, Origin.COMMAND, 0, False)]
    seen = {(command, False)}
    spent = 0
    at = 0
    while at < len(units):
        unit = units[at]
        at += 1
        for text, only in scripts_in(SgRoot(unit.text, "bash").root(), unit.restricted):
            if (text, only) in seen:
                continue
            spent += len(text)
            limit = (Limit.DEPTH if unit.depth + 1 > MAX_DEPTH else Limit.UNITS if len(units) > MAX_UNITS
                     else Limit.SIZE if spent > MAX_SCRIPT_BYTES else None)
            if limit is not None:
                return units, limit
            seen.add((text, only))
            units.append(Unit(text, Origin.SCRIPT, unit.depth + 1, only))
    return units, None


def broken(root: SgNode) -> bool:
    """ERROR nodes, or zero-width nodes the parser invented to recover (MISSING)."""
    stack = [root]
    while stack:
        node = stack.pop()
        where = node.range()
        if node.kind() == "ERROR" or (where.start.index == where.end.index and node is not root
                                       and node.kind() not in ("program", "heredoc_body", "heredoc_content")):
            return True
        stack.extend(node.children())
    return False


def tree_of(unit: Unit) -> UnitTree:
    """[depth, kind, text or None] per named node: text for leaves and for the kinds that hold a name or a literal."""
    root = SgRoot(unit.text, "bash").root()
    rows: list[tuple[int, str, str | None]] = []
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        rows.append((depth, node.kind(), node.text() if node.is_leaf() or node.kind() in LEAF_KINDS else None))
        stack.extend((child, depth + 1) for child in reversed(node.named_children()))
    return UnitTree("shell string" if unit.origin is Origin.SCRIPT else "", unit.text, rows, broken(root))
