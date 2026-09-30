#!/usr/bin/env python3
"""guardrails entry point: no arguments → PreToolUse hook reading stdin; `warm` → SessionStart engine warm-up; other arguments → CLI (see --help)."""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine

HOOK_BUDGET = 7.0


def hook(data: str) -> None:
    import watchdog

    out = io.StringIO()
    failure: BaseException = RuntimeError("unknown")
    watchdog.start(HOOK_BUDGET)
    try:
        engine.run_hook(io.StringIO(data), out)
        sys.stdout.write(out.getvalue())
        return
    except watchdog.Expired:
        failure = TimeoutError("hook time budget exceeded")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        watchdog.stop()
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
    if args == ["warm"]:
        try:
            engine.run_warm(sys.stdin)
        except Exception:  # noqa: BLE001, S110
            pass
        return 0
    if len(args) == 2 and args[0] == "warm-install":
        import astbin

        try:
            astbin.install(args[1], respect_backoff=True)
        except (astbin.InstallError, astbin.Unavailable, OSError):
            pass
        return 0
    import cli

    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main())
