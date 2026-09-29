#!/usr/bin/env python3
"""Expand mdbook `{{#include path[:anchor]}}` directives in a markdown page.

Usage: expand_includes.py --docsite DIR PAGE

Include paths are resolved relative to the page; a path into `docs-router/`
falls back to DIR/packages/docs-router/. An anchor selects the lines between
`ANCHOR: name` and `ANCHOR_END: name`. Anchor marker lines are always dropped.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DIRECTIVE = re.compile(r"\{\{#include\s+([^}\s]+?)\s*\}\}")
MARKER = re.compile(r"\bANCHOR(?:_END)?:\s*\S+")


def resolve(target: str, page: Path, docsite: Path) -> Path | None:
    direct = (page.parent / target).resolve()
    if direct.is_file():
        return direct
    parts = Path(target).parts
    if "docs-router" in parts:
        tail = Path(*parts[parts.index("docs-router"):])
        mapped = docsite / "packages" / tail
        if mapped.is_file():
            return mapped
    return None


def select(lines: list[str], anchor: str | None) -> list[str] | None:
    if anchor is None:
        body = lines
    else:
        start = re.compile(rf"\bANCHOR:\s*{re.escape(anchor)}\s*$")
        end = re.compile(rf"\bANCHOR_END:\s*{re.escape(anchor)}\s*$")
        body = []
        inside = False
        found = False
        for line in lines:
            if not inside and start.search(line):
                inside = found = True
            elif inside and end.search(line):
                break
            elif inside:
                body.append(line)
        if not found:
            return None
    return [ln for ln in body if not MARKER.search(ln)]


def expand(page: Path, docsite: Path) -> str:
    out: list[str] = []
    for line in page.read_text(errors="replace").splitlines():
        m = DIRECTIVE.search(line)
        if not m:
            out.append(line)
            continue
        prefix = line[: m.start()]
        spec = m.group(1)
        target, _, anchor = spec.partition(":")
        src = resolve(target, page, docsite)
        body = None
        if src is not None:
            body = select(src.read_text(errors="replace").splitlines(), anchor or None)
        if body is None:
            out.append(f"{prefix}(include missing: {spec})")
        else:
            out.extend(prefix + ln if ln else prefix.rstrip() for ln in body)
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docsite", required=True)
    ap.add_argument("page")
    args = ap.parse_args()
    sys.stdout.write(expand(Path(args.page), Path(args.docsite)))


if __name__ == "__main__":
    main()
