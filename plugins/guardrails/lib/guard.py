#!/usr/bin/env python3
"""guardrails entry point: no arguments → PreToolUse hook reading stdin; `warm` → SessionStart cache warm-up; other arguments → CLI (see --help)."""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine

HOOK_BUDGET = 7.0
VENV_FAILED = "--venv-failed"


def hook(data: str, notes: tuple[str, ...]) -> None:
    import watchdog

    out = io.StringIO()
    failure: BaseException = RuntimeError("unknown")
    fast = False
    watchdog.start(HOOK_BUDGET)
    try:
        engine.run_hook(io.StringIO(data), out, notes)
        sys.stdout.write(out.getvalue())
        return
    except watchdog.Expired:
        failure, fast = TimeoutError("hook time budget exceeded"), True
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        watchdog.stop()
    try:
        engine.run_safe(data, sys.stdout, failure, fast)
    except Exception:  # noqa: BLE001
        json.dump({"systemMessage": "guardrails: the hook failed and could not evaluate this command, so it was "
                   "allowed."}, sys.stdout)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args == [VENV_FAILED]:
        notes = ("guardrails: the venv python failed, so this call ran under the system python.",) if args else ()
        hook(sys.stdin.read(), notes)
        return 0
    if args == ["warm"]:
        try:
            engine.run_warm(sys.stdin)
        except Exception:  # noqa: BLE001, S110
            pass
        return 0
    if len(args) == 2 and args[0] == "warm-install":
        import astrun

        astrun.BUILD_ALLOWED = True
        try:
            astrun.maintain(args[1] or None)
            astrun.ensure(args[1] or None, 120.0)
        except astrun.Unavailable:
            pass
        return 0
    import cli

    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main())
