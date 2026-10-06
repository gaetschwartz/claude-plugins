"""Text repairs for tree-sitter-bash quirks that hide commands from every rule.

A multi-stage pipeline followed by a newline can swallow the next statement into its last command as arguments
(tree-sitter-bash issue 347), and a heredoc marker followed by a redirect or an operator on its line yields an ERROR
node and hides what comes after. Each quirk is recognised from the tree it produces and fixed in the text, which is
parsed again; the repaired text is kept only when it shows more commands (or fewer ERROR nodes) and no symptom
remains. Pure: no subprocess, bounded parses.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from itertools import pairwise
from typing import NamedTuple

from ast_grep_py import SgNode

type Reparse = Callable[[str], SgNode]

MAX_ROUNDS = 6
CONTINUATION = "\\"


class Budget:
    """The parses repairs may still spend in one walk; a repair that cannot finish within it is dropped."""

    __slots__ = ("left",)

    def __init__(self, parses: int) -> None:
        self.left = parses

    def take(self, parses: int = 1) -> bool:
        if self.left < parses:
            return False
        self.left -= parses
        return True


class Parsed(NamedTuple):
    text: str
    root: SgNode
    repaired: bool


class Shape(StrEnum):
    REDIRECT_THEN_OPERATOR = "redirect-then-operator"
    OPERATOR_AFTER_MARKER = "operator-after-marker"


class Heredoc(NamedTuple):
    shape: Shape
    redirect: SgNode
    swallowed: SgNode | None
    error: SgNode


class Census(NamedTuple):
    commands: int
    errors: int

    @classmethod
    def of(cls, root: SgNode) -> Census:
        return cls(len(root.find_all(kind="command")), len(root.find_all(kind="ERROR")))

    def improves(self, before: Census) -> bool:
        return self.commands > before.commands or self.errors < before.errors


def newline_gaps(root: SgNode, lines: list[str]) -> list[tuple[int, int]]:
    """Where a newline sits between two children of one command, which the grammar swallowed instead of ending the
    statement: the (line, column) just after the last real child before it. A backslash continuation is no gap."""
    found: set[tuple[int, int]] = set()
    for command in root.find_all(kind="command"):
        children = [child for child in command.children() if child.kind() != "comment"]
        for before, after in pairwise(children):
            end, start = before.range().end, after.range().start
            if start.line > end.line and not lines[end.line].rstrip().endswith(CONTINUATION):
                found.add((end.line, end.column))
    return sorted(found)


def heredoc_symptoms(root: SgNode) -> list[Heredoc]:
    """Heredoc redirects whose rest of the line was swallowed: an ERROR inside a redirect after the marker, or directly
    in the heredoc redirect."""
    found: list[Heredoc] = []
    for redirect in root.find_all(kind="heredoc_redirect"):
        children = redirect.children()
        if not any(child.kind() == "heredoc_start" for child in children):
            continue
        for child in children:
            if child.kind() == "file_redirect":
                error = next((c for c in child.children() if c.kind() == "ERROR"), None)
                if error is not None:
                    found.append(Heredoc(Shape.REDIRECT_THEN_OPERATOR, redirect, child, error))
            elif child.kind() == "ERROR":
                found.append(Heredoc(Shape.OPERATOR_AFTER_MARKER, redirect, None, child))
    return found


def insert_separators(lines: list[str], gaps: list[tuple[int, int]]) -> str:
    for line, column in reversed(gaps):
        lines[line] = lines[line][:column] + ";" + lines[line][column:]
    return "\n".join(lines)


def move_heredoc_tail(lines: list[str], symptom: Heredoc) -> str | None:
    """`cmd <<E 2>&1 | x` becomes `cmd 2>&1 <<E | x`; `cmd <<E ; x` becomes `cmd <<E` with `x` after the heredoc.
    None when the symptom spans lines."""
    operator = symptom.redirect.range().start
    error = symptom.error.range()
    if error.start.line != operator.line or error.end.line != operator.line:
        return None
    text = lines[operator.line]
    if symptom.swallowed is not None:
        swallowed = symptom.swallowed.range().start
        if swallowed.line != operator.line:
            return None
        moved = text[swallowed.column:error.start.column].strip()
        lines[operator.line] = (text[:operator.column] + moved + " " + text[operator.column:swallowed.column].rstrip()
                                + " " + text[error.start.column:]).rstrip()
        return "\n".join(lines)
    last = symptom.redirect.range().end.line
    if last <= operator.line:
        return None
    rest = text[error.end.column:].strip()
    lines[operator.line] = text[:error.start.column].rstrip()
    if rest:
        lines.insert(last + 1, rest)
    return "\n".join(lines)


def next_edit(root: SgNode, text: str) -> tuple[bool, str | None]:
    """(a symptom was found, the text with it repaired; None when it cannot be)."""
    lines = text.split("\n")
    if gaps := newline_gaps(root, lines):
        return True, insert_separators(lines, gaps)
    if symptoms := heredoc_symptoms(root):
        return True, move_heredoc_tail(lines, symptoms[0])
    return False, None


def mend(text: str, root: SgNode, budget: Budget, reparse: Reparse) -> Parsed:
    original = Parsed(text, root, False)
    current, tree = text, root
    before: Census | None = None
    for _ in range(MAX_ROUNDS):
        found, edited = next_edit(tree, current)
        if not found:
            break
        if edited is None or not budget.take():
            return original
        before = before or Census.of(root)
        current, tree = edited, reparse(edited)
    else:
        if next_edit(tree, current)[0]:
            return original
    if before is None or not Census.of(tree).improves(before):
        return original
    return Parsed(current, tree, True)


def parse(text: str, budget: Budget, reparse: Reparse, repair: bool = True) -> Parsed:
    """The text and its tree, repaired when it holds a quirk; a text without a newline is parsed once."""
    root = reparse(text)
    if not repair or "\n" not in text:
        return Parsed(text, root, False)
    return mend(text, root, budget, reparse)
