"""Evaluate rules against a command with ast-grep-py, in process.

The command is parsed as written. The commands behind each argv wrapper (sudo, env, xargs, ...) and the scripts handed
to a shell or joined by a runner (`bash -c '...'`, `eval`, `ssh host '...'`, heredocs) are text variants and units of
their own, parsed and matched the same way; a hit inside one of them counts as wrapped. A variant is one top-level
statement with its wrappers replaced, never the whole text, so its cost does not grow with the size of the script around
it.
"""

from __future__ import annotations

import itertools
import math
import re
import sys
import time
from collections import defaultdict, deque
from collections.abc import Callable, Hashable, Iterator, Sequence
from enum import StrEnum
from typing import NamedTuple

import rulebuilder
from ast_grep_py import Config, SgNode, SgRoot
from verdict import MAX_COMMAND_BYTES, Detail, Kind, Limit, UnitTree

type Describe = Callable[[str, SgNode, Kind, tuple[str, ...]], Detail | None]

MAX_DEPTH = 8
MAX_UNITS = 128
MAX_VARIANTS = 2048
MAX_VARIANT_BYTES = 4 * MAX_COMMAND_BYTES
MAX_COMBINATIONS = 64
CONTEXT_KINDS = frozenset({"pipeline", "command_substitution", "process_substitution"})
ARGUMENT_KINDS = frozenset({"raw_string", "string", "word", "number", "concatenation", "simple_expansion", "expansion",
                            "command_substitution", "arithmetic_expansion", "process_substitution"})
LEAF_KINDS = frozenset({"word", "command_name", "raw_string", "number"})
ESCAPED_IN_DOUBLE_QUOTES = re.compile(r'\\([\\"$`])')
WORD_PIECE = re.compile(r"""'([^']*)'|"((?:\\.|[^"\\])*)"|\\(.)|([^'"\\]+)""", re.DOTALL)
SELF_TEST_COMMAND = "echo a | cat"


class Script(NamedTuple):
    """A script a unit hands to a shell; `restricted` scripts only count inside their own substitutions. `runner` is
    the command that runs it (None for a heredoc body that is no command's script); a script with a `group` is one of
    several starts of that runner command's words."""

    text: str
    restricted: bool
    runner: str | None
    group: int | None = None

    @property
    def candidate(self) -> bool:
        return self.group is not None


class Origin(StrEnum):
    COMMAND = "command"
    VARIANT = "variant"
    SCRIPT = "script"


class Unit(NamedTuple):
    text: str
    origin: Origin
    depth: int
    restricted: bool
    via: tuple[str, ...] = ()
    group: tuple[int, int] | None = None

    @property
    def candidate(self) -> bool:
        return self.group is not None

    @property
    def through_wrapper(self) -> bool:
        return through_wrapper(self.via)


def through_wrapper(via: tuple[str, ...]) -> bool:
    return any(name in rulebuilder.WRAPPERS for name in via)


class Hit(NamedTuple):
    rule: str
    kind: Kind
    start: int
    end: int


class Scan(NamedTuple):
    hits: tuple[Hit, ...]
    invalid: dict[str, str]
    limit: Limit | None
    micros: dict[str, int]
    parse_us: int
    details: dict[str, Detail]
    unparsed: bool

    def kinds(self, rule_ids: Sequence[str]) -> dict[str, Kind | None]:
        found = {hit.rule: hit.kind for hit in self.hits}
        return {rid: found.get(rid) for rid in rule_ids}


class TooManyVariants(Exception):
    """A unit holds more wrapped commands, or one wrapper has more words, than the variant cap allows."""


class Span(NamedTuple):
    start: int
    end: int
    words: tuple[int, ...]
    name: str
    crossed: tuple[tuple[str, ...], ...]
    statement: tuple[int, int] = (0, 0)


class Variant(NamedTuple):
    text: str
    via: tuple[str, ...]


def unquote(word: str) -> str:
    """The text one shell word stands for: its single-quoted, double-quoted, backslashed and bare pieces joined."""
    # not shlex: it keeps `\$` inside double quotes, so `bash -c "\$(x)"` would read as harmless text (fail-open)

    def piece(found: re.Match[str]) -> str:
        single, double, escaped, bare = found.groups()
        if double is not None:
            return ESCAPED_IN_DOUBLE_QUOTES.sub(r"\1", double)
        return next(text for text in (single, escaped, bare) if text is not None)

    return WORD_PIECE.sub(piece, word)


def case_rank(detail: Detail | None) -> int:
    """Position of the message case a hit satisfies; the rule's own message comes after every case."""
    return detail.case if detail is not None and detail.case is not None else sys.maxsize


def clean_error(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "invalid rule"


def self_test() -> str | None:
    """Why the library cannot parse and match a trivial pipeline, or None when it can."""
    try:
        found = SgRoot(SELF_TEST_COMMAND, "bash").root().find(kind="pipeline")
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__} while parsing a test command"
    return None if found is not None and found.text() == SELF_TEST_COMMAND else "it misparsed a test command"


def commands_named(root: SgNode, names: Sequence[str]) -> list[SgNode]:
    return root.find_all({"rule": rulebuilder.command_named(names)})


def is_command_flag(word: str) -> bool:
    """`-c`, `-lc`, `-ec`: a short-option cluster that contains c."""
    return word.startswith("-") and word[1:].isalpha() and word.isascii() and "c" in word[1:]


def command_word(command: SgNode) -> str:
    """The name the command is run as, unquoted and without its directory."""
    name = command.field("name")
    return unquote(name.text()).rsplit("/", 1)[-1] if name is not None else ""


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
        if not is_command_flag(word.text()):
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


def joined_scripts(command: SgNode, runner: str, index: int) -> Iterator[Script]:
    """What a runner that joins its words with spaces hands to a shell: `eval` all of them, `ssh` and `watch` the words
    from each non-option word on (no option table says which one starts the remote command)."""
    words = [unquote(arg.text()) for arg in arguments(command) if arg.kind() in ARGUMENT_KINDS]
    starts = [0] if runner == "eval" else [at for at, word in enumerate(words) if not word.startswith("-")]
    if len(starts) > MAX_VARIANTS:
        raise TooManyVariants
    return (Script(" ".join(words[at:]), False, runner, None if runner == "eval" else index) for at in starts)


def scripts_in(root: SgNode, restricted: bool) -> Iterator[Script]:
    """Each script a unit hands to a shell: `bash -c` strings, the words of `eval`, `ssh` and `watch` (joined like the
    shell does), heredocs and here-strings fed to a shell, and the bodies of unquoted heredocs that contain a
    substitution, which only count inside their substitutions. A restricted unit only yields scripts inside its own."""
    found: list[Iterator[Script]] = []
    for command in commands_named(root, rulebuilder.SHELLS):
        if not restricted or inside_substitution(command):
            runner = command_word(command)
            found.append(iter([Script(script, False, runner) for script in shell_scripts(command)]))
    for index, command in enumerate(commands_named(root, rulebuilder.JOINED_RUNNERS)):
        if not restricted or inside_substitution(command):
            found.append(joined_scripts(command, command_word(command), index))
    for redirect in root.find_all(kind="heredoc_redirect"):
        start = next((n for n in redirect.named_children() if n.kind() == "heredoc_start"), None)
        if start is None or not (start.text().isascii() and start.text().replace("_", "").isalnum()):
            continue
        if restricted and not inside_substitution(redirect):
            continue
        for body in (n for n in redirect.named_children() if n.kind() == "heredoc_body"):
            text = body.text()
            if "`" in text or "$(" in text:
                found.append(iter([Script(text, True, None)]))
    return (script for scripts in found for script in scripts if script.text.strip())


def statement_node(node: SgNode) -> SgNode:
    """The top-level statement (a direct child of the program) that holds the node."""
    while (parent := node.parent()) is not None and parent.kind() != "program":
        node = parent
    return node


def statement_range(node: SgNode) -> tuple[int, int]:
    """The range of the top-level statement that holds the node."""
    where = statement_node(node).range()
    return where.start.index, where.end.index


def wrapper_spans(root: SgNode, wrappers: Sequence[str]) -> list[Span]:
    """Each command named by a wrapper: its range, where each of its non-option words starts (an option cannot start
    the wrapped command) and the top-level statement around it."""
    commands = commands_named(root, wrappers)
    if len(commands) > MAX_VARIANTS:
        raise TooManyVariants
    spans = []
    for command in commands:
        nodes = arguments(command)
        if len(nodes) > MAX_VARIANTS:
            raise TooManyVariants
        words = [word for word in nodes if not word.text().startswith("-")]
        if words:
            where = command.range()
            names = [unquote(word.text()).rsplit("/", 1)[-1] for word in words]
            crossed = tuple(tuple(name for name in names[:at] if name in wrappers) for at in range(len(words)))
            spans.append(Span(where.start.index, where.end.index, tuple(w.range().start.index for w in words),
                              command_word(command), crossed, statement_range(command)))
    return spans


def variants_of(text: str, spans: Sequence[Span]) -> Iterator[Variant]:
    """The text of each top-level statement that holds a wrapper, with the wrappers replaced (see statement_variants).
    Lazy, so the caller's caps stop the work before the copies are made."""
    by_statement: dict[tuple[int, int], list[Span]] = {}
    for span in spans:
        by_statement.setdefault(span.statement, []).append(span)
    for (first, last), group in by_statement.items():
        yield from statement_variants(text[first:last], [Span(s.start - first, s.end - first,
                                                             tuple(w - first for w in s.words), s.name, s.crossed)
                                      for s in group])


def statement_variants(text: str, spans: Sequence[Span]) -> Iterator[Variant]:
    """The statement text with one wrapper replaced by the text from each of its words on, and with several outermost
    wrappers replaced together: every combination while there are few, else each k-th word of all of them at once."""

    def replaced(chosen: Sequence[tuple[Span, int]]) -> Variant:
        out = text
        for span, word in sorted(chosen, key=lambda pair: pair[0].start, reverse=True):
            out = out[:span.start] + text[word:span.end] + out[span.end:]
        via = [name for span, word in sorted(chosen, key=lambda pair: pair[0].start)
               for name in (span.name, *span.crossed[span.words.index(word)])]
        return Variant(out, tuple(via))

    for span in spans:
        for word in span.words:
            yield replaced([(span, word)])
    outermost: list[Span] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if not outermost or span.start >= outermost[-1].end:
            outermost.append(span)
    if len(outermost) > 1:
        if math.prod(len(span.words) + 1 for span in outermost) <= MAX_COMBINATIONS:
            options = [[(span, word) for word in (*span.words, None)] for span in outermost]
            for combination in itertools.product(*options):
                if (variant := replaced([(span, word) for span, word in combination if word is not None])).text != text:
                    yield variant
        else:
            for k in range(max(len(span.words) for span in outermost)):
                yield replaced([(span, span.words[k]) for span in outermost if len(span.words) > k])


class Scanner:
    """One evaluation: the rules and what has been found so far. `describe` reads what a rule's message needs from the
    node of each hit that becomes the rule's verdict."""

    def __init__(self, rules: dict[str, Config], direct_only: frozenset[str] = frozenset(),
                 describe: Describe | None = None, watched: frozenset[str] = frozenset()) -> None:
        self.active = dict(rules)
        self.direct_only = direct_only
        self.describe = describe
        self.watched = watched.union(*(rulebuilder.via_names(config["rule"]) for config in rules.values()))
        self.resolved: dict[tuple[str, frozenset[str]], Config] = {}
        self.unparsed = False
        self.clean_groups: set[tuple[int, int]] = set()
        self.broken_groups: set[tuple[int, int]] = set()
        self.invalid: dict[str, str] = {}
        self.found: dict[str, Hit] = {}
        self.details: dict[str, Detail] = {}
        self.spent: defaultdict[str, int] = defaultdict(int)

    def drop(self, rid: str, exc: Exception) -> None:
        del self.active[rid]
        self.invalid[rid] = clean_error(str(exc))

    def config_for(self, rid: str, via: tuple[str, ...]) -> Config:
        """The rule's config for a unit reached through this chain: its `via` placeholders settled."""
        key = (rid, rulebuilder.facts(self.watched, via))
        if key not in self.resolved:
            self.resolved[key] = {**self.active[rid], "rule": rulebuilder.resolve_via(self.active[rid]["rule"], via)}
        return self.resolved[key]

    def matches(self, rid: str, root: SgNode, first_only: bool, via: tuple[str, ...] = ()) -> list[SgNode]:
        """The nodes a rule selects; a rule whose config does not compile is dropped with the reason."""
        try:
            config = self.config_for(rid, via)
            if first_only:
                one = root.find(config)
                return [one] if one is not None else []
            return root.find_all(config)
        except Exception as exc:  # noqa: BLE001
            self.drop(rid, exc)
            return []

    def consider(self, rid: str, node: SgNode, kind: Kind, via: tuple[str, ...], keep_first: bool) -> bool:
        """Offer this node as the rule's verdict: it replaces a wrapped one only when its message case comes earlier.
        False when the case cannot be evaluated (the rule is dropped)."""
        detail = None
        if self.describe is not None:
            began = time.perf_counter_ns()
            try:
                detail = self.describe(rid, node, kind, via)
            except Exception as exc:  # noqa: BLE001
                self.drop(rid, exc)
                self.found.pop(rid, None)
                self.details.pop(rid, None)
                return False
            finally:
                self.spent[rid] += time.perf_counter_ns() - began
        if keep_first and case_rank(detail) >= case_rank(self.details.get(rid)):
            return True
        where = node.range()
        self.found[rid] = Hit(rid, kind, where.start.index, where.end.index)
        if detail is not None:
            self.details[rid] = detail
        else:
            self.details.pop(rid, None)
        return True

    def kind_of(self, unit: Unit, node: SgNode) -> Kind | None:
        """None when the hit does not count: outside the substitutions of a restricted unit."""
        kinds = {ancestor.kind() for ancestor in node.ancestors()}
        if unit.restricted and "command_substitution" not in kinds:
            return None
        if unit.origin is not Origin.COMMAND or kinds & CONTEXT_KINDS:
            return Kind.WRAPPED
        return Kind.DIRECT

    def judge(self, unit: Unit, root: SgNode) -> None:
        if not (unit.restricted or unit.origin is Origin.VARIANT):
            if unit.group is not None:
                (self.broken_groups if broken(root) else self.clean_groups).add(unit.group)
            elif not self.unparsed:
                self.unparsed = broken(root)
        for rid in list(self.active):
            known = self.found.get(rid)
            if (known is not None and known.kind is Kind.DIRECT) or (unit.through_wrapper and rid in self.direct_only):
                continue
            first_only = unit.origin is not Origin.COMMAND and not unit.restricted
            began = time.perf_counter_ns()
            nodes = self.matches(rid, root, first_only, unit.via)
            self.spent[rid] += time.perf_counter_ns() - began
            for node in nodes:
                kind = Kind.WRAPPED if first_only else self.kind_of(unit, node)
                if kind is None:
                    continue
                later = known is not None and kind is Kind.WRAPPED
                if later and self.describe is None:
                    continue
                if not self.consider(rid, node, kind, unit.via, later):
                    break
                known = self.found[rid]
                if kind is Kind.DIRECT:
                    break

    def run(self, command: str) -> Scan:
        """Scan the command, then its variants and scripts level by level. Hits found before a cap is reached stand."""

        began = time.perf_counter_ns()

        def distinct(via: tuple[str, ...]) -> Hashable:
            return rulebuilder.facts(self.watched, via), bool(self.direct_only) and through_wrapper(via)

        limit = walk(command, True, self.judge, distinct)
        parse_ns = time.perf_counter_ns() - began - sum(self.spent.values())
        return Scan(tuple(self.found.values()), dict(self.invalid), limit,
                    {rid: ns // 1000 for rid, ns in self.spent.items()}, max(parse_ns, 0) // 1000, dict(self.details),
                    self.unparsed or bool(self.broken_groups - self.clean_groups))


def walk(command: str, with_variants: bool, visit: Callable[[Unit, SgNode], None],
         distinct: Callable[[tuple[str, ...]], Hashable] = lambda via: False) -> Limit | None:
    """Visit the command, then its shell-string scripts (and wrapper variants) breadth first; the bound that stopped the
    walk, if any. Only a new text is charged to the bounds. The same text reached through chains that `distinct` tells
    apart is visited again, uncharged, for the rules that look at the chain."""
    queue = deque([Unit(command, Origin.COMMAND, 0, False)])
    charged = {(command, False, False)}
    visited = {(command, False, False, distinct(()))}
    scripts = variants = script_bytes = variant_bytes = serial = 0
    while queue:
        unit = queue.popleft()
        serial += 1
        root = SgRoot(unit.text, "bash").root()
        visit(unit, root)
        found: Iterator[Unit] = iter(())
        try:
            if with_variants and unit.origin is not Origin.VARIANT:
                spans = wrapper_spans(root, rulebuilder.WRAPPERS)
                found = (Unit(variant.text, Origin.VARIANT, unit.depth, unit.restricted, (*unit.via, *variant.via))
                         for variant in variants_of(unit.text, spans) if variant.text != unit.text)
            found = itertools.chain(found, (
                Unit(script.text, Origin.SCRIPT, unit.depth + 1, script.restricted,
                     unit.via if script.runner is None else (*unit.via, script.runner),
                     None if script.group is None else (serial, script.group))
                for script in scripts_in(root, unit.restricted)))
            for new in found:
                text_key = (new.text, new.restricted, new.origin is Origin.VARIANT)
                key = (*text_key, distinct(new.via))
                if key in visited:
                    continue
                if text_key not in charged:
                    if new.origin is Origin.VARIANT or new.candidate:
                        variants += 1
                        variant_bytes += len(new.text)
                        limit = (Limit.DEPTH if new.depth > MAX_DEPTH else Limit.VARIANTS if variants > MAX_VARIANTS
                                 else Limit.VARIANT_BYTES if variant_bytes > MAX_VARIANT_BYTES else None)
                    else:
                        scripts += 1
                        script_bytes += len(new.text)
                        limit = (Limit.DEPTH if new.depth > MAX_DEPTH else Limit.UNITS if scripts > MAX_UNITS
                                 else Limit.SIZE if script_bytes > MAX_COMMAND_BYTES else None)
                    if limit is not None:
                        return limit
                    charged.add(text_key)
                visited.add(key)
                queue.append(new)
        except TooManyVariants:
            return Limit.VARIANTS
    return None


def compile_errors(configs: dict[str, Config]) -> dict[str, str]:
    """The reason per rule id whose config does not compile."""
    scanner = Scanner(configs)
    root = SgRoot("true", "bash").root()
    for rid in list(scanner.active):
        scanner.matches(rid, root, True)
    return scanner.invalid


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


def tree_of(unit: Unit, root: SgNode) -> UnitTree:
    """[depth, kind, text or None] per named node: text for leaves and for the kinds that hold a name or a literal."""
    rows: list[tuple[int, str, str | None]] = []
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        rows.append((depth, node.kind(), node.text() if node.is_leaf() or node.kind() in LEAF_KINDS else None))
        stack.extend((child, depth + 1) for child in reversed(node.named_children()))
    return UnitTree("shell string" if unit.origin is Origin.SCRIPT else "", unit.text, rows, broken(root))
