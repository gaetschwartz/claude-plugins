"""State file locations and safe read/modify/write of guardrails state."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from typing import Any, Callable, TypeVar

PLUGIN = "guardrails"
MARKETPLACE = "gaetans-claude-plugins"
MANAGED_ENV = "GUARDRAILS_MANAGED_PATH"
MANAGED_DEFAULTS = {
    "darwin": "/Library/Application Support/ClaudeCode/guardrails.json",
    "win32": r"C:\Program Files\ClaudeCode\guardrails.json",
}
MANAGED_LINUX = "/etc/claude-code/guardrails.json"
MANAGED_MODE = 0o644
SESSION_TTL_DAYS = 7
MAX_SESSIONS = 50
HERE = os.path.dirname(os.path.abspath(__file__))

State = dict[str, Any]
T = TypeVar("T")


class StateError(Exception):
    """A state file exists but cannot be used; it must never be silently overwritten."""


class NotWritable(StateError):
    """A state file (or the directory it would be created in) is not writable by this user."""


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def plugin_id(here: str = HERE) -> str:
    """Mirror Claude Code's CLAUDE_PLUGIN_DATA naming: <plugin>-<marketplace>, sanitised."""
    parts = here.split(os.sep)
    if "cache" in parts:
        i = len(parts) - 1 - parts[::-1].index("cache")
        if i >= 1 and parts[i - 1] == "plugins" and len(parts) > i + 3:
            return re.sub(r"[^A-Za-z0-9_-]", "-", f"{parts[i + 2]}-{parts[i + 1]}")
    return f"{PLUGIN}-{MARKETPLACE}"


def global_state_path() -> str:
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        return os.path.join(data, "state.json")
    return os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data", plugin_id(), "state.json")


def managed_path() -> str:
    return os.environ.get(MANAGED_ENV) or MANAGED_DEFAULTS.get(sys.platform, MANAGED_LINUX)


def load_managed() -> tuple[State, str | None]:
    """The managed layer and, when the file is unusable, why it was skipped (never raises)."""
    try:
        return load(managed_path()), None
    except StateError as exc:
        return {}, str(exc)


def ensure_writable(path: str) -> None:
    probe = os.path.dirname(os.path.abspath(path))
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    if not os.access(probe, os.W_OK | os.X_OK):
        raise NotWritable(f"cannot write {path}: directory {probe} is not writable")
    if os.path.exists(path) and not os.access(path, os.W_OK):
        raise NotWritable(f"cannot write {path}: file is not writable")


def project_root(cwd: str | None = None) -> str | None:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return os.path.realpath(env)
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or os.getcwd(), capture_output=True,
                              text=True, timeout=3, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return os.path.realpath(out.splitlines()[0]) if proc.returncode == 0 and out else None


def project_state_path(cwd: str | None = None) -> str | None:
    root = project_root(cwd)
    return os.path.join(root, ".claude", "plugins", "data", plugin_id(), "state.json") if root else None


def load(path: str | None) -> State:
    """Missing file → {}; unreadable or non-object JSON → StateError."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (OSError, ValueError) as exc:
        raise StateError(f"{path}: {exc}") from exc
    if not isinstance(state, dict):
        raise StateError(f"{path}: top level is not a JSON object")
    return state


def prune(sessions: object) -> dict[str, Any]:
    if not isinstance(sessions, dict):
        return {}
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=SESSION_TTL_DAYS)
    kept: dict[str, Any] = {}
    for sid, record in sessions.items():
        if not isinstance(record, dict):
            continue
        try:
            seen = datetime.datetime.fromisoformat(str(record.get("seenAt", "")))
        except ValueError:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=datetime.timezone.utc)
        if seen >= cutoff:
            kept[str(sid)] = record
    if len(kept) > MAX_SESSIONS:
        newest = sorted(kept.items(), key=lambda kv: str(kv[1].get("seenAt", "")), reverse=True)
        kept = dict(newest[:MAX_SESSIONS])
    return kept


def write(path: str, state: State, mode: int | None = None) -> None:
    """Atomic write; with mode, the file gets those permissions and a missing directory is created 0755."""
    state["updatedAt"] = now()
    if "sessions" in state:
        state["sessions"] = prune(state["sessions"])
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o755 if mode is not None else 0o777, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(path: str, dir_mode: int = 0o777) -> Iterator[None]:
    os.makedirs(os.path.dirname(path), mode=dir_mode, exist_ok=True)
    with open(path + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def mutate(path: str, fn: Callable[[State], T], mode: int | None = None) -> T:
    """Load, apply fn, write back, all under the file lock; nothing is written if fn raises."""
    with locked(path, 0o777 if mode is None else 0o755):
        state = load(path)
        result = fn(state)
        write(path, state, mode)
    return result
