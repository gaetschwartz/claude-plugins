#!/usr/bin/env python3
"""guardrails entry point: no arguments → PreToolUse hook reading stdin; other arguments → CLI (see --help)."""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bootstrap
import engine

UNSAFE_DATA_DIR = 111


def hook(data: str) -> None:
    out = io.StringIO()
    try:
        recorder = engine.run_hook(io.StringIO(data), out)
        sys.stdout.write(out.getvalue())
        sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001
        failure = exc
    else:
        if recorder is not None:
            recorder.finish()
        return
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
    if bootstrap.data_problem(bootstrap.data_dir()) is not None:
        sys.exit(UNSAFE_DATA_DIR)
    code = main()
    if len(sys.argv) == 1:  # the hook has nothing left to release; skip interpreter teardown
        sys.stdout.flush()
        os._exit(code)
    sys.exit(code)
