#!/usr/bin/env python3
"""Render `semble search --format json` as `path:start-end` headers plus snippets,
dropping stale results and keeping the first --limit."""
import argparse
import json
import os
import sys


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    print("hint: rerun the query; if it persists, check `uvx --from 'semble[mcp]' semble --version`", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--limit", type=int, required=True)
    ap.add_argument("--stale-prefix", required=True)
    ap.add_argument("roots", nargs="+")
    args = ap.parse_args()

    try:
        payload = json.load(sys.stdin)
        entries = payload.get("results", [])
        repos = payload.get("repos")
    except (ValueError, AttributeError):
        return fail("semble returned unexpected output")

    results = []
    for r in entries:
        rel = r.get("file_path")
        content = r.get("content")
        if not rel or content is None:
            continue
        root = args.roots[0]
        if repos:
            label, _, rel = rel.partition("/")
            root = repos.get(label)
            if root is None:
                continue
        if any(part.startswith(args.stale_prefix) for part in rel.split("/")):
            continue
        results.append((os.path.join(root, rel), r.get("start_line", 1), r.get("end_line", 1), content))
    results = results[: args.limit]
    if not results:
        print(f"no matches for: {args.query}", file=sys.stderr)
        return 0

    blocks = []
    for path, start, end, content in results:
        shown = os.path.relpath(path, args.data)
        if shown.startswith(".."):
            shown = path
        blocks.append(f"{shown}:{start}-{end}\n{content.rstrip()}\n")
    print("\n".join(blocks), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
