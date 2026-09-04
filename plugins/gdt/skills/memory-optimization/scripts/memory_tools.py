#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic helpers for a Claude Code memory-store optimization run.

Subcommands: backup, survey, repair, rebuild-index, validate.

Everything here is the part of the job that must not be left to a model's
judgement: the backup that makes the run reversible, the index that must match
disk exactly, and the link graph that no single batch agent can see.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NotRequired, Self, TypedDict

INDEX_RE = re.compile(r"^- \[[^\]]+\]\(([A-Za-z0-9_.\-]+\.md)\) — .+")
LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
NAME_LINE_RE = re.compile(r"(?m)^name:\s*.+$")
INDEX_NAME = "MEMORY.md"


class Section(StrEnum):
    """The `## ` headings of MEMORY.md, in the order they are written."""

    ENVIRONMENT = "Environment"
    SERVICES = "Services"
    USER_PREFERENCES = "User Preferences"
    CLAUDE_CODE_AUTHORING = "Claude Code authoring"


class MemoryType(StrEnum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


class FinalFileTD(TypedDict):
    """One surviving file as reported by a workflow agent."""

    file: str
    indexLine: str
    section: NotRequired[str]
    absorbed: NotRequired[list[str]]
    justification: NotRequired[str]


class ReportTD(TypedDict):
    """One batch's report."""

    batch: NotRequired[str]
    finalFiles: list[FinalFileTD]


@dataclass(frozen=True, slots=True)
class Frontmatter:
    name: str | None
    description: str | None
    type: str | None

    @classmethod
    def parse(cls, text: str) -> Self | None:
        match = FRONTMATTER_RE.match(text)
        if match is None:
            return None
        block = match.group(1)

        def field_value(key: str) -> str | None:
            found = re.search(rf"(?m)^\s*{key}:\s*(.+)$", block)
            return found.group(1).strip() if found else None

        return cls(field_value("name"), field_value("description"), field_value("type"))


@dataclass(frozen=True, slots=True)
class MemoryFile:
    path: Path
    text: str
    frontmatter: Frontmatter | None

    @classmethod
    def load(cls, path: Path) -> Self:
        text = path.read_text()
        return cls(path, text, Frontmatter.parse(text))

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem_slug(self) -> str:
        return slug(self.path.stem)

    @property
    def line_count(self) -> int:
        return self.text.count("\n")

    def links(self) -> set[str]:
        return set(LINK_RE.findall(self.text))


@dataclass(slots=True)
class RepairCounts:
    """Mutable on purpose — it tallies edits as files stream past."""

    names: int = 0
    repointed: int = 0
    normalized: int = 0
    dead: list[tuple[str, str]] = field(default_factory=list)


def slug(value: str) -> str:
    """Canonical link/name form: kebab-case."""
    return value.strip().replace("_", "-")


def memory_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.md") if p.name != INDEX_NAME)


def load_all(directory: Path) -> list[MemoryFile]:
    return [MemoryFile.load(p) for p in memory_files(directory)]


# --------------------------------------------------------------------- backup


def cmd_backup(directory: Path, dest: Path | None) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = (
        dest.expanduser().resolve()
        if dest
        else Path(tempfile.mkdtemp(prefix="memory-optimization-"))
    )
    root = base / f"memory-backup-{stamp}"
    root.mkdir(parents=True, exist_ok=True)

    tarball = root / "memory.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(directory, arcname="memory")

    pristine = root / "pristine"
    shutil.copytree(directory, pristine)

    print(f"tarball:  {tarball}")
    print(f"pristine: {pristine}")
    print(f"files:    {len(memory_files(directory))}")
    print(
        "\nPass the pristine path to the workflow as args.pristine — loss-check diffs"
    )
    print("against it, so it is load-bearing, not just insurance.")


# --------------------------------------------------------------------- survey


def cmd_survey(directory: Path) -> None:
    files = load_all(directory)
    for memo in sorted(files, key=lambda m: m.line_count):
        description = ""
        if memo.frontmatter and memo.frontmatter.description:
            description = memo.frontmatter.description.strip('"')
        print(f"{memo.line_count:5d}  {memo.name:<52.52} {description[:88]}")
    print(f"\n{len(files)} files, {sum(m.line_count for m in files)} lines")
    print("\nGroup these into batches of 6-12 by subject domain. Anything that might")
    print("merge must share a batch — agents cannot touch files outside their own.")


# ------------------------------------------------------------------ link work


def load_reports(path: Path) -> tuple[list[ReportTD], Mapping[str, str]]:
    """Accept the raw Workflow tool-result file or a bare reports array."""
    data: object = json.loads(path.read_text())
    for _ in range(4):
        if isinstance(data, str):
            data = json.loads(data)
        elif isinstance(data, dict) and "reports" not in data and "result" in data:
            data = data["result"]
        else:
            break
    if isinstance(data, list):
        return data, {}
    if isinstance(data, dict):
        reports = data.get("reports", [])
        merge_map = data.get("mergeMap") or {}
        if isinstance(reports, list) and isinstance(merge_map, dict):
            return reports, merge_map
    raise ValueError(f"{path}: could not find a reports array in this JSON")


def strip_suffix(value: str) -> str:
    return value.removesuffix(".md")


def build_merge_map(
    reports: Sequence[ReportTD], seed: Mapping[str, str]
) -> dict[str, str]:
    """absorbed-slug -> surviving-slug, with a -> b -> c collapsed to a -> c."""
    mapping = {slug(strip_suffix(k)): slug(strip_suffix(v)) for k, v in seed.items()}
    for report in reports:
        for entry in report.get("finalFiles", []):
            target = slug(strip_suffix(entry["file"]))
            for source in entry.get("absorbed") or []:
                cleaned = strip_suffix(source)
                if " " in cleaned or ".md" in cleaned:
                    continue  # a prose note, not a filename
                mapping[slug(cleaned)] = target
    for key in list(mapping):
        hops = 0
        while mapping.get(mapping[key]) and mapping[key] != key and hops < 10:
            mapping[key] = mapping[mapping[key]]
            hops += 1
    return mapping


def repair_text(
    memo: MemoryFile,
    live: frozenset[str],
    redirect: Mapping[str, str],
    keep: frozenset[str],
    counts: RepairCounts,
) -> str:
    text = NAME_LINE_RE.sub(f"name: {memo.stem_slug}", memo.text, count=1)
    if text != memo.text:
        counts.names += 1

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        if raw in keep:
            return match.group(0)
        canonical = slug(raw)
        target = redirect.get(canonical)
        if target is not None and target in live:
            counts.repointed += 1
            return f"[[{target}]]"
        if canonical in live:
            if canonical != raw:
                counts.normalized += 1
            return f"[[{canonical}]]"
        counts.dead.append((memo.name, raw))
        return match.group(0)

    return LINK_RE.sub(replace, text)


def cmd_repair(directory: Path, reports_path: Path | None, keep: Iterable[str]) -> None:
    files = load_all(directory)
    live = frozenset(m.stem_slug for m in files)
    reports, seed = load_reports(reports_path) if reports_path else ([], {})
    redirect = build_merge_map(reports, seed)
    counts = RepairCounts()

    for memo in files:
        updated = repair_text(memo, live, redirect, frozenset(keep), counts)
        if updated != memo.text:
            memo.path.write_text(updated)

    print(f"name slugs normalized: {counts.names}")
    print(f"links repointed to merge targets: {counts.repointed}")
    print(f"links normalized to kebab-case: {counts.normalized}")
    if not counts.dead:
        print("\nno dangling links")
        return
    print(f"\n{len(counts.dead)} link(s) point at nothing — decide each one by hand:")
    print("(a target that never existed is rot; drop the link. Otherwise repoint it.)")
    for filename, link in sorted(set(counts.dead)):
        print(f"  {filename}: [[{link}]]")


# --------------------------------------------------------------------- index


def cmd_rebuild_index(directory: Path, reports_path: Path, force: bool) -> None:
    reports, _ = load_reports(reports_path)

    buckets: dict[Section, list[str]] = {s: [] for s in Section}
    reported: set[str] = set()
    malformed: list[tuple[str, str, str]] = []

    for report in reports:
        for entry in report.get("finalFiles", []):
            line = entry["indexLine"].strip()
            match = INDEX_RE.match(line)
            if match is None:
                malformed.append((entry["file"], "malformed", line))
            elif match.group(1) != entry["file"]:
                malformed.append((entry["file"], "filename mismatch", line))
            try:
                section = Section(entry.get("section", ""))
            except ValueError:
                section = Section.SERVICES
            buckets[section].append(line)
            reported.add(entry["file"])

    on_disk = {p.name for p in memory_files(directory)}
    missing = sorted(on_disk - reported)
    extra = sorted(reported - on_disk)

    for filename, reason, line in malformed:
        print(f"BAD INDEX LINE ({reason}): {filename}\n  {line}", file=sys.stderr)
    if missing:
        print(f"ON DISK BUT UNREPORTED: {missing}", file=sys.stderr)
    if extra:
        print(f"REPORTED BUT NOT ON DISK: {extra}", file=sys.stderr)
    if (missing or extra or malformed) and not force:
        raise SystemExit(
            "\nrefusing to write MEMORY.md while reports and disk disagree "
            "(--force to write anyway)"
        )

    lines = ["# System Memory", ""]
    for section in Section:
        if buckets[section]:
            lines += [f"## {section}", *buckets[section], ""]
    (directory / INDEX_NAME).write_text("\n".join(lines).rstrip() + "\n")

    filled = sum(1 for s in Section if buckets[s])
    print(f"{INDEX_NAME} rebuilt: {len(reported)} entries across {filled} sections")


# ------------------------------------------------------------------ validate


def validate_file(
    memo: MemoryFile, live: frozenset[str], keep: frozenset[str]
) -> list[str]:
    front = memo.frontmatter
    if front is None:
        return [f"NO FRONTMATTER: {memo.name}"]

    problems: list[str] = []
    if not front.description:
        problems.append(f"NO DESCRIPTION: {memo.name}")
    if front.type not in set(MemoryType):
        problems.append(f"BAD TYPE ({front.type}): {memo.name}")
    if not front.name or slug(front.name) != memo.stem_slug:
        problems.append(f"NAME/FILENAME MISMATCH: {memo.name} has name: {front.name}")
    problems += [
        f"DANGLING LINK: {memo.name} -> [[{link}]]"
        for link in sorted(memo.links())
        if slug(link) not in live and link not in keep
    ]
    return problems


def cmd_validate(directory: Path, keep: Iterable[str]) -> None:
    files = load_all(directory)
    live = frozenset(m.stem_slug for m in files)
    keep_set = frozenset(keep)

    problems: list[str] = []
    for memo in files:
        problems += validate_file(memo, live, keep_set)

    index = directory / INDEX_NAME
    if index.exists():
        listed = {
            m.group(1)
            for line in index.read_text().splitlines()
            if (m := INDEX_RE.match(line))
        }
        on_disk = {m.name for m in files}
        problems += [f"NOT IN {INDEX_NAME}: {n}" for n in sorted(on_disk - listed)]
        problems += [
            f"IN {INDEX_NAME} BUT MISSING: {n}" for n in sorted(listed - on_disk)
        ]
    else:
        problems.append(f"{INDEX_NAME} does not exist")

    for problem in problems:
        print(problem)
    total_lines = sum(m.line_count for m in files)
    print(f"\n{len(files)} files, {total_lines} lines, {len(problems)} problem(s)")
    if problems:
        raise SystemExit(1)


# ----------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    backup = sub.add_parser("backup", help="tarball + pristine copy (do this first)")
    backup.add_argument("dir", type=Path)
    backup.add_argument(
        "--dest",
        type=Path,
        help="where both land; defaults to a fresh temp dir. "
        "Pass the session scratchpad when you have one.",
    )

    survey = sub.add_parser(
        "survey", help="list files by size with descriptions, to build batches"
    )
    survey.add_argument("dir", type=Path)

    repair = sub.add_parser(
        "repair", help="normalize name slugs and repoint merged links"
    )
    repair.add_argument("dir", type=Path)
    repair.add_argument(
        "--reports", type=Path, help="workflow result JSON (supplies the merge map)"
    )
    repair.add_argument(
        "--keep",
        nargs="*",
        default=[],
        help="literal [[strings]] that are not links (e.g. config syntax)",
    )

    index = sub.add_parser(
        "rebuild-index", help="regenerate MEMORY.md from workflow reports"
    )
    index.add_argument("dir", type=Path)
    index.add_argument("--reports", type=Path, required=True)
    index.add_argument(
        "--force", action="store_true", help="write even if reports disagree with disk"
    )

    validate = sub.add_parser(
        "validate", help="frontmatter, slugs, links, index coverage"
    )
    validate.add_argument("dir", type=Path)
    validate.add_argument(
        "--keep", nargs="*", default=[], help="literal [[strings]] that are not links"
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()
    directory: Path = args.dir.expanduser().resolve()
    if not directory.is_dir():
        raise SystemExit(f"not a directory: {directory}")

    match args.cmd:
        case "backup":
            cmd_backup(directory, args.dest)
        case "survey":
            cmd_survey(directory)
        case "repair":
            cmd_repair(directory, args.reports, args.keep)
        case "rebuild-index":
            cmd_rebuild_index(directory, args.reports, args.force)
        case "validate":
            cmd_validate(directory, args.keep)
        case unknown:
            raise SystemExit(f"unknown command: {unknown}")


if __name__ == "__main__":
    main()
