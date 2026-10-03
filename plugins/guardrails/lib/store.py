"""State file locations and safe read/modify/write of guardrails state."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import sys
from collections.abc import Callable, Iterator
from typing import Any

import bootstrap
import policy

MANAGED_ENV = "GUARDRAILS_MANAGED_PATH"
MANAGED_DARWIN = "/Library/Application Support/ClaudeCode/guardrails.json"
MANAGED_LINUX = "/etc/claude-code/guardrails.json"
MANAGED_MODE = 0o644
MANAGED_DIR_MODE = 0o755
SESSION_TTL_DAYS = 7
MAX_SESSIONS = 50
HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(os.path.dirname(HERE), "bin", "guardrails")

State = dict[str, Any]


class StateError(Exception):
    """A state file exists but cannot be used; it must never be silently overwritten."""


class NotWritable(StateError):
    """A state file (or the directory it would be created in) is not writable by this user."""


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def global_state_path() -> str:
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        return os.path.join(data, "state.json")
    return os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data", bootstrap.PLUGIN_ID, "state.json")


def default_managed_path() -> str:
    return MANAGED_DARWIN if sys.platform == "darwin" else MANAGED_LINUX


def managed_paths(extra: str | None = None) -> list[str]:
    """Managed sources, highest ranked first: the platform default, the env override, then an explicit extra file."""
    paths = [default_managed_path()]
    for candidate in (os.environ.get(MANAGED_ENV), extra):
        if candidate and all(os.path.abspath(candidate) != os.path.abspath(p) for p in paths):
            paths.append(candidate)
    return paths


def managed_write_path(extra: str | None = None) -> str:
    return extra or os.environ.get(MANAGED_ENV) or default_managed_path()


def hook_enforces(path: str) -> bool:
    """Whether the hook loads this managed file (the platform default or the env override)."""
    known = [default_managed_path(), os.environ.get(MANAGED_ENV) or ""]
    return any(k and os.path.abspath(path) == os.path.abspath(k) for k in known)


def load_managed(extra: str | None = None) -> tuple[State, list[str]]:
    """The combined managed layer and what is wrong with it; an unusable source is skipped, never fatal."""
    sources: list[tuple[str, State]] = []
    problems: list[str] = []
    for path in managed_paths(extra):
        try:
            sources.append((path, load(path)))
        except StateError as exc:
            problems.append("unreadable managed state, so its rules are NOT enforced until it is fixed (fix or "
                            f"remove the file by hand; the CLI never overwrites a corrupt state file): {exc}")
    layer, more = policy.managed_layer(sources)
    return layer, problems + more


def presence(path: str) -> str:
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return " (absent)"
    except OSError:
        return " (unreadable)"
    return ""


def trust_problems(path: str) -> list[str]:
    """POSIX only: the managed file and its directory should be root-owned and not writable by others."""
    if os.name != "posix":
        return []
    problems = []
    for what, target in (("file", path), ("directory", os.path.dirname(os.path.abspath(path)))):
        try:
            info = os.stat(target)
        except OSError:
            continue
        if info.st_uid != 0:
            problems.append(f"managed {what} {target} is not owned by root, so its owner can change the managed rules")
        if info.st_mode & 0o022:
            problems.append(f"managed {what} {target} is writable by group or others, so they can change the "
                            "managed rules")
    return problems


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
        import subprocess

        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or os.getcwd(), capture_output=True,
                              text=True, timeout=3, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return os.path.realpath(out.splitlines()[0]) if proc.returncode == 0 and out else None


def project_state_path(cwd: str | None = None) -> str | None:
    root = project_root(cwd)
    return os.path.join(root, ".claude", "plugins", "data", bootstrap.PLUGIN_ID, "state.json") if root else None


def load(path: str | None) -> State:
    """Missing file → {}; unreadable (including a directory we cannot enter) or non-object JSON → StateError."""
    if not path:
        return {}
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except (OSError, ValueError) as exc:
        raise StateError(f"{path}: {exc}") from exc
    if not isinstance(state, dict):
        raise StateError(f"{path}: top level is not a JSON object")
    return state


def prune(sessions: object) -> dict[str, Any]:
    if not isinstance(sessions, dict):
        return {}
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=SESSION_TTL_DAYS)
    kept: dict[str, Any] = {}
    for sid, record in sessions.items():
        if not isinstance(record, dict):
            continue
        try:
            seen = datetime.datetime.fromisoformat(str(record.get("seenAt", "")))
        except ValueError:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=datetime.UTC)
        if seen >= cutoff:
            kept[str(sid)] = record
    if len(kept) > MAX_SESSIONS:
        newest = sorted(kept.items(), key=lambda kv: str(kv[1].get("seenAt", "")), reverse=True)
        kept = dict(newest[:MAX_SESSIONS])
    return kept


def make_dirs(directory: str, mode: int) -> None:
    """Create missing directories with exactly mode, whatever the umask."""
    missing = []
    current = os.path.abspath(directory)
    while not os.path.isdir(current) and os.path.dirname(current) != current:
        missing.append(current)
        current = os.path.dirname(current)
    for path in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, mode)
        os.chmod(path, mode)


def write(path: str, state: State, mode: int | None = None) -> None:
    """Atomic, durable write; with mode, the file gets those permissions and missing directories are made 0755."""
    state["updatedAt"] = now()
    if "sessions" in state:
        state["sessions"] = prune(state["sessions"])
    directory = os.path.dirname(path)
    if mode is None:
        os.makedirs(directory, exist_ok=True)
    else:
        make_dirs(directory, MANAGED_DIR_MODE)
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


@contextlib.contextmanager
def locked(path: str, dir_mode: int | None = None) -> Iterator[None]:
    directory = os.path.dirname(path)
    if dir_mode is None:
        os.makedirs(directory, exist_ok=True)
    else:
        make_dirs(directory, dir_mode)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def mutate[T](path: str, fn: Callable[[State], T], mode: int | None = None) -> T:
    """Load, apply fn, write back, all under the file lock; nothing is written if fn raises."""
    with locked(path, None if mode is None else MANAGED_DIR_MODE):
        state = load(path)
        try:
            result = fn(state)
        except StateError as exc:
            raise StateError(f"{path}: {exc}") from exc
        write(path, state, mode)
    return result
