"""Launch the AST worker: one `uv run` per call (or in-process when allowed), bounded by a deadline."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from typing import Any

PIN = "0.45.3"
DEADLINE = 4.0
RETRY_AFTER = 600.0
UV_ENV = "GUARDRAILS_UV"
INPROCESS_ENV = "GUARDRAILS_AST_INPROCESS"
STAMP = "ast-download-failed"
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "astworker.py")


class Unavailable(Exception):
    """The AST engine could not produce an answer; the reason is meant for the user."""


def uv_path() -> str | None:
    override = os.environ.get(UV_ENV)
    if override is not None:
        return override if override and shutil.which(override) else None
    return shutil.which("uv")


def uv_command(uv: str, offline: bool, *tail: str) -> list[str]:
    return [uv, "run", "--quiet", "--no-project", *(["--offline"] if offline else []),
            "--with", f"ast-grep-py=={PIN}", "python", *tail]


def _summary(text: str) -> str:
    lines = [ln.strip(" ×╰─▶") for ln in text.splitlines() if ln.strip()]
    return " ".join(lines[:2])[:160] if lines else "no output"


def _launch(cmd: list[str], payload: str, timeout: float) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise Unavailable(f"timed out after {timeout:.1f}s") from exc
    except OSError as exc:
        raise Unavailable(f"cannot run {cmd[0]}: {exc.strerror or exc}") from exc
    try:
        response = json.loads(proc.stdout)
    except ValueError:
        raise Unavailable(f"uv failed: {_summary(proc.stderr)}") from None
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = response.get("error") if isinstance(response, dict) else None
        raise Unavailable(f"ast-grep worker failed: {detail or 'bad response'}")
    return response


def _recent(stamp: str | None) -> bool:
    if not stamp:
        return False
    try:
        return time.time() - os.stat(stamp).st_mtime < RETRY_AFTER
    except OSError:
        return False


def call(request: dict[str, Any], state_dir: str | None = None) -> dict[str, Any]:
    """The worker's response to request; raise Unavailable with a reason when it cannot answer."""
    if os.environ.get(INPROCESS_ENV) == "1":
        try:
            import astworker

            return astworker.handle(json.loads(json.dumps(request)))
        except ImportError as exc:
            raise Unavailable(f"ast_grep_py is not importable: {exc}") from exc
    uv = uv_path()
    if uv is None:
        raise Unavailable("uv is not installed or not on PATH")
    payload = json.dumps(request)
    started = time.monotonic()
    try:
        return _launch(uv_command(uv, True, WORKER), payload, DEADLINE)
    except Unavailable as offline_failure:
        stamp = os.path.join(state_dir, STAMP) if state_dir else None
        left = DEADLINE - (time.monotonic() - started)
        if _recent(stamp) or left < 0.5:
            raise Unavailable(f"ast-grep-py=={PIN} is not in the uv cache and the last download attempt failed "
                              f"recently ({offline_failure})") from None
        try:
            response = _launch(uv_command(uv, False, WORKER), payload, left)
        except Unavailable as online_failure:
            if stamp:
                with contextlib.suppress(OSError):
                    os.makedirs(state_dir or "", exist_ok=True)
                    with open(stamp, "w"):
                        pass
            raise Unavailable(str(online_failure)) from None
        if stamp:
            with contextlib.suppress(OSError):
                os.unlink(stamp)
        return response


def warm() -> None:
    """Fill the uv cache in a detached process; never raises."""
    uv = uv_path()
    if uv is None or os.environ.get(INPROCESS_ENV) == "1":
        return
    with contextlib.suppress(OSError):
        subprocess.Popen(uv_command(uv, False, "-c", "import ast_grep_py"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
