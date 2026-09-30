#!/usr/bin/env python3
"""guardrails entry point: no arguments → PreToolUse hook reading stdin; `warm` → SessionStart cache warm-up; other arguments → CLI (see --help)."""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        data = sys.stdin.read()
        out = io.StringIO()
        try:
            engine.run_hook(io.StringIO(data), out)
            sys.stdout.write(out.getvalue())
        except Exception as exc:  # noqa: BLE001
            try:
                engine.run_safe(data, sys.stdout, exc)
            except Exception:  # noqa: BLE001
                json.dump({"systemMessage": "guardrails: the hook failed and could not evaluate this command, so it "
                           "was allowed."}, sys.stdout)
        return 0
    if args == ["warm"]:
        try:
            engine.run_warm(sys.stdin)
        except Exception:  # noqa: BLE001, S110
            pass
        return 0
    if len(args) == 2 and args[0] == "warm-install":
        import astrun

        try:
            astrun.ensure(args[1] or None, 120.0)
        except astrun.Unavailable:
            pass
        return 0
    import cli

    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main())
