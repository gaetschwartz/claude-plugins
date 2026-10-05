"""Find where guardrails rules denied a command in Claude Code session transcripts, with the messages around it."""

from __future__ import annotations

import json
import multiprocessing
import os
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, TypedDict

SHELL_TOOLS = ("Bash", "Monitor")
MARKER = "[guardrails:"
HOOK_ERROR = " hook error: "
COMMAND_CAP = 2000
TEXT_CAP = 600
MESSAGE_CAP = 2000
RING_SLACK = 256
USE_LIMIT = 4096
MAX_WORKERS = 8
READ_BUFFER = 1 << 20


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def valid_id(text: str) -> bool:
    return bool(text) and text.isascii() and text[0].isalnum() and all(c.isalnum() or c in "_.-" for c in text)


def leading_ids(text: str) -> list[str] | None:
    """The rule ids of a `[guardrails:a, b (managed)]` marker at the very start of text."""
    if not text.startswith(MARKER):
        return None
    close = text.find("]", len(MARKER))
    if close < 0:
        return None
    ids = [part.removesuffix(" (managed)") for part in text[len(MARKER):close].split(", ")]
    return ids if all(valid_id(i) for i in ids) else None


def denial_ids(text: str, tool: str) -> list[str] | None:
    """The rule ids in the hook's denial text for this tool, or None when the text is not such a denial.

    Claude Code shows the hook's reason either bare or after `PreToolUse:<tool> hook error: `; later paragraphs
    each start with their own marker.
    """
    body = text
    if text.startswith("PreToolUse:"):
        prefix = f"PreToolUse:{tool}{HOOK_ERROR}"
        if not text.startswith(prefix):
            return None
        body = text[len(prefix):]
    ids = leading_ids(body)
    if ids is None:
        return None
    for paragraph in body.split("\n\n")[1:]:
        ids += [i for i in leading_ids(paragraph) or [] if i not in ids]
    return ids


class Block(TypedDict):
    type: str
    text: str
    name: str | None
    tool_use_id: str | None
    is_error: bool
    truncated: bool


class Message(TypedDict):
    role: str
    timestamp: str
    line: int
    blocks: list[Block]


class Hit(TypedDict):
    rule: str
    rules: list[str]
    tool: str
    tool_use_id: str
    timestamp: str
    local: str
    session_id: str
    file: str
    line: int
    is_sidechain: bool
    agent_id: str | None
    cwd: str | None
    command: str
    command_truncated: bool
    message: str
    message_truncated: bool
    before: list[Message]
    after: list[Message]
    also_in: list[str]


class Found(NamedTuple):
    epoch: float
    hit: Hit


def cap(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


def text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def block(kind: str, text: str = "", limit: int = TEXT_CAP, name: str | None = None, use_id: str | None = None,
          error: bool = False) -> Block:
    shown, truncated = cap(text, limit)
    return {"type": kind, "text": shown, "name": name, "tool_use_id": use_id, "is_error": error, "truncated": truncated}


def blocks_of(content: object) -> list[Block]:
    if isinstance(content, str):
        return [block("text", content)]
    out: list[Block] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            out.append(block("text", str(item.get("text", ""))))
        elif kind == "tool_use":
            args = item.get("input")
            name = str(item.get("name", ""))
            command = args.get("command") if isinstance(args, dict) else None
            text = command if isinstance(command, str) else json.dumps(args, ensure_ascii=False, separators=(",", ":"))
            out.append(block("tool_use", text, COMMAND_CAP if name in SHELL_TOOLS else TEXT_CAP, name,
                             str(item.get("id", ""))))
        elif kind == "tool_result":
            out.append(block("tool_result", text_of(item.get("content")), use_id=str(item.get("tool_use_id", "")),
                             error=bool(item.get("is_error"))))
        elif kind != "thinking":
            out.append(block(str(kind)))
    return out


def instant(timestamp: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(timestamp)).astimezone(UTC)
    except ValueError:
        return None


def local_time(timestamp: str) -> str:
    when = instant(timestamp)
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S") if when else ""


@dataclass(frozen=True)
class Query:
    rule: str | None = None
    known: frozenset[str] | None = None
    warn: frozenset[str] = frozenset()
    before: int = 4
    after: int = 4


@dataclass
class FileScan:
    hits: list[Found] = field(default_factory=list)
    corrupt: int = 0
    dropped: int = 0
    unreadable: bool = False


def kept_ids(ids: list[str], query: Query) -> tuple[list[str], bool]:
    """The ids that denied (warn rules ride along in the text) and whether the hit passes the query."""
    deniers = [i for i in ids if i not in query.warn]
    if query.rule is not None:
        return deniers, query.rule in deniers
    return deniers, query.known is None or any(i in query.known for i in deniers)


def denied_results(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """The (tool_use_id, text) of every error tool_result in a user entry that could be a hook denial."""
    inner = entry.get("message")
    if entry.get("type") != "user" or not isinstance(inner, dict) or not isinstance(inner.get("content"), list):
        return []
    found = []
    for item in inner["content"]:
        if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("is_error") is True:
            text, use_id = text_of(item.get("content")), item.get("tool_use_id")
            if isinstance(use_id, str) and use_id and text.startswith(("PreToolUse:", MARKER)):
                found.append((use_id, text))
    return found


@dataclass
class Use:
    tool: str
    command: str
    seq: int


@dataclass
class Pending:
    found: Found
    want: int


class Scanner:
    """One streaming pass over a transcript: every line is parsed, and only a bounded window of messages is kept."""

    def __init__(self, rel: str, query: Query, scan: FileScan) -> None:
        self.rel = rel
        self.query = query
        self.scan = scan
        self.ring: deque[tuple[int, Message]] = deque(maxlen=query.before + RING_SLACK)
        self.uses: dict[str, Use] = {}
        self.pending: list[Pending] = []
        self.seen: set[str] = set()
        self.seq = 0

    def line(self, number: int, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            entry = json.loads(raw)
        except ValueError:
            self.scan.corrupt += 1
            return
        inner = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(inner, dict) or entry.get("type") not in ("user", "assistant"):
            return
        message: Message = {"role": entry["type"], "timestamp": str(entry.get("timestamp", "")), "line": number,
                            "blocks": blocks_of(inner.get("content"))}
        for pending in self.pending if message["blocks"] else []:
            pending.found.hit["after"].append(message)
            pending.want -= 1
        self.pending = [p for p in self.pending if p.want > 0]
        for use_id, text in denied_results(entry):
            self.denial(entry, number, use_id, text)
        self.remember(entry, inner, message)

    def remember(self, entry: dict[str, Any], inner: dict[str, Any], message: Message) -> None:
        content = inner.get("content")
        for item in content if entry["type"] == "assistant" and isinstance(content, list) else []:
            if (isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") in SHELL_TOOLS
                    and isinstance(item.get("id"), str)):
                args = item.get("input")
                command = args.get("command") if isinstance(args, dict) else None
                self.uses[item["id"]] = Use(item["name"], command if isinstance(command, str) else "", self.seq)
                if len(self.uses) > USE_LIMIT:
                    del self.uses[next(iter(self.uses))]
        if message["blocks"]:
            self.ring.append((self.seq, message))
        self.seq += 1

    def denial(self, entry: dict[str, Any], number: int, use_id: str, text: str) -> None:
        used = self.uses.get(use_id)
        ids = denial_ids(text, used.tool) if used else None
        if used is None or ids is None or use_id in self.seen:
            return
        self.seen.add(use_id)
        query = self.query
        deniers, keep = kept_ids(ids, query)
        if not keep:
            self.scan.dropped += query.rule is None
            return
        shown, command_cut = cap(used.command, COMMAND_CAP)
        message, message_cut = cap(text, MESSAGE_CAP)
        stamp = str(entry.get("timestamp", ""))
        when = instant(stamp)
        primary = query.rule or next((i for i in deniers if query.known is None or i in query.known), deniers[0])
        earlier = [m for seq, m in self.ring if seq < used.seq]
        hit: Hit = {
            "rule": primary, "rules": deniers, "tool": used.tool, "tool_use_id": use_id, "timestamp": stamp,
            "local": local_time(stamp), "session_id": str(entry.get("sessionId") or entry.get("session_id") or ""),
            "file": self.rel, "line": number, "is_sidechain": bool(entry.get("isSidechain")),
            "agent_id": entry.get("agentId"), "cwd": entry.get("cwd"), "command": shown,
            "command_truncated": command_cut, "message": message, "message_truncated": message_cut,
            "before": earlier[-query.before:] if query.before else [], "after": [], "also_in": [],
        }
        found = Found(when.timestamp() if when else -1.0, hit)
        self.scan.hits.append(found)
        if query.after:
            self.pending.append(Pending(found, query.after))


def scan_file(path: str, rel: str, query: Query) -> FileScan:
    scan = FileScan()
    scanner = Scanner(rel, query, scan)
    try:
        with open(path, "rb", buffering=READ_BUFFER) as handle:
            for number, raw in enumerate(handle, 1):
                scanner.line(number, raw)
    except OSError:
        scan.unreadable = True
    return scan


@dataclass
class Report:
    hits: list[Hit] = field(default_factory=list)
    dropped: int = 0
    corrupt: int = 0
    unreadable: int = 0
    files_total: int = 0
    opened: list[str] = field(default_factory=list)
    stopped_early: bool = False
    projects: str = ""


def transcripts(root: Path) -> list[tuple[float, str, str]]:
    """(mtime, absolute path, path relative to root) of every transcript, newest first."""
    found = []
    for folder, _, names in os.walk(root):
        for name in names:
            if name.endswith(".jsonl"):
                full = os.path.join(folder, name)
                try:
                    found.append((os.stat(full).st_mtime, full, os.path.relpath(full, root)))
                except OSError:
                    continue
    found.sort(key=lambda f: (-f[0], f[2]))
    return found


def worker_count(requested: int | None) -> int:
    return max(1, min(requested or os.cpu_count() or 1, MAX_WORKERS))


def find_denials(root: Path, limit: int, query: Query, workers: int | None = None) -> Report:
    """The newest `limit` denials under root, newest first, one per tool_use_id; stops once no older file can matter.

    A file's mtime is never older than the events inside it, so once `limit` hits are held and the next file is older
    than the limit-th newest, no remaining file holds anything newer or any copy of a held hit.
    """
    files = transcripts(root)
    report = Report(files_total=len(files), projects=str(root))
    count = worker_count(workers)
    window = 2 * count if count > 1 else 1
    held: dict[str, Found] = {}
    pool = ProcessPoolExecutor(count, mp_context=multiprocessing.get_context("fork")) if count > 1 and len(files) > 1 else None
    try:
        for first in range(0, len(files), window):
            batch = files[first:first + window]
            newest = [found.epoch for found in held.values()]
            if len(newest) >= limit > 0 and batch[0][0] < sorted(newest, reverse=True)[limit - 1]:
                report.stopped_early = True
                break
            args = [(full, rel, query) for _, full, rel in batch]
            scans = pool.map(scan_file, *zip(*args)) if pool else (scan_file(*a) for a in args)
            for (_, _, rel), scan in zip(batch, scans):
                report.opened.append(rel)
                report.corrupt += scan.corrupt
                report.dropped += scan.dropped
                report.unreadable += scan.unreadable
                for found in scan.hits:
                    if found.hit["tool_use_id"] in held:
                        held[found.hit["tool_use_id"]].hit["also_in"].append(rel)
                    else:
                        held[found.hit["tool_use_id"]] = found
    finally:
        if pool:
            pool.shutdown()
    order = {rel: n for n, (_, _, rel) in enumerate(files)}
    ranked = sorted(held.values(), key=lambda f: (-f.epoch, order[f.hit["file"]], -f.hit["line"]))
    report.hits = [found.hit for found in ranked[:limit]]
    return report
