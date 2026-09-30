"""Drive the ast-grep binary: scans, rule checks and parse-tree dumps, always with a minimal environment."""

from __future__ import annotations

import bisect
import collections
import json
import os
import re
import subprocess
import time
from typing import Any, NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "engine-sgconfig.yml")
ENV = {"PATH": "/usr/bin:/bin"}
NEUTRAL_CWD = os.sep
PARALLEL = 8
NODE_LINE = re.compile(r"^( *)(?:([a-z_]+): )?(MISSING )?(\S+) \((\d+),(\d+)\)-\((\d+),(\d+)\)$", re.MULTILINE)
CAUSE = re.compile(r"^\s*╰▻ ?(.*)$")
GENERIC_CAUSE = re.compile(r"^(Fail to parse yaml|`rule` is not configured|Rule contains invalid)")


class Unavailable(Exception):
    """The AST engine could not produce an answer; the reason is meant for the user."""


class RuleError(Exception):
    """ast-grep rejected a rule; the text is ast-grep's own explanation."""


class Src:
    """A unit's text with the byte offsets ast-grep reports mapped back to character offsets."""

    def __init__(self, text: str) -> None:
        self.data = text.replace("\x00", " ").encode("utf-8", "replace")
        self.text = self.data.decode()
        self.ascii = len(self.data) == len(self.text)
        self._starts: list[int] | None = None
        self._bytes: list[int] | None = None
        if not self.ascii:
            ends, total = [], 0
            for char in self.text:
                ends.append(total)
                total += len(char.encode("utf-8", "replace"))
            ends.append(total)
            self._bytes = ends

    def char(self, offset: int) -> int:
        if self._bytes is None:
            return offset
        return bisect.bisect_left(self._bytes, offset)

    def line_starts(self) -> list[int]:
        if self._starts is None:
            self._starts = [0] + [i + 1 for i, b in enumerate(self.data) if b == 10]
        return self._starts


class Hit(NamedTuple):
    rule: str
    lo: int
    hi: int


class Node:
    __slots__ = ("children", "field", "hi", "kind", "lo", "missing", "named", "src")

    def __init__(self, kind: str, field: str | None, lo: int, hi: int, src: str, missing: bool = False) -> None:
        self.kind, self.field, self.lo, self.hi, self.src = kind, field, lo, hi, src
        self.missing = missing
        self.named = True
        self.children: list[Node] = []

    def text(self) -> str:
        return self.src[self.lo:self.hi]

    def walk(self) -> list[Node]:
        out: list[Node] = []
        stack: list[Node] = [self]
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(reversed(node.children))
        return out

    def named_children(self) -> list[Node]:
        return [c for c in self.children if c.named]


def blocks(text: str) -> list[list[str]]:
    """The `╰▻` causes of an ast-grep failure, each with its continuation lines."""
    out: list[list[str]] = []
    for line in text.splitlines():
        found = CAUSE.match(line)
        if found:
            out.append([found.group(1).strip()])
        elif out and line.strip() and not line.startswith(("Error", "Help", "See also")):
            out[-1].append(line.strip())
    return out


def summary(text: str) -> str:
    """The informative causes of an ast-grep failure, on one line."""
    causes = [" ".join(b) for b in blocks(text)]
    useful = [c for c in causes if not GENERIC_CAUSE.match(c)] or causes
    if useful:
        return "; ".join(useful)[:200]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " ".join(lines[:2])[:200] if lines else "no output"


def rule_reason(text: str) -> str:
    """The deepest cause of a rejected rule, as ast-grep words it (`Kind ... is invalid.`)."""
    found = blocks(text)
    return found[-1][-1][:200] if found else summary(text)


def parse_dump(src: Src, text: str) -> Node:
    """The tree in `--debug-query` output (named nodes, or every node for cst) as Node objects."""
    head = text.find("Debug ")
    if head < 0:
        raise Unavailable("ast-grep printed no parse tree")
    starts = src.line_starts()
    plain = src.ascii
    char = src.char
    full = src.text
    root: Node | None = None
    stack: list[Node] = []
    expect = text.index("\n", head) + 1
    try:
        for found in NODE_LINE.finditer(text, expect):
            if found.start() != expect:
                break
            expect = found.end() + 1
            indent, field, missing, kind, r0, c0, r1, c1 = found.groups()
            lo, hi = starts[int(r0)] + int(c0), starts[int(r1)] + int(c1)
            if not plain:
                lo, hi = char(lo), char(hi)
            node = Node(kind, field, lo, hi, full, bool(missing))
            del stack[len(indent) >> 1:]
            if stack:
                stack[-1].children.append(node)
            elif root is None:
                root = node
            stack.append(node)
    except IndexError:
        raise Unavailable("ast-grep printed a position outside the command") from None
    if root is None:
        raise Unavailable("ast-grep printed an empty parse tree")
    return root


class Cli:
    """One ast-grep binary and the wall-clock instant by which every call must be finished."""

    def __init__(self, binary: str, deadline: float, version: str = "") -> None:
        self.binary = binary
        self.deadline = deadline
        self.version = version

    def _left(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Unavailable("timed out")
        return left

    def _run(self, args: list[str], payload: bytes | None = None) -> tuple[int, str, str]:
        try:
            proc = subprocess.run([self.binary, *args], input=payload if payload is not None else b"",
                                  capture_output=True, timeout=self._left(), check=False, env=ENV, cwd=NEUTRAL_CWD)
        except subprocess.TimeoutExpired as exc:
            raise Unavailable("timed out") from exc
        except OSError as exc:
            raise Unavailable(f"cannot run ast-grep: {exc.strerror or exc}") from exc
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")

    def _scan_args(self, rules: str) -> list[str]:
        return ["scan", "-c", CONFIG, "--inline-rules", rules, "--json=compact"]

    def _failure(self, code: int, err: str) -> Exception:
        if "Cannot parse rule" in err:
            return RuleError(rule_reason(err))
        return Unavailable(f"ast-grep failed (exit {code}): {summary(err)}")

    def check(self, rules: str) -> None:
        """Raise RuleError when ast-grep does not accept these rules."""
        code, _, err = self._run([*self._scan_args(rules), "--stdin"], b"true\n")
        if code != 0:
            raise self._failure(code, err)

    def scan(self, rules: str, sources: list[Src]) -> list[list[Hit]]:
        """Per source, every match of any rule (by rule id and character range)."""
        if not sources:
            return []
        hits: list[list[Hit]] = [[] for _ in sources]
        if len(sources) == 1:
            code, out, err = self._run([*self._scan_args(rules), "--stdin"], sources[0].data)
            names = {"STDIN": 0}
        else:
            import shutil
            import tempfile

            folder = tempfile.mkdtemp(prefix="guardrails-")
            try:
                paths = []
                for i, src in enumerate(sources):
                    path = os.path.join(folder, f"u{i}.sh")
                    with open(path, "wb") as fh:
                        fh.write(src.data)
                    paths.append(path)
                code, out, err = self._run([*self._scan_args(rules), *paths])
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            names = {os.path.basename(p): i for i, p in enumerate(paths)}
        if code != 0:
            raise self._failure(code, err)
        try:
            found: list[dict[str, Any]] = json.loads(out) if out.strip() else []
            for item in found:
                at = names[os.path.basename(item["file"])]
                span = item["range"]["byteOffset"]
                hits[at].append(Hit(item["ruleId"], sources[at].char(span["start"]), sources[at].char(span["end"])))
        except (ValueError, KeyError, TypeError) as exc:
            raise Unavailable(f"ast-grep printed unreadable output: {type(exc).__name__}") from exc
        return hits

    def _dump_args(self, src: Src, fmt: str) -> list[str]:
        return ["run", "-c", CONFIG, "--lang", "bash", f"--pattern={src.text}", f"--debug-query={fmt}", "--stdin"]

    def dump(self, sources: list[Src], fmt: str = "ast", until: float | None = None) -> list[Node]:
        """The parse tree of each source, with up to PARALLEL ast-grep processes at once.

        Past `until` no further process is started, so the result may be shorter than `sources`.
        """
        trees: list[Node] = []
        running: collections.deque[tuple[Src, subprocess.Popen[bytes]]] = collections.deque()

        def finish() -> None:
            src, proc = running.popleft()
            try:
                _, err = proc.communicate(timeout=self._left())
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                proc.communicate()
                raise Unavailable("timed out") from exc
            trees.append(parse_dump(src, err.decode("utf-8", "replace")))

        try:
            for src in sources:
                if until is not None and time.monotonic() > until:
                    break
                if len(running) >= PARALLEL:
                    finish()
                self._left()
                try:
                    proc = subprocess.Popen([self.binary, *self._dump_args(src, fmt)], stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=ENV,
                                            cwd=NEUTRAL_CWD)
                except OSError as exc:
                    raise Unavailable(f"cannot run ast-grep: {exc.strerror or exc}") from exc
                running.append((src, proc))
            while running:
                finish()
        finally:
            for _, proc in running:
                proc.kill()
                proc.communicate()
        return trees
