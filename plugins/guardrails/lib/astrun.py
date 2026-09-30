"""Run the AST matcher: find the pinned ast-grep binary and evaluate requests with it, never from repo-controlled input."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import astbin
import astworker
import store
import watchdog
from astbin import Missing
from astcli import Cli, Unavailable

DEADLINE = 4.0
WATCHDOG_MARGIN = 0.4
HERE = os.path.dirname(os.path.abspath(__file__))
RUN_ENV = ("HOME", "LANG", "TMPDIR")
PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy")

__all__ = ["DEADLINE", "Missing", "Unavailable", "call", "take_rejected", "warm"]


def take_rejected() -> list[str]:
    return astbin.take_rejected()


def shape_ok(op: object, response: object) -> bool:
    if not isinstance(response, dict) or response.get("ok") is not True:
        return False
    if op == "eval":
        return isinstance(response.get("verdicts"), dict) and isinstance(response.get("errors"), dict)
    if op == "check":
        return isinstance(response.get("errors"), dict)
    if op == "tree":
        return isinstance(response.get("units"), list)
    return True


def call(request: dict[str, Any], state_dir: str | None = None) -> dict[str, Any]:
    """The worker's response to request; raise Unavailable with a reason when it cannot answer (Missing without a binary)."""
    engine = astbin.locate(state_dir or os.path.dirname(store.global_state_path()))
    cli = Cli(engine.binary, time.monotonic() + DEADLINE - WATCHDOG_MARGIN, engine.version)
    try:
        with watchdog.limit(DEADLINE):
            response = astworker.handle(request, cli)
    except Unavailable:
        raise
    except TimeoutError as exc:
        raise Unavailable("timed out", "timeout") from exc
    except Exception as exc:
        raise Unavailable(f"the AST worker failed: {type(exc).__name__}: {exc}", "unexpected") from exc
    if not shape_ok(request.get("op"), response):
        detail = response.get("error") if isinstance(response, dict) else None
        raise Unavailable(f"the AST worker failed: {detail or 'unexpected reply'}", "unexpected")
    return response


def warm(state_dir: str | None = None) -> None:
    """Install the wheel binary in a detached process when nothing usable exists; never raises, never waits."""
    try:
        state_dir = state_dir or os.path.dirname(store.global_state_path())
        if not astbin.wanted(state_dir):
            return
        env = {k: os.environ[k] for k in (*RUN_ENV, *PROXIES) if k in os.environ}
        env["PATH"] = "/usr/bin:/bin"
        subprocess.Popen([sys.executable, "-I", "-S", os.path.join(HERE, "guard.py"), "warm-install", state_dir],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env, cwd=os.sep)
    except Exception:  # noqa: BLE001, S110
        pass
