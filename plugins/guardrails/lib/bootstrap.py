"""Install and verify the guardrails runtime: a pinned uv, a managed Python and ast-grep-py, all under the plugin data dir.

Stdlib only, Python 3.9 syntax: with hostcli.py (what a hook or CLI call says while the runtime is not ready) this is
the code that runs on whatever interpreter the host has.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict

LIB = Path(__file__).resolve().parent
REPEAT_SECONDS = 600
BACKOFFS = (REPEAT_SECONDS, 3600, 21600)
KEEP_SECONDS = 30 * 86400
PLUGIN_ID = "guardrails-gaetans-claude-plugins"
State = Literal["ready", "installed", "backoff", "busy", "failed", "unsupported", "relative", "missing"]


class Wheel(TypedDict):
    url: str
    sha256: str
    size: int
    member: str


class Pins(NamedTuple):
    python: str
    ast_grep_py: str
    uv: str
    wheels: dict[str, Wheel]
    runtime_id: str


class Outcome(NamedTuple):
    """`detail` is a fixed phrase, never text from the network, the environment or the repository."""

    state: State
    detail: str = ""
    retry_at: float = 0.0


class Stamp(NamedTuple):
    at: float
    count: int
    why: str

    @property
    def retry_at(self) -> float:
        return self.at + BACKOFFS[min(self.count, len(BACKOFFS)) - 1]


def load_pins() -> Pins:
    manifest = json.loads((LIB / "runtime-manifest.json").read_text())
    return Pins(manifest["python"], manifest["astGrepPy"], manifest["uv"]["version"], manifest["uv"]["wheels"],
                (LIB / "runtime-id").read_text().strip())


def data_dir() -> Path:
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(data) if data else Path.home() / ".claude" / "plugins" / "data" / PLUGIN_ID


def sanitised(text: str, limit: int = 120) -> str:
    return "".join(c if " " <= c <= "~" else " " for c in text).strip()[:limit]


def session_id(payload: object) -> str:
    """The session a hook payload belongs to; calls without one share a single bucket."""
    found = payload.get("session_id") if isinstance(payload, dict) else None
    return str(found or "nosession")


class Platform(NamedTuple):
    key: str
    why: str | None


def platform_problem() -> Platform:
    """The platform key, or why there is no runtime for it."""
    import platform

    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine().lower())
    system = platform.system()
    if arch is None or system not in ("Darwin", "Linux"):
        return Platform("", f"{system} {platform.machine()}")
    if system == "Darwin":
        return Platform(f"darwin-{arch}", None)
    try:
        libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (OSError, ValueError):
        libc = ""
    if not libc.startswith("glibc "):
        return Platform("", f"Linux {platform.machine()} without glibc (musl is not supported)")
    if tuple(int(x) for x in libc.split()[1].split(".")[:2]) < (2, 28):
        return Platform("", f"{libc} is older than glibc 2.28")
    return Platform(f"linux-{'aarch64' if arch == 'arm64' else arch}", None)


def runtime_dir(data: Path, pins: Pins) -> Path:
    """The path everything runs from: a symlink to the current build, swapped atomically by an install."""
    return data / "runtime" / pins.runtime_id


def marker_problem(rt: Path, pins: Pins) -> str | None:
    """None when the runtime is complete: the marker matches the pins and the files the hook runs exist."""
    try:
        marker = json.loads((rt / "marker.json").read_text())
        if marker["runtimeId"] != pins.runtime_id or (rt / "broken").exists():
            return "marker does not match the pins or the runtime is marked broken"
        for rel in marker["files"]:
            (rt / rel).stat()
    except (OSError, ValueError, KeyError, TypeError):
        return "not installed"
    return None


def read_stamp(data: Path) -> Stamp | None:
    """The last failed install: install.log's first line is `<count> <why>` and its mtime the attempt."""
    log = data / "runtime" / "install.log"
    try:
        count, why = log.read_text().split("\n", 1)[0].split(" ", 1)
        return Stamp(log.stat().st_mtime, int(count), why)
    except (OSError, ValueError):
        return None


def diagnose(data: Path, pins: Pins | None = None) -> Outcome:
    """What a hook or CLI call can rely on right now, without installing anything."""
    pins = pins or load_pins()
    key, why = platform_problem()
    if why:
        return Outcome("unsupported", sanitised(why))
    if not data.is_absolute():
        return Outcome("relative", "not an absolute path")
    problem = marker_problem(runtime_dir(data, pins), pins)
    if problem is None:
        return Outcome("ready", key)
    stamp = read_stamp(data)
    if stamp and time.time() < stamp.retry_at:
        return Outcome("backoff", stamp.why, stamp.retry_at)
    return Outcome("missing", problem)


def mark_broken(data: Path) -> None:
    """A trivial command crashed the library: the next ensure rebuilds the runtime."""
    with contextlib.suppress(OSError):
        (runtime_dir(data, load_pins()) / "broken").touch()


@contextlib.contextmanager
def locked(data: Path, wait: bool) -> Generator[bool]:
    """Hold the install lock (the OS releases it if we die); False when `wait` is off and another install holds it."""
    (data / "runtime").mkdir(parents=True, mode=0o700, exist_ok=True)
    with open(data / "runtime" / "lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except OSError:
            yield False
            return
        yield True


def ensure(data: Path, *, wait: bool = True, retry_now: bool = False) -> Outcome:
    """Make the runtime ready: a no-op when it is; otherwise install, unless a recent failure says to wait."""
    pins = load_pins()
    found = diagnose(data, pins)
    if found.state in ("ready", "unsupported", "relative") or (found.state == "backoff" and not retry_now):
        return found
    rt = runtime_dir(data, pins)
    data.mkdir(parents=True, mode=0o700, exist_ok=True)
    with locked(data, wait) as acquired:
        if not acquired:
            return Outcome("busy")
        if marker_problem(rt, pins) is None:
            return Outcome("ready")
        log = data / "runtime" / "install.log"
        import installer

        try:
            installer.clean_up(data, rt)
            installer.install(rt, pins, platform_problem().key)
        except (installer.InstallError, OSError, ValueError) as exc:
            why = exc.why if isinstance(exc, installer.InstallError) else installer.DISK
            previous = read_stamp(data)
            count = previous.count + 1 if previous else 1
            log.write_text(f"{count} {why}\n{exc!r}\n{exc}\n")
            return Outcome("failed", why, Stamp(time.time(), count, why).retry_at)
        log.unlink(missing_ok=True)
    return Outcome("installed")


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(LIB))
    import hostcli

    return hostcli.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
