"""Text repairs for tree-sitter-bash quirks that hide commands from every rule.

A multi-stage pipeline followed by a newline can swallow the next statement into its last command as arguments
(tree-sitter-bash issue 347). The quirk is recognised from the tree it produces and fixed in the text, which is parsed
again; the repaired text is kept only when it shows more commands and no symptom remains. Pure: no subprocess, bounded
parses.
"""

from __future__ import annotations

from collections.abc import Callable
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


def insert_separators(lines: list[str], gaps: list[tuple[int, int]]) -> str:
    for line, column in reversed(gaps):
        lines[line] = lines[line][:column] + ";" + lines[line][column:]
    return "\n".join(lines)


def count_commands(root: SgNode) -> int:
    return len(root.find_all(kind="command"))


def mend(text: str, root: SgNode, budget: Budget, reparse: Reparse) -> Parsed:
    original = Parsed(text, root, False)
    current, tree = text, root
    before: int | None = None
    for _ in range(MAX_ROUNDS):
        lines = current.split("\n")
        if not (gaps := newline_gaps(tree, lines)):
            break
        if not budget.take():
            return original
        before = count_commands(root) if before is None else before
        current = insert_separators(lines, gaps)
        tree = reparse(current)
    else:
        if newline_gaps(tree, current.split("\n")):
            return original
    if before is None or count_commands(tree) <= before:
        return original
    return Parsed(current, tree, True)


def parse(text: str, budget: Budget, reparse: Reparse, repair: bool = True) -> Parsed:
    """The text and its tree, repaired when it holds the quirk; a text without a newline is parsed once."""
    root = reparse(text)
    if not repair or "\n" not in text:
        return Parsed(text, root, False)
    return mend(text, root, budget, reparse)
