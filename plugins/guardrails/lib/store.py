"""State file locations and safe read/modify/write of guardrails state."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

import bootstrap
import policy

SESSION_TTL = datetime.timedelta(days=7)
MAX_SESSIONS = 50
HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "bin" / "guardrails"

State = dict[str, Any]


class Layers(NamedTuple):
    managed: State
    glob: State
    project: State


def managed_path_for(platform: str) -> Path:
    return Path("/Library/Application Support/ClaudeCode/guardrails.json" if platform == "darwin"
                else "/etc/claude-code/guardrails.json")


MANAGED_PATH = managed_path_for(sys.platform)


class StateError(Exception):
    """A state file exists but cannot be used; it must never be silently overwritten."""


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def state_dir(root: Path) -> Path:
    return root / ".claude" / "plugins" / "data" / bootstrap.PLUGIN_ID


def global_state_path() -> Path:
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    return (Path(data) if data else state_dir(Path.home())) / "state.json"


def load_managed() -> tuple[State, list[str]]:
    """The managed layer and what is wrong with it; an unusable file is skipped, never fatal."""
    try:
        state = load(MANAGED_PATH)
    except StateError as exc:
        problem = ("unreadable managed state, so its rules are NOT enforced until it is fixed (fix or remove the file "
                   f"by hand; the CLI never overwrites a corrupt state file): {exc}")
        return {"rules": {}, "modes": {}}, [problem]
    layer, problems = policy.managed_layer(state, str(MANAGED_PATH))
    return layer, problems + policy.removed_key_problems(("managed", state))


def presence(path: Path) -> str:
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError:
        return "unreadable"
    return "present"


def trust_problems(path: Path) -> list[str]:
    """POSIX only: the managed file and its directory should be root-owned and not writable by others."""
    problems = []
    for what, target in (("file", path), ("directory", path.parent)):
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


def project_root(cwd: Path | None = None) -> Path | None:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    import subprocess

    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or Path.cwd(), capture_output=True,
                              text=True, timeout=3, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return Path(out.splitlines()[0]).resolve() if proc.returncode == 0 and out else None


def project_state_path(cwd: Path | None = None) -> Path | None:
    """The project's state file; None without a project, or when it is the global file (a project at $HOME)."""
    root = project_root(cwd)
    path = state_dir(root) / "state.json" if root else None
    return None if path is None or path.resolve() == global_state_path().resolve() else path


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
    cutoff = datetime.datetime.now(datetime.UTC) - SESSION_TTL
    live: dict[str, datetime.datetime] = {}
    for sid, record in sessions.items():
        with contextlib.suppress(KeyError, TypeError, ValueError):
            if (seen := datetime.datetime.fromisoformat(record["seenAt"])) >= cutoff:
                live[sid] = seen
    return {sid: sessions[sid] for sid in sorted(live, key=live.__getitem__, reverse=True)[:MAX_SESSIONS]}


def write(path: Path, state: State, public: bool = False) -> None:
    """Atomic write, private (0600) unless public (0644, for the managed file)."""
    state["updatedAt"] = now()
    if "sessions" in state:
        state["sessions"] = prune(state["sessions"])
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False) as tmp:
        try:
            json.dump(state, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            if public:
                os.fchmod(tmp.fileno(), 0o644)
            Path(tmp.name).replace(path)
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def mutate[T](path: Path, fn: Callable[[State], T], public: bool = False) -> T:
    """Load, apply fn, write back, all under the file lock; nothing is written if fn raises."""
    with locked(path):
        state = load(path)
        try:
            result = fn(state)
        except StateError as exc:
            raise StateError(f"{path}: {exc}") from exc
        write(path, state, public)
    return result
