"""State file locations and safe read/modify/write of guardrails state."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import bootstrap
import policy

MANAGED_ENV = "GUARDRAILS_MANAGED_PATH"
MANAGED_DARWIN = Path("/Library/Application Support/ClaudeCode/guardrails.json")
MANAGED_LINUX = Path("/etc/claude-code/guardrails.json")
MANAGED_MODE = 0o644
MANAGED_DIR_MODE = 0o755
SESSION_TTL_DAYS = 7
MAX_SESSIONS = 50
HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "bin" / "guardrails"

State = dict[str, Any]


class StateError(Exception):
    """A state file exists but cannot be used; it must never be silently overwritten."""


class NotWritable(StateError):
    """A state file (or the directory it would be created in) is not writable by this user."""


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def state_dir(root: Path) -> Path:
    return root / ".claude" / "plugins" / "data" / bootstrap.PLUGIN_ID


def global_state_path() -> Path:
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    return (Path(data) if data else state_dir(Path.home())) / "state.json"


def default_managed_path() -> Path:
    return MANAGED_DARWIN if sys.platform == "darwin" else MANAGED_LINUX


def env_managed_path() -> Path | None:
    override = os.environ.get(MANAGED_ENV)
    return Path(override) if override else None


def managed_paths(extra: Path | None = None) -> list[Path]:
    """Managed sources, highest ranked first: the platform default, the env override, then an explicit extra file."""
    paths = [default_managed_path()]
    for candidate in (env_managed_path(), extra):
        if candidate and all(candidate.absolute() != p.absolute() for p in paths):
            paths.append(candidate)
    return paths


def managed_write_path(extra: Path | None = None) -> Path:
    return extra or env_managed_path() or default_managed_path()


def hook_enforces(path: Path) -> bool:
    """Whether the hook loads this managed file (the platform default or the env override)."""
    return any(path.absolute() == known.absolute() for known in managed_paths())


def load_managed(extra: Path | None = None) -> tuple[State, list[str]]:
    """The combined managed layer and what is wrong with it; an unusable source is skipped, never fatal."""
    sources: list[tuple[str, State]] = []
    problems: list[str] = []
    for path in managed_paths(extra):
        try:
            sources.append((str(path), load(path)))
        except StateError as exc:
            problems.append("unreadable managed state, so its rules are NOT enforced until it is fixed (fix or "
                            f"remove the file by hand; the CLI never overwrites a corrupt state file): {exc}")
    layer, more = policy.managed_layer(sources)
    return layer, problems + more


def presence(path: Path) -> str:
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return " (absent)"
    except OSError:
        return " (unreadable)"
    return ""


def trust_problems(path: Path) -> list[str]:
    """POSIX only: the managed file and its directory should be root-owned and not writable by others."""
    problems = []
    for what, target in (("file", path), ("directory", path.absolute().parent)):
        try:
            info = target.stat()
        except OSError:
            continue
        if info.st_uid != 0:
            problems.append(f"managed {what} {target} is not owned by root, so its owner can change the managed rules")
        if info.st_mode & 0o022:
            problems.append(f"managed {what} {target} is writable by group or others, so they can change the "
                            "managed rules")
    return problems


def ensure_writable(path: Path) -> None:
    probe = path.absolute().parent
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        raise NotWritable(f"cannot write {path}: directory {probe} is not writable")
    if path.exists() and not os.access(path, os.W_OK):
        raise NotWritable(f"cannot write {path}: file is not writable")


def project_root(cwd: Path | None = None) -> Path | None:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    try:
        import subprocess

        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or Path.cwd(), capture_output=True,
                              text=True, timeout=3, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return Path(out.splitlines()[0]).resolve() if proc.returncode == 0 and out else None


def project_state_path(cwd: Path | None = None) -> Path | None:
    root = project_root(cwd)
    return state_dir(root) / "state.json" if root else None


def load(path: Path | None) -> State:
    """Missing file → {}; unreadable (including a directory we cannot enter) or non-object JSON → StateError."""
    if path is None:
        return {}
    try:
        state = json.loads(path.read_text())
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except (OSError, ValueError) as exc:
        raise StateError(f"{path}: {exc}") from exc
    if not isinstance(state, dict):
        raise StateError(f"{path}: top level is not a JSON object")
    return state


def prune(sessions: object) -> dict[str, Any]:
    """Keep the newest MAX_SESSIONS sessions seen within the TTL; a record with no readable seenAt is dropped."""
    if not isinstance(sessions, dict):
        return {}
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=SESSION_TTL_DAYS)
    seen: dict[str, datetime.datetime] = {}
    for sid, record in sessions.items():
        try:
            stamp = datetime.datetime.fromisoformat(str(record["seenAt"])) if isinstance(record, dict) else None
        except (KeyError, ValueError):
            continue
        if stamp is not None and (stamp := stamp.replace(tzinfo=stamp.tzinfo or datetime.UTC)) >= cutoff:
            seen[str(sid)] = stamp
    newest = sorted(seen, key=seen.__getitem__, reverse=True)[:MAX_SESSIONS]
    return {sid: sessions[sid] for sid in newest}


def make_dirs(directory: Path, mode: int) -> None:
    """Create missing directories with exactly mode, whatever the umask."""
    directory = directory.absolute()
    for path in reversed([d for d in (directory, *directory.parents) if not d.is_dir()]):
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=mode)
        path.chmod(mode)


def write(path: Path, state: State, mode: int | None = None) -> None:
    """Atomic write; with mode, the file gets those permissions and missing directories are made 0755."""
    state["updatedAt"] = now()
    if "sessions" in state:
        state["sessions"] = prune(state["sessions"])
    if mode is None:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        make_dirs(path.parent, MANAGED_DIR_MODE)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        if mode is not None:
            Path(tmp).chmod(mode)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def locked(path: Path, dir_mode: int | None = None) -> Iterator[None]:
    if dir_mode is None:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        make_dirs(path.parent, dir_mode)
    fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def mutate[T](path: Path, fn: Callable[[State], T], mode: int | None = None) -> T:
    """Load, apply fn, write back, all under the file lock; nothing is written if fn raises."""
    with locked(path, None if mode is None else MANAGED_DIR_MODE):
        state = load(path)
        try:
            result = fn(state)
        except StateError as exc:
            raise StateError(f"{path}: {exc}") from exc
        write(path, state, mode)
    return result
