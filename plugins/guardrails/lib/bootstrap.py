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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast

LIB = Path(__file__).resolve().parent
BACKOFFS = (600, 3600, 21600)
INSTALL_SECONDS = 60.0
NETWORK_SECONDS = 10.0
KEEP_DAYS = 30
PLUGIN_ID = "guardrails-gaetans-claude-plugins"
DOWNLOAD_PREFIX = "https://files.pythonhosted.org/"
ENV_ALLOWED = ("HOME", "LANG", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
               "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")
SELF_TEST = ("import importlib.metadata as m, sys\nsys.path.insert(0, sys.argv[1])\nimport scanner\n"
             "print(sys.version.split()[0], m.version('ast-grep-py'), scanner.self_test() or 'ok')\n")
State = Literal["ready", "installed", "backoff", "busy", "failed", "unsupported", "unsafe", "broken", "missing"]
Reason = Literal["dns", "connect", "timeout", "tls", "http", "hash", "disk", "tool", "crash"]

class Wheel(TypedDict):
    url: str
    sha256: str
    size: int
    member: str


@dataclass(frozen=True)
class Pins:
    python: str
    ast_grep_py: str
    uv: str
    wheels: dict[str, Wheel]
    runtime_id: str


@dataclass(frozen=True)
class Stamp:
    """The last failed install: when, how many in a row, and what kind of failure at which step."""

    at: float
    count: int
    reason: Reason
    step: str

    @property
    def retry_at(self) -> float:
        return self.at + BACKOFFS[min(self.count, len(BACKOFFS)) - 1]


@dataclass(frozen=True)
class Outcome:
    state: State
    reason: Reason | None = None
    step: str = ""
    detail: str = ""
    retry_at: float = 0.0


class InstallError(Exception):
    def __init__(self, reason: Reason, step: str, detail: str = "") -> None:
        super().__init__(f"{step}: {detail}")
        self.reason, self.step, self.detail = reason, step, detail


def load_pins() -> Pins:
    manifest = json.loads((LIB / "runtime-manifest.json").read_text())
    return Pins(manifest["python"], manifest["astGrepPy"], manifest["uv"]["version"], manifest["uv"]["wheels"],
                (LIB / "runtime-id").read_text().strip())


def data_dir() -> Path:
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(data) if data else Path(os.path.realpath(Path.home() / ".claude" / "plugins" / "data" / PLUGIN_ID))


def sanitised(text: str, limit: int = 120) -> str:
    return "".join(c if " " <= c <= "~" else " " for c in text).strip()[:limit]


def data_problem(data: Path) -> str | None:
    """Why the data dir is not safe to run code from: it must be absolute, canonical, ours and not writable by others."""
    if not data.is_absolute() or os.path.realpath(data) != str(data):
        return "not an absolute canonical path"
    try:
        info = data.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return "not accessible"
    return "not owned by you or writable by others" if info.st_uid != os.geteuid() or info.st_mode & 0o022 else None


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
    return data / "runtime" / pins.runtime_id


def marker_problem(rt: Path, pins: Pins) -> str | None:
    """None when the runtime is complete and the files the hook runs are ours, unmodified and inside it."""
    try:
        marker = json.loads((rt / "marker.json").read_text())
        files: dict[str, list[int]] = marker["files"]
        if marker["runtimeId"] != pins.runtime_id or not files:
            return "marker does not match the pins"
        for rel, (size, mtime) in files.items():
            info = (rt / rel).stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                return f"{rel} is not owned by you or is writable by others"
            if (size, mtime) != (info.st_size, info.st_mtime_ns) or not (rt / rel).resolve().is_relative_to(rt.resolve()):
                return f"{rel} changed since the install"
    except (OSError, ValueError, KeyError, TypeError):
        return "not installed"
    return "marked broken" if (rt / "broken").exists() else None


def read_stamp(data: Path) -> Stamp | None:
    """The last failed install: install.log's first line is `<count> <reason> <step>` and its mtime the attempt."""
    path = data / "runtime" / "install.log"
    try:
        count, reason, step = path.read_text().split("\n", 1)[0].split(" ", 2)
        return Stamp(path.stat().st_mtime, int(count), cast(Reason, reason), step)
    except (OSError, ValueError):
        return None


def diagnose(data: Path, pins: Pins | None = None) -> Outcome:
    """What a hook or CLI call can rely on right now, without installing anything."""
    pins = pins or load_pins()
    key, why = platform_problem()
    if why:
        return Outcome("unsupported", detail=sanitised(why))
    if (bad := data_problem(data)) is not None:
        return Outcome("unsafe", detail=f"{bad}: {sanitised(str(data))}")
    problem = marker_problem(runtime_dir(data, pins), pins)
    if problem is None:
        return Outcome("ready", detail=key)
    stamp = read_stamp(data)
    if stamp and time.time() < stamp.retry_at:
        return Outcome("backoff", stamp.reason, stamp.step, retry_at=stamp.retry_at)
    return Outcome("broken" if problem == "marked broken" else "missing", detail=problem)


def mark_broken(data: Path) -> None:
    """A trivial command crashed the library: the next ensure rebuilds the runtime."""
    with contextlib.suppress(OSError):
        (runtime_dir(data, load_pins()) / "broken").touch()


@contextlib.contextmanager
def locked(data: Path, wait: bool) -> Iterator[bool]:
    """Hold the install lock (the OS releases it if we die); False when `wait` is off and another install holds it."""
    (data / "runtime").mkdir(parents=True, mode=0o700, exist_ok=True)
    with open(data / "runtime" / "lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except OSError:
            yield False
            return
        yield True


def network_reason(exc: OSError) -> Reason:
    """What kind of network failure an exception is, by type."""
    import socket
    import ssl
    import urllib.error

    inner = getattr(exc, "reason", exc)
    if isinstance(exc, urllib.error.HTTPError):
        return "http"
    if isinstance(inner, socket.gaierror):
        return "dns"
    if isinstance(inner, ssl.SSLError):
        return "tls"
    return "timeout" if isinstance(inner, (TimeoutError, socket.timeout)) else "connect"


def remaining(deadline: float, cap: float, step: str) -> float:
    """Seconds a step may still take (at most `cap`); an install that is out of time stops here."""
    if (left := deadline - time.monotonic()) <= 0:
        raise InstallError("timeout", step, "out of time")
    return min(cap, left)


def download(wheel: Wheel, deadline: float) -> bytes:
    """The wheel's bytes, refusing any other host and anything whose sha256 differs."""
    import hashlib
    import urllib.request

    if not wheel["url"].startswith(DOWNLOAD_PREFIX):
        raise InstallError("tool", "uv download", "the manifest names another host")
    data = bytearray()
    try:
        with urllib.request.urlopen(wheel["url"], timeout=remaining(deadline, NETWORK_SECONDS, "uv download")) as response:
            while chunk := response.read(1 << 20):
                remaining(deadline, NETWORK_SECONDS, "uv download")
                data += chunk
                if len(data) > 2 * wheel["size"]:
                    break
    except OSError as exc:
        raise InstallError(network_reason(exc), "uv download", repr(exc)) from exc
    if hashlib.sha256(data).hexdigest() != wheel["sha256"]:
        raise InstallError("hash", "uv download", "sha256 differs from the pin")
    return bytes(data)


def run_tool(argv: list[str], rt: Path, env: dict[str, str], deadline: float, step: str) -> str:
    """Run one install step in its own process group, killed whole at the deadline."""
    import signal
    import subprocess

    try:
        proc = subprocess.Popen(argv, cwd=rt, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        out, err = proc.communicate(timeout=remaining(deadline, INSTALL_SECONDS, step))
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise InstallError("timeout", step, "killed at the deadline") from exc
    except OSError as exc:
        raise InstallError("tool", step, repr(exc)) from exc
    if proc.returncode:
        raise InstallError("tool", step, (err or out)[-1500:])
    return out


def install(rt: Path, pins: Pins, plat: str, progress: Callable[[str], None]) -> None:
    import io
    import shutil
    import zipfile

    deadline = time.monotonic() + INSTALL_SECONDS
    shutil.rmtree(rt, ignore_errors=True)
    (rt / "bin").mkdir(parents=True, mode=0o700)
    wheel, uv, python = pins.wheels[plat], rt / "bin" / "uv", rt / "venv" / "bin" / "python"
    env = {key: os.environ[key] for key in ENV_ALLOWED if key in os.environ}
    env.update(PATH="/usr/bin:/bin", UV_CACHE_DIR=str(rt / "uv-cache"), UV_PYTHON_INSTALL_DIR=str(rt / "python"),
               UV_PYTHON_BIN_DIR=str(rt / "python-bin"), UV_TOOL_DIR=str(rt / "tools"), UV_NO_CONFIG="1",
               UV_PYTHON_PREFERENCE="only-managed", UV_NO_PROGRESS="1", UV_LINK_MODE="copy",
               UV_HTTP_TIMEOUT=str(int(NETWORK_SECONDS)), UV_HTTP_RETRIES="0")
    progress(f"downloading uv {pins.uv} ({wheel['size'] >> 20} MB)")
    archive = download(wheel, deadline)
    try:
        uv.write_bytes(zipfile.ZipFile(io.BytesIO(archive)).read(wheel["member"]))
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise InstallError("disk", "uv unpack", repr(exc)) from exc
    uv.chmod(0o700)
    progress(f"installing Python {pins.python} (a standalone build, hash-checked by uv)")
    run_tool([str(uv), "python", "install", "--no-config", pins.python], rt, env, deadline, "python install")
    run_tool([str(uv), "venv", "--no-config", "--python", pins.python, "--quiet", str(rt / "venv")], rt, env, deadline,
             "venv")
    progress(f"installing ast-grep-py {pins.ast_grep_py}")
    run_tool([str(uv), "pip", "install", "--no-config", "--python", str(python), "--only-binary", ":all:", "--no-deps",
              "--require-hashes", "--quiet", "-r", str(LIB / "runtime-requirements.txt")], rt, env, deadline,
             "library install")
    shutil.rmtree(rt / "uv-cache", ignore_errors=True)
    version, library, verdict = run_tool([str(python), "-I", "-c", SELF_TEST, str(LIB)], rt, {}, deadline,
                                         "self-test").split()
    if library != pins.ast_grep_py or verdict != "ok":
        raise InstallError("crash", "self-test", f"{library} {verdict}")
    extension = sorted((rt / "venv").glob("lib/python*/site-packages/ast_grep_py/ast_grep_py*.so"))
    files = {str(path.relative_to(rt)): [path.stat().st_size, path.stat().st_mtime_ns] for path in [python, *extension]}
    (rt / "marker.json").write_text(json.dumps({"runtimeId": pins.runtime_id, "python": version, "files": files}))


def clean_up(data: Path, keep: Path) -> None:
    """With the lock held: runtimes of other pins go once their directory is over KEEP_DAYS old, never sooner."""
    import shutil

    cutoff = time.time() - KEEP_DAYS * 86400
    for entry in (data / "runtime").iterdir():
        if entry.is_dir() and entry != keep and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)


def ensure(data: Path, *, wait: bool = True, retry_now: bool = False,
           progress: Callable[[str], None] = lambda text: None) -> Outcome:
    """Make the runtime ready: a no-op when it is; otherwise install, unless a recent failure says to wait."""
    pins = load_pins()
    found = diagnose(data, pins)
    if found.state in ("ready", "unsupported", "unsafe") or (found.state == "backoff" and not retry_now):
        return found
    rt = runtime_dir(data, pins)
    data.mkdir(parents=True, mode=0o700, exist_ok=True)
    with locked(data, wait) as acquired:
        if not acquired:
            return Outcome("busy")
        if marker_problem(rt, pins) is None:
            return Outcome("ready")
        try:
            clean_up(data, rt)
            install(rt, pins, platform_problem().key, progress)
        except (InstallError, OSError, ValueError) as exc:
            error = exc if isinstance(exc, InstallError) else InstallError("disk", "install", repr(exc))
            previous = read_stamp(data)
            count = previous.count + 1 if previous else 1
            log = data / "runtime" / "install.log"
            log.write_text(f"{count} {error.reason} {error.step}\n{error.detail}\n")
            os.utime(log, (time.time(), time.time()))
            return Outcome("failed", error.reason, error.step, retry_at=time.time() + BACKOFFS[min(count, len(BACKOFFS)) - 1])
        (data / "runtime" / "install.log").unlink(missing_ok=True)
    return Outcome("installed")


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(LIB))
    import hostcli

    return hostcli.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
