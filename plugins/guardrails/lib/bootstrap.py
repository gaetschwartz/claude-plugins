"""Install and verify the guardrails runtime: a pinned uv, a managed Python and ast-grep-py, all under the plugin data dir.

Stdlib only, Python 3.9 syntax: this is the one module that runs on whatever interpreter the host has.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal, NamedTuple

LIB = Path(__file__).resolve().parent
BACKOFF_SECONDS = 600
NOTICE_REPEAT_SECONDS = 600
INSTALL_SECONDS = 100.0
DOWNLOAD_PREFIX = "https://files.pythonhosted.org/"
UV_MAX_BYTES = 120 << 20
ENV_ALLOWED = ("HOME", "LANG", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
               "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")
SELF_TEST = ("import importlib.metadata as m, sys\nfrom ast_grep_py import SgRoot\n"
             "found = SgRoot('echo a | cat', 'bash').root().find({'rule': {'kind': 'pipeline'}})\n"
             "assert found is not None and found.text() == 'echo a | cat'\n"
             "print(sys.version.split()[0], m.version('ast-grep-py'))\n")

State = Literal["ready", "installed", "backoff", "busy", "failed", "unsupported"]


class Wheel(NamedTuple):
    url: str
    sha256: str
    size: int
    member: str


class Pins(NamedTuple):
    bootstrap: int
    python: str
    ast_grep_py: str
    uv: str
    wheels: dict[str, Wheel]
    runtime_id: str


class Outcome(NamedTuple):
    state: State
    reason: str = ""
    retry_at: float = 0.0


class Unsupported(Exception):
    """This platform has no runtime."""


class InstallError(Exception):
    """An install step failed; the message is safe to show."""


def load_pins() -> Pins:
    manifest = json.loads((LIB / "runtime-manifest.json").read_text())
    wheels = {key: Wheel(w["url"], w["sha256"], w["size"], w["member"]) for key, w in manifest["uv"]["wheels"].items()}
    runtime_id = (LIB / "runtime-id").read_text().strip()
    return Pins(manifest["bootstrapVersion"], manifest["python"], manifest["astGrepPy"], manifest["uv"]["version"],
                wheels, runtime_id)


def plugin_id(here: Path = LIB) -> str:
    """Mirror Claude Code's CLAUDE_PLUGIN_DATA naming: <plugin>-<marketplace>, sanitised."""
    parts = here.parts
    if "cache" in parts:
        i = len(parts) - 1 - parts[::-1].index("cache")
        if i >= 1 and parts[i - 1] == "plugins" and len(parts) > i + 3:
            return re.sub(r"[^A-Za-z0-9_-]", "-", f"{parts[i + 2]}-{parts[i + 1]}")
    return "guardrails-gaetans-claude-plugins"


def data_dir() -> Path:
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(data) if data else Path.home() / ".claude" / "plugins" / "data" / plugin_id()


def runtime_dir(data: Path, pins: Pins) -> Path:
    return data / "runtime" / pins.runtime_id


def platform_key() -> str:
    import platform

    machine = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine().lower())
    system = platform.system()
    if machine is None or system not in ("Darwin", "Linux"):
        raise Unsupported(f"unsupported platform {system} {platform.machine()}")
    if system == "Darwin":
        return f"darwin-{machine}"
    libc = os.confstr("CS_GNU_LIBC_VERSION") if hasattr(os, "confstr") else None
    if not libc or not libc.startswith("glibc "):
        raise Unsupported(f"unsupported platform: Linux {platform.machine()} without glibc (musl is not supported)")
    version = tuple(int(x) for x in re.findall(r"\d+", libc)[:2])
    if version < (2, 28):
        raise Unsupported(f"unsupported platform: {libc} is older than glibc 2.28")
    return f"linux-{'aarch64' if machine == 'arm64' else machine}"


def inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    real, base = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return real == base or base in real.parents


def sanitised(text: str, limit: int = 200) -> str:
    return re.sub(r"[^\x20-\x7e]+", " ", text).strip()[:limit]


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temp.write_text(text)
    os.replace(temp, path)


def stat_record(path: Path) -> dict[str, int]:
    info = path.stat()
    return {"size": info.st_size, "mtimeNs": info.st_mtime_ns}


def run_files(rt: Path) -> list[Path]:
    """The files the hook executes or loads: the interpreter and the ast-grep extension module."""
    extension = sorted((rt / "venv").glob("lib/python*/site-packages/ast_grep_py/ast_grep_py*.so"))
    return [rt / "venv" / "bin" / "python", *extension]


def marker_problem(rt: Path, pins: Pins, project: Path | None, cwd: Path | None) -> str | None:
    """None when the runtime is complete and its run-time files are ours, unmodified and outside the project."""
    if inside(rt, project) or inside(rt, cwd):
        return "the runtime directory is inside the project directory"
    try:
        marker = json.loads((rt / "marker.json").read_text())
    except (OSError, ValueError):
        return "not installed"
    if not isinstance(marker, dict) or marker.get("runtimeId") != pins.runtime_id:
        return "marker does not match the pins"
    files = marker.get("files")
    if not isinstance(files, dict) or not files:
        return "marker lists no files"
    for rel, recorded in files.items():
        path = rt / rel
        try:
            info = path.stat()
        except OSError:
            return f"{rel} is missing"
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            return f"{rel} is not owned by this user or is writable by others"
        if recorded != {"size": info.st_size, "mtimeNs": info.st_mtime_ns} or not inside(path, rt):
            return f"{rel} changed since the install"
    return None


class Deadline:
    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def left(self, cap: float) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise InstallError("the install ran out of time")
        return min(cap, remaining)


def failure_path(data: Path) -> Path:
    return data / "runtime" / "failure.json"


class Failure(NamedTuple):
    at: float
    reason: str


def read_failure(data: Path) -> Failure | None:
    try:
        raw = json.loads(failure_path(data).read_text())
        return Failure(float(raw["at"]), str(raw["reason"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def retry_at(data: Path) -> float:
    failure = read_failure(data)
    return failure.at + BACKOFF_SECONDS if failure else 0.0


@contextlib.contextmanager
def locked(data: Path, wait: float) -> Iterator[bool]:
    """Hold the install lock (released by the OS if we die); False when another install keeps it for `wait` seconds."""
    (data / "runtime").mkdir(parents=True, mode=0o700, exist_ok=True)
    with open(data / "runtime" / "lock", "a") as handle:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= end:
                    yield False
                    return
                time.sleep(0.2)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def tool_env(rt: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_ALLOWED if key in os.environ}
    env.update(PATH="/usr/bin:/bin", UV_CACHE_DIR=str(rt / "uv-cache"), UV_PYTHON_INSTALL_DIR=str(rt / "python"),
               UV_PYTHON_BIN_DIR=str(rt / "python-bin"), UV_TOOL_DIR=str(rt / "tools"), UV_NO_CONFIG="1",
               UV_PYTHON_PREFERENCE="only-managed", UV_NO_PROGRESS="1", UV_LINK_MODE="copy", UV_HTTP_TIMEOUT="30")
    return env


def download(wheel: Wheel, dest: Path, deadline: Deadline) -> None:
    """Fetch the wheel to dest, refusing any other host and anything whose sha256 differs."""
    import hashlib
    import urllib.request

    if not wheel.url.startswith(DOWNLOAD_PREFIX):
        raise InstallError("the manifest names a download host other than files.pythonhosted.org")
    digest, size = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(wheel.url, timeout=deadline.left(60)) as response, open(dest, "wb") as out:
            while chunk := response.read(1 << 20):
                deadline.left(60)
                size += len(chunk)
                if size > UV_MAX_BYTES:
                    raise InstallError("the uv download is larger than expected")
                digest.update(chunk)
                out.write(chunk)
    except OSError as exc:
        raise InstallError(f"cannot download uv from files.pythonhosted.org ({sanitised(str(exc))})") from exc
    if digest.hexdigest() != wheel.sha256:
        raise InstallError("the downloaded uv wheel does not match the pinned sha256 and was discarded")


def extract_uv(wheel: Wheel, archive: Path, dest: Path) -> None:
    """Write the one member of the (already verified) wheel that is the uv binary."""
    import shutil
    import zipfile

    try:
        with zipfile.ZipFile(archive) as zf, zf.open(wheel.member) as src:
            temp = dest.with_name(dest.name + ".tmp")
            with open(temp, "wb") as out:
                shutil.copyfileobj(src, out)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise InstallError(f"cannot unpack uv from its wheel ({sanitised(str(exc))})") from exc
    temp.chmod(0o700)
    os.replace(temp, dest)


def run_tool(argv: list[str], rt: Path, env: dict[str, str], deadline: Deadline, what: str) -> str:
    import subprocess

    try:
        done = subprocess.run(argv, cwd=rt, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=deadline.left(90), check=False)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"{what} timed out") from exc
    except OSError as exc:
        raise InstallError(f"{what} could not start ({sanitised(exc.strerror or str(exc))})") from exc
    if done.returncode:
        lines = [line for line in (done.stderr or done.stdout).splitlines() if line.strip()]
        raise InstallError(f"{what} failed ({sanitised(lines[-1] if lines else f'exit {done.returncode}')})")
    return done.stdout


def install(rt: Path, pins: Pins, plat: str, deadline: Deadline, progress: Callable[[str], None]) -> None:
    import hashlib
    import shutil

    shutil.rmtree(rt, ignore_errors=True)
    rt.mkdir(parents=True, mode=0o700)
    wheel, uv, env = pins.wheels[plat], rt / "bin" / "uv", tool_env(rt)
    uv.parent.mkdir(mode=0o700)
    progress(f"downloading uv {pins.uv} ({wheel.size >> 20} MB)")
    download(wheel, rt / "uv.whl", deadline)
    extract_uv(wheel, rt / "uv.whl", uv)
    (rt / "uv.whl").unlink()
    python = rt / "venv" / "bin" / "python"
    progress(f"installing Python {pins.python} (standalone build from GitHub releases)")
    run_tool([str(uv), "python", "install", "--no-config", pins.python], rt, env, deadline, "uv python install")
    run_tool([str(uv), "venv", "--no-config", "--python", pins.python, "--quiet", str(rt / "venv")], rt, env, deadline,
             "uv venv")
    progress(f"installing ast-grep-py {pins.ast_grep_py}")
    run_tool([str(uv), "pip", "install", "--no-config", "--python", str(python), "--only-binary", ":all:", "--no-deps",
              "--require-hashes", "--quiet", "-r", str(LIB / "runtime-requirements.txt")], rt, env, deadline,
             "uv pip install")
    shutil.rmtree(rt / "uv-cache", ignore_errors=True)
    reported = run_tool([str(python), "-I", "-c", SELF_TEST], rt, {}, deadline, "self-test").split()
    if reported[1:] != [pins.ast_grep_py]:
        raise InstallError("the installed ast-grep-py is not the pinned version")
    files = {str(path.relative_to(rt)): stat_record(path) for path in run_files(rt)}
    write_atomic(rt / "marker.json", json.dumps({
        "runtimeId": pins.runtime_id, "bootstrap": pins.bootstrap, "uv": pins.uv, "pythonPin": pins.python,
        "python": reported[0], "astGrepPy": pins.ast_grep_py, "platform": plat,
        "requirementsSha256": hashlib.sha256((LIB / "runtime-requirements.txt").read_bytes()).hexdigest(),
        "installedAt": time.strftime("%Y-%m-%d %H:%M:%S%z"), "files": files}, indent=2))


def clean_up(data: Path, keep: Path) -> None:
    """With the lock held: stale temp files and runtimes of other pins are leftovers."""
    import shutil

    root = data / "runtime"
    for entry in root.iterdir():
        if entry.is_dir() and entry != keep and entry.name.startswith("r"):
            shutil.rmtree(entry, ignore_errors=True)
        elif ".tmp" in entry.name:
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)


def ensure(data: Path, *, wait: float = 120.0, retry_now: bool = False, project: Path | None = None,
           cwd: Path | None = None, progress: Callable[[str], None] = lambda text: None) -> Outcome:
    pins = load_pins()
    try:
        plat = platform_key()
    except Unsupported as exc:
        return Outcome("unsupported", str(exc))
    rt = runtime_dir(data, pins)
    if inside(rt, project) or inside(rt, cwd):
        return Outcome("failed", "the runtime directory is inside the project directory")
    if marker_problem(rt, pins, project, cwd) is None:
        return Outcome("ready")
    if not retry_now and time.time() < retry_at(data):
        return Outcome("backoff", (read_failure(data) or Failure(0, "")).reason, retry_at(data))
    with locked(data, wait) as acquired:
        if not acquired:
            return Outcome("busy")
        if marker_problem(rt, pins, project, cwd) is None:
            return Outcome("ready")
        try:
            clean_up(data, rt)
            install(rt, pins, plat, Deadline(INSTALL_SECONDS), progress)
        except (InstallError, OSError, ValueError) as exc:
            reason = sanitised(str(exc)) if isinstance(exc, InstallError) else sanitised(f"{type(exc).__name__}: {exc}")
            write_atomic(failure_path(data), json.dumps({"at": time.time(), "reason": reason}))
            return Outcome("failed", reason, time.time() + BACKOFF_SECONDS)
        failure_path(data).unlink(missing_ok=True)
    return Outcome("installed")


def clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def notice(outcome: Outcome) -> str:
    """The text for the user and the agent while rules cannot be enforced; fixed wording, no repo-controlled text."""
    tail = "guardrails rules are NOT enforced until it is ready, so this command was not checked."
    if outcome.state == "unsupported":
        return (f"guardrails: {outcome.reason}. The rules engine cannot run here, so guardrails rules are NOT enforced "
                "and commands are not checked.")
    if outcome.state in ("failed", "backoff"):
        when = f" The next automatic attempt is at {clock(outcome.retry_at)}." if outcome.retry_at else ""
        return (f"guardrails: the rules runtime could not be installed ({outcome.reason}).{when} Rules are NOT "
                "enforced meanwhile and commands are not checked. `guardrails engine status` shows the details.")
    return ("guardrails: the rules runtime is being installed automatically (about 10 seconds the first time, nothing "
            f"for you to do); {tail}")


def spawn_ensure() -> None:
    """Start an ensure in the background, detached from the hook that asked for it."""
    import subprocess

    argv = [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "ensure", "--quiet", "--no-wait"]
    env = {key: os.environ[key] for key in (*ENV_ALLOWED, "CLAUDE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR") if key in os.environ}
    with contextlib.suppress(OSError):
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, cwd="/", env=env)


def status_now(data: Path) -> Outcome:
    """What a hook sees without installing anything: ready, unsupported, in backoff, busy installing, or untried."""
    pins = load_pins()
    try:
        platform_key()
    except Unsupported as exc:
        return Outcome("unsupported", str(exc))
    if marker_problem(runtime_dir(data, pins), pins, optional_path("CLAUDE_PROJECT_DIR"), Path.cwd()) is None:
        return Outcome("ready")
    if time.time() < retry_at(data):
        return Outcome("backoff", (read_failure(data) or Failure(0, "")).reason, retry_at(data))
    with locked(data, 0) as free:
        return Outcome("failed" if free else "busy", "not installed")


def due(data: Path, session: str, outcome: Outcome) -> bool:
    """Show the notice once per session, and again every NOTICE_REPEAT_SECONDS while an install failure persists."""
    stamp = data / "runtime" / "notices" / (re.sub(r"[^A-Za-z0-9_-]", "_", session)[:80] or "nosession")
    try:
        seen = stamp.stat().st_mtime
    except OSError:
        seen = None
    repeats = outcome.state in ("failed", "backoff", "unsupported")
    if seen is not None and not (repeats and time.time() - seen >= NOTICE_REPEAT_SECONDS):
        return False
    with contextlib.suppress(OSError):
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp)
    return True


def hook(stdin_text: str, data: Path) -> str:
    """The PreToolUse answer while the runtime is not ready: allow, loudly and at most once per interval."""
    try:
        payload = json.loads(stdin_text)
        session = str(payload.get("session_id") or "nosession") if isinstance(payload, dict) else "nosession"
    except ValueError:
        session = "nosession"
    outcome = status_now(data)
    if outcome.state == "failed":
        spawn_ensure()
        outcome = Outcome("busy")
    if not due(data, session, outcome):
        return ""
    text = notice(outcome)
    return json.dumps({"systemMessage": text, "hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                                      "additionalContext": text}})


def cli_ensure(args: list[str]) -> int:
    quiet = "--quiet" in args
    outcome = ensure(data_dir(), wait=0 if "--no-wait" in args else 120.0, retry_now="--retry-now" in args, project=optional_path("CLAUDE_PROJECT_DIR"),
                     cwd=Path.cwd(), progress=(lambda text: None) if quiet else lambda text: print(f"guardrails: {text}",
                                                                                                     file=sys.stderr))
    if outcome.state in ("ready", "installed"):
        if outcome.state == "installed" and not quiet:
            print("guardrails: the rules runtime is installed", file=sys.stderr)
        return 0
    if not quiet:
        print(notice(outcome) if outcome.state != "busy" else "guardrails: another install is still running",
              file=sys.stderr)
    return 2


def optional_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def session_start() -> str:
    """SessionStart: install synchronously when needed; only trouble produces output."""
    outcome = ensure(data_dir(), project=optional_path("CLAUDE_PROJECT_DIR"), cwd=Path.cwd())
    if outcome.state in ("ready", "installed"):
        return json.dumps({"systemMessage": "guardrails: the rules runtime was installed"}) if outcome.state == "installed" else ""
    return json.dumps({"systemMessage": notice(outcome)})


def exec_guard(args: list[str]) -> None:
    python = str(runtime_dir(data_dir(), load_pins()) / "venv" / "bin" / "python")
    os.execv(python, [python, "-I", str(LIB / "guard.py"), *args])


def main(argv: list[str]) -> int:
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    if command == "ensure":
        return cli_ensure(args)
    if command == "run":
        code = cli_ensure([])
        if code == 0:
            exec_guard(args)
        return code
    if command == "session-start":
        sys.stdout.write(session_start())
        return 0
    if command == "hook":
        if status_now(data_dir()).state == "ready":
            exec_guard([])
        sys.stdout.write(hook(sys.stdin.read(), data_dir()))
        return 0
    print("usage: bootstrap.py ensure [--quiet] [--retry-now] [--no-wait] | run ARGS | session-start | hook", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
