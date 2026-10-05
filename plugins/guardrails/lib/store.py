"""Config and state file locations, and safe read/modify/write of both.

Config (rules, modes, the enabled switch) is what a user or agent sets on purpose: global under the XDG config dir,
per project in `<project>/.claude/guardrails.json`, managed in a platform file. State is what the hook remembers per
session, in the plugin data dir. The hook writes state only.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

import bootstrap
import policy

SESSION_TTL = datetime.timedelta(days=7)
MAX_SESSIONS = 50
HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "bin" / "guardrails"
APP_DIR = Path("dev.gaetans.guardrails") / "claude-plugin"
CONFIG_KEYS = ("rules", "modes", "enabled")

Doc = dict[str, Any]


class Layers(NamedTuple):
    managed: Doc
    glob: Doc
    project: Doc


def managed_path_for(platform: str) -> Path:
    return Path("/Library/Application Support/ClaudeCode/guardrails.json" if platform == "darwin"
                else "/etc/claude-code/guardrails.json")


MANAGED_PATH = managed_path_for(sys.platform)


class StoreError(Exception):
    """A config or state file exists but cannot be used; it must never be silently overwritten."""


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def config_home() -> Path:
    """$XDG_CONFIG_HOME when it is absolute (the XDG spec says to ignore a relative one), else ~/.config."""
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or "")
    return xdg if xdg.is_absolute() else Path.home() / ".config"


def global_config_path() -> Path:
    return config_home() / APP_DIR / "config.json"


def state_path() -> Path:
    return bootstrap.data_dir() / "state.json"


def config_lock_path() -> Path:
    return bootstrap.data_dir() / "config.lock"


def load_managed() -> tuple[Doc, list[str]]:
    """The managed layer and what is wrong with it; an unusable file is skipped, never fatal."""
    try:
        doc = load(MANAGED_PATH)
    except StoreError as exc:
        problem = ("unreadable managed file, so its rules are NOT enforced until it is fixed (fix or remove the file "
                   f"by hand; the CLI never overwrites a corrupt file): {exc}")
        return {"rules": {}, "modes": {}}, [problem]
    layer, problems = policy.managed_layer(doc, str(MANAGED_PATH))
    return layer, problems + policy.removed_key_problems(("managed", doc))


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


def is_project(root: Path | None) -> TypeGuard[Path]:
    """A root at the home directory is no project: its .claude directory is the user's own."""
    return root is not None and root != Path.home().resolve()


def project_config_path(root: Path | None) -> Path | None:
    """The project's config file; None without a project, at the home directory, or when it is the global file."""
    if not is_project(root):
        return None
    path = root / ".claude" / "guardrails.json"
    return None if path.resolve() == global_config_path().resolve() else path


def old_project_state_path(root: Path) -> Path:
    return root / ".claude" / "plugins" / "data" / bootstrap.PLUGIN_ID / "state.json"


def stray_config_problems(state: Doc) -> list[str]:
    """Configuration keys left in the state file, where they are no longer read."""
    keys = [key for key in CONFIG_KEYS if key in state]
    if not keys:
        return []
    return [(f"the state file {state_path()} still holds {', '.join(keys)}, which is configuration and is no longer "
             "read from there, so it is NOT applied (its rules are not enforced). Move it to "
             f"{global_config_path()} and remove {', '.join(keys)} from the state file")]


def old_project_problems(root: Path | None) -> list[str]:
    """A project's state file from before project config moved to `<project>/.claude/guardrails.json`."""
    if not is_project(root):
        return []
    old = old_project_state_path(root)
    if presence(old) == "absent" or old.resolve() == state_path().resolve():
        return []
    return [(f"the old project state file {old} is no longer read, so its rules and modes are NOT enforced. Move its "
             f"rules, modes and enabled to {root / '.claude' / 'guardrails.json'} and delete the old file")]


def load(path: Path | None) -> Doc:
    """Missing file → {}; unreadable (including a directory we cannot enter) or non-object JSON → StoreError."""
    if path is None:
        return {}
    try:
        doc = json.loads(path.read_text())
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except (OSError, ValueError) as exc:
        raise StoreError(f"{path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise StoreError(f"{path}: top level is not a JSON object")
    return doc


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


def write(path: Path, doc: Doc, public: bool = False) -> None:
    """Atomic write, private (0600) unless public (0644, for the managed file)."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False) as tmp:
        try:
            json.dump(doc, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            if public:
                os.fchmod(tmp.fileno(), 0o644)
            Path(tmp.name).replace(path)
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise


def write_state(state: Doc) -> None:
    state["updatedAt"] = now()
    if "sessions" in state:
        state["sessions"] = prune(state["sessions"])
    write(state_path(), state)


@contextlib.contextmanager
def locked(lock: Path) -> Iterator[None]:
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def state_lock() -> contextlib.AbstractContextManager[None]:
    return locked(state_path().with_name("state.json.lock"))


def mutate[T](path: Path, fn: Callable[[Doc], T], lock: Path, public: bool = False) -> T:
    """Load, apply fn, write back, all under `lock`; nothing is written if fn raises."""
    with locked(lock):
        doc = load(path)
        try:
            result = fn(doc)
        except StoreError as exc:
            raise StoreError(f"{path}: {exc}") from exc
        write(path, doc, public)
    return result


def mutate_config[T](path: Path, fn: Callable[[Doc], T]) -> T:
    """A global or project config change; the lock lives in the data dir, never next to the config."""
    return mutate(path, fn, config_lock_path())


def mutate_state[T](fn: Callable[[Doc], T]) -> T:
    with state_lock():
        state = load(state_path())
        try:
            result = fn(state)
        except StoreError as exc:
            raise StoreError(f"{state_path()}: {exc}") from exc
        write_state(state)
    return result
