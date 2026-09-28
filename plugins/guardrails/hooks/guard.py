#!/usr/bin/env python3
"""guardrails entry point: no arguments → PreToolUse hook reading stdin; arguments → CLI (see --help)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        try:
            engine.run_hook(sys.stdin, sys.stdout)
        except Exception:
            pass
        return 0
    print("usage: guard.py (no arguments: PreToolUse hook)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
