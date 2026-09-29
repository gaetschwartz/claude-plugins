#!/usr/bin/env python3
"""Render `semble search --format json` as `path:start-end` headers plus snippets,
dropping stale untested_* results and keeping the first --limit."""
import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--limit", type=int, required=True)
    ap.add_argument("roots", nargs="+")
    args = ap.parse_args()

    payload = json.load(sys.stdin)
    repos = payload.get("repos")
    results = []
    for r in payload.get("results", []):
        rel = r["file_path"]
        root = args.roots[0]
        if repos:
            label, _, rel = rel.partition("/")
            root = repos[label]
        if any(part.startswith("untested_") for part in rel.split("/")):
            continue
        r["_path"] = os.path.join(root, rel)
        results.append(r)
    results = results[: args.limit]
    if not results:
        print(f"no matches for: {args.query}", file=sys.stderr)
        return 0

    blocks = []
    for r in results:
        path = r["_path"]
        shown = os.path.relpath(path, args.data)
        if shown.startswith(".."):
            shown = path
        header = f"{shown}:{r['start_line']}-{r['end_line']}"
        blocks.append(f"{header}\n{r['content'].rstrip()}\n")
    print("\n".join(blocks), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
