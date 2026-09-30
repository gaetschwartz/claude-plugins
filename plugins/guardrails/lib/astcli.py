"""Drive the ast-grep binary: scans, rule checks and parse-tree dumps, always with a minimal environment."""

from __future__ import annotations

import bisect
import collections
import contextlib
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


CANARY = '{"id":"canary","language":"bash","rule":{"any":[{"kind":"program"},{"kind":"ERROR"}]}}\n'
YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff]")
NO_IGNORE = ("--no-ignore", "hidden", "--no-ignore", "dot", "--no-ignore", "exclude", "--no-ignore", "global",
             "--no-ignore", "parent", "--no-ignore", "vcs")


def document(rule_id: str, body: dict[str, Any]) -> str:
    """One ast-grep rule document; JSON is valid YAML, and the characters YAML treats as line breaks stay escaped."""
    text = json.dumps({"id": rule_id, "language": "bash", **body}, ensure_ascii=False)
    return YAML_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def private_dir() -> str:
    """A fresh directory only this user can enter."""
    base = os.environ.get("TMPDIR")
    if not base or not os.path.isdir(base):
        base = "/tmp"
    for _ in range(100):
        path = os.path.join(base, "guardrails-" + os.urandom(8).hex())
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise Unavailable(f"cannot create a temporary directory in {base}: {exc.strerror or exc}") from exc
        return path
    raise Unavailable("cannot create a private temporary directory")


def write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def remove_tree(folder: str) -> None:
    for root, dirs, files in os.walk(folder, topdown=False):
        for name in files:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(root, name))
        for name in dirs:
            with contextlib.suppress(OSError):
                os.rmdir(os.path.join(root, name))
    with contextlib.suppress(OSError):
        os.rmdir(folder)


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

    def _failure(self, code: int, err: str) -> Exception:
        if "Cannot parse rule" in err:
            return RuleError(rule_reason(err))
        return Unavailable(f"ast-grep failed (exit {code}): {summary(err)}")

    def check(self, rules: dict[str, Any]) -> None:
        """Raise RuleError when ast-grep does not accept these rules."""
        self.scan(rules, [Src("true")])

    def scan(self, rules: dict[str, Any], sources: list[Src]) -> list[list[Hit]]:
        """Per source, every match of any rule (by rule id and character range).

        Rules and sources travel as files in a private temporary directory, so no size of either can overflow the
        argument list. The reply must be exactly ast-grep's JSON list and must contain its canary match for every
        non-empty source, or the engine counts as malfunctioning.
        """
        if not sources:
            return []
        folder = private_dir()
        try:
            os.mkdir(os.path.join(folder, "rules"), 0o700)
            os.mkdir(os.path.join(folder, "units"), 0o700)
            write_private(os.path.join(folder, "sgconfig.yml"), b"ruleDirs:\n  - rules\n")
            write_private(os.path.join(folder, "rules", "canary.yml"), CANARY.encode())
            text = "\n---\n".join(document(rid, body) for rid, body in rules.items()) + "\n"
            write_private(os.path.join(folder, "rules", "rules.yml"), text.encode())
            for i, src in enumerate(sources):
                write_private(os.path.join(folder, "units", f"u{i}.sh"), src.data)
            code, out, err = self._run(["scan", "-c", os.path.join(folder, "sgconfig.yml"), "--format", "sarif",
                                        *NO_IGNORE, os.path.join(folder, "units")])
        finally:
            remove_tree(folder)
        if code != 0:
            raise self._failure(code, err)
        return self._hits(out, sources, {"canary", *rules})

    def _hits(self, out: str, sources: list[Src], allowed: set[str]) -> list[list[Hit]]:
        try:
            found = json.loads(out)
            if not isinstance(found, dict) or len(found["runs"]) != 1 or not isinstance(found["runs"][0], dict):
                raise TypeError("unexpected document")
            hits: list[list[Hit]] = [[] for _ in sources]
            canary = [False] * len(sources)
            for item in found["runs"][0]["results"]:
                place = item["locations"][0]["physicalLocation"]
                named = re.fullmatch(r"u(\d+)\.sh", os.path.basename(place["artifactLocation"]["uri"]))
                if named is None:
                    raise ValueError("unknown file")
                at = int(named.group(1))
                rule = item["ruleId"]
                lo = place["region"]["byteOffset"]
                hi = lo + place["region"]["byteLength"]
                if rule not in allowed or not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi
                                               <= len(sources[at].data)):
                    raise ValueError("unexpected entry")
                if rule == "canary":
                    canary[at] = True
                else:
                    hits[at].append(Hit(rule, sources[at].char(lo), sources[at].char(hi)))
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise Unavailable(f"ast-grep printed unreadable output: {type(exc).__name__}") from exc
        if any(src.data and not seen for src, seen in zip(sources, canary)):
            raise Unavailable("ast-grep did not report its built-in check match, so it is not working")
        return hits

    def _dump_args(self, src: Src, fmt: str) -> list[str]:
        return ["run", "-c", CONFIG, "--lang", "bash", f"--pattern={src.text}", f"--debug-query={fmt}", "--stdin"]

    def dump(self, sources: list[Src], fmt: str = "ast") -> list[Node]:
        """The parse tree of each source, with up to PARALLEL ast-grep processes at once.

        ast-grep prints a tree only for `--pattern` text, so the text of one unit (at most MAX_UNIT_BYTES) is an argument.
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
