#!/usr/bin/env python3
"""guardrails entry point: no arguments → PreToolUse hook reading stdin; other arguments → CLI (see --help)."""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine


def hook(data: str) -> None:
    out = io.StringIO()
    try:
        engine.run_hook(io.StringIO(data), out)
        sys.stdout.write(out.getvalue())
        return
    except Exception as exc:  # noqa: BLE001
        failure = exc
    try:
        engine.run_safe(data, sys.stdout, failure)
    except Exception:  # noqa: BLE001
        json.dump({"systemMessage": "guardrails: the hook failed and could not evaluate this command, so it was "
                   "allowed."}, sys.stdout)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        hook(sys.stdin.read())
        return 0
    import cli

    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main())
