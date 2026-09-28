#!/usr/bin/env python3
"""guardrails entry point: no arguments → PreToolUse hook reading stdin; arguments → CLI (see --help)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli
import engine


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        try:
            engine.run_hook(sys.stdin, sys.stdout)
        except Exception:  # noqa: BLE001, S110
            pass
        return 0
    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main())
