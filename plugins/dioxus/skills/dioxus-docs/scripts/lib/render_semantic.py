#!/usr/bin/env python3
"""Render `semble search --format json` as `path:start-end` headers plus snippets."""
import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("roots", nargs="+")
    args = ap.parse_args()

    payload = json.load(sys.stdin)
    results = payload.get("results", [])
    if not results:
        print(f"no matches for: {args.query}", file=sys.stderr)
        return 0

    repos = payload.get("repos")
    blocks = []
    for r in results:
        rel = r["file_path"]
        if repos:
            label, _, rest = rel.partition("/")
            path = os.path.join(repos[label], rest)
        else:
            path = os.path.join(args.roots[0], rel)
        shown = os.path.relpath(path, args.data)
        if shown.startswith(".."):
            shown = path
        header = f"{shown}:{r['start_line']}-{r['end_line']}"
        blocks.append(f"{header}\n{r['content'].rstrip()}\n")
    print("\n".join(blocks), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
