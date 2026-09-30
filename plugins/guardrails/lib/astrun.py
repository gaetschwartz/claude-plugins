"""Run the AST worker from a hash-pinned ast-grep-py venv kept in the plugin data dir, never from repo-controlled input."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from typing import Any

import watchdog

PIN = "0.45.3"
DEADLINE = 4.0
RETRY_AFTER = 600.0
STALE_AFTER = 600.0
STAMP = "ast-download-failed"
READY = "guardrails-ast-ready"
LOCK = ".venv.lock"
INDEX = "https://pypi.org/simple"
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "astworker.py")
REQUIREMENTS = os.path.join(HERE, "ast-requirements.txt")
RUN_ENV = ("HOME", "LANG", "TMPDIR")
CLEAN_PATH = "/usr/bin:/bin"
PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy")
INSTALL_ENV = (*RUN_ENV, "UV_CACHE_DIR", "XDG_CACHE_HOME", "PIP_CACHE_DIR", *PROXIES)
FIXED_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
LINUXBREW = "/home/linuxbrew/.linuxbrew/bin"
WHEEL_PYTHONS = (10, 14)

INPROCESS: bool = False
BUILD_ALLOWED: bool = False
REJECTED: list[str] = []


class Unavailable(Exception):
    """The AST engine could not produce an answer; the reason is meant for the user."""


def take_rejected() -> list[str]:
    out = list(dict.fromkeys(REJECTED))
    REJECTED.clear()
    return out


def _inside(real: str, root: str | None) -> bool:
    if not root:
        return False
    base = os.path.realpath(root)
    home = os.path.realpath(os.path.expanduser("~"))
    if base == os.sep or home == base or home.startswith(base + os.sep):
        return False
    return real == base or real.startswith(base + os.sep)


def untrusted(path: str) -> str | None:
    """Why an executable must not be run by the hook, or None: inside the project or cwd, or writable by others."""
    real = os.path.realpath(path)
    for root in (os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd()):
        if _inside(real, root):
            return "it is inside the project directory"
    me = os.getuid()
    try:
        info = os.stat(real)
        if info.st_uid not in (0, me):
            return "it is owned by another user"
        for directory in {os.path.dirname(real), os.path.dirname(os.path.abspath(path))}:
            dinfo = os.stat(directory)
            if dinfo.st_mode & 0o002:
                return f"{directory} is writable by everyone"
            if dinfo.st_mode & 0o020 and dinfo.st_uid not in (0, me):
                return f"{directory} is group-writable and owned by another user"
    except OSError as exc:
        return exc.strerror or "it cannot be inspected"
    return None


def trusted_executable(candidates: list[str]) -> str | None:
    for path in candidates:
        if not os.access(path, os.X_OK) or os.path.isdir(path):
            continue
        why = untrusted(path)
        if why is None:
            return path
        REJECTED.append(f"guardrails: ignored {path} as an executable to run: {why}")
    return None


def path_dirs() -> list[str]:
    return [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]


def uv_path() -> str | None:
    """uv from fixed locations first, PATH last; nothing inside the project or writable by others."""
    fixed = [os.path.expanduser("~/.local/bin/uv"), *(os.path.join(d, "uv") for d in FIXED_DIRS)]
    if sys.platform.startswith("linux"):
        fixed.append(os.path.join(LINUXBREW, "uv"))
    return trusted_executable([*fixed, *(os.path.join(d, "uv") for d in path_dirs())])


def scrubbed(names: tuple[str, ...], extra: dict[str, str] | None = None) -> dict[str, str]:
    """Only these variables reach a child: a repository can set the rest (index, find-links, PYTHON*, PIP_*, SSL_*) in
    its settings, while cache, proxy and locale variables are harmless because every wheel is hash-checked."""
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["PATH"] = CLEAN_PATH
    env.update(extra or {})
    return env


def workdir(state_dir: str | None) -> str:
    if state_dir:
        with contextlib.suppress(OSError):
            os.makedirs(state_dir, exist_ok=True)
            return state_dir
    return tempfile.gettempdir()


def requirements_digest() -> str:
    with open(REQUIREMENTS, "rb") as fh:
        return f"{PIN}-{hashlib.sha256(fh.read()).hexdigest()[:16]}"


def venv_dir(state_dir: str | None) -> str:
    return os.path.join(workdir(state_dir), "venv")


def venv_python(venv: str) -> str:
    return os.path.join(venv, "bin", "python")


def ready(venv: str) -> bool:
    """The venv exists, was built from the current pin and hash set, and has its interpreter."""
    try:
        with open(os.path.join(venv, READY)) as fh:
            return fh.read().strip() == requirements_digest() and os.access(venv_python(venv), os.X_OK)
    except OSError:
        return False


def _summary(text: str) -> str:
    lines = [ln.strip(" ×╰─▶") for ln in text.splitlines() if ln.strip()]
    out = " ".join(lines[:2])[:160] if lines else "no output"
    if "certificate" in out.lower():
        out += " (a custom CA or package mirror is not supported; the wheel is fetched from pypi.org only)"
    return out


def _run(cmd: list[str], payload: str | None, timeout: float, env: dict[str, str],
         cwd: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, input=payload, capture_output=True, text=True, timeout=timeout, check=False,
                              env=env, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise Unavailable(f"timed out after {timeout:.1f}s") from exc
    except OSError as exc:
        raise Unavailable(f"cannot run {cmd[0]}: {exc.strerror or exc}") from exc


def _recent(stamp: str | None) -> bool:
    if not stamp:
        return False
    try:
        return 0 <= time.time() - os.stat(stamp).st_mtime < RETRY_AFTER
    except OSError:
        return False


def suitable(version: tuple[int, int]) -> bool:
    return version[0] == 3 and WHEEL_PYTHONS[0] <= version[1] <= WHEEL_PYTHONS[1]


def find_interpreter(cwd: str | None = None) -> str:
    """The newest trusted interpreter ast-grep-py has a wheel for, looking in fixed locations before PATH."""
    dirs = [*FIXED_DIRS, *([LINUXBREW] if sys.platform.startswith("linux") else [])]
    for minor in range(WHEEL_PYTHONS[1], WHEEL_PYTHONS[0] - 1, -1):
        found = trusted_executable([os.path.join(d, f"python3.{minor}") for d in dirs])
        if found is not None:
            return found
    if suitable(sys.version_info[:2]) and untrusted(sys.executable) is None:
        return sys.executable
    for minor in range(WHEEL_PYTHONS[1], WHEEL_PYTHONS[0] - 1, -1):
        found = trusted_executable([os.path.join(d, f"python3.{minor}") for d in [*path_dirs(), "/usr/bin"]])
        if found is not None:
            return found
    raise Unavailable(f"ast-grep-py has no wheel for Python {sys.version_info[0]}.{sys.version_info[1]}; it needs "
                      f"3.{WHEEL_PYTHONS[0]} to 3.{WHEEL_PYTHONS[1]} and none was found")


def uv_steps(uv: str, venv: str, offline: bool, interpreter: str | None = None) -> list[list[str]]:
    return [
        [uv, "venv", "--quiet", "--no-config", "--no-python-downloads", "--python", interpreter or sys.executable,
         venv],
        [uv, "pip", "install", "--quiet", "--no-config", "--python", venv_python(venv), "--no-deps",
         "--only-binary", ":all:", "--require-hashes", "--default-index", INDEX, *(["--offline"] if offline else []),
         "-r", REQUIREMENTS],
    ]


def pip_steps(venv: str, interpreter: str | None = None) -> list[list[str]]:
    return [
        [interpreter or sys.executable, "-I", "-m", "venv", venv],
        [venv_python(venv), "-I", "-m", "pip", "install", "--isolated", "--no-input", "--disable-pip-version-check",
         "--quiet", "--no-deps", "--only-binary", ":all:", "--require-hashes", "--index-url", INDEX, "-r",
         REQUIREMENTS],
    ]


def _build(steps: list[list[str]], cwd: str, deadline: float, extra: dict[str, str] | None) -> str | None:
    """Run the steps; the first failure's summary, or None when all passed."""
    for step in steps:
        left = deadline - time.monotonic()
        if left <= 0:
            return "timed out"
        proc = _run(step, None, left, scrubbed(INSTALL_ENV, extra), cwd)
        if proc.returncode != 0:
            return _summary(proc.stderr or proc.stdout)
    return None


@contextlib.contextmanager
def locked(cwd: str, deadline: float) -> Iterator[None]:
    fd = os.open(os.path.join(cwd, LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Unavailable("another process is building the AST venv") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def reap(cwd: str) -> None:
    """Remove build leftovers older than ten minutes; callers hold the lock, so none is in use."""
    for name in os.listdir(cwd):
        if name.startswith((".venv-", "venv.old-")):
            path = os.path.join(cwd, name)
            with contextlib.suppress(OSError):
                if time.time() - os.stat(path).st_mtime > STALE_AFTER:
                    shutil.rmtree(path, ignore_errors=True)


def ensure(state_dir: str | None, budget: float = DEADLINE) -> str:
    """The interpreter of the ready venv, building it when missing or stale: uv when found, else venv + pip.

    Only the session-start warm-up and CLI commands call this; the hook itself never installs anything.
    """
    venv = venv_dir(state_dir)
    if ready(venv):
        return venv_python(venv)
    cwd = workdir(state_dir)
    stamp = os.path.join(cwd, STAMP)
    if _recent(stamp):
        raise Unavailable("installing ast-grep-py failed within the last ten minutes")
    deadline = time.monotonic() + budget
    try:
        with locked(cwd, deadline):
            if ready(venv):
                return venv_python(venv)
            reap(cwd)
            return _build_venv(venv, cwd, deadline)
    except Unavailable:
        with contextlib.suppress(OSError), open(stamp, "w"):
            pass
        raise
    except OSError as exc:
        raise Unavailable(f"cannot prepare {cwd}: {exc.strerror or exc}") from exc


def _build_venv(venv: str, cwd: str, deadline: float) -> str:
    stamp = os.path.join(cwd, STAMP)
    interpreter = find_interpreter(cwd)
    uv = uv_path()
    tmp = tempfile.mkdtemp(dir=cwd, prefix=".venv-")
    built = os.path.join(tmp, "venv")
    try:
        if uv is not None:
            failure = _build(uv_steps(uv, built, True, interpreter), cwd, deadline, None)
            if failure is not None:
                shutil.rmtree(built, ignore_errors=True)
                failure = _build(uv_steps(uv, built, False, interpreter), cwd, deadline, None)
            how = "uv"
        else:
            failure = _build(pip_steps(built, interpreter), cwd, deadline,
                             {"PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
            how = "python -m venv and pip (uv was not found)"
        if failure is not None:
            missing = " Install the python venv/ensurepip package or uv." if "ensurepip" in failure else ""
            raise Unavailable(f"installing ast-grep-py=={PIN} with {how} failed: {failure}.{missing}")
        left = max(1.0, deadline - time.monotonic())
        proc = _run([venv_python(built), "-I", "-c", "import ast_grep_py"], None, left, scrubbed(RUN_ENV), cwd)
        if proc.returncode != 0:
            raise Unavailable(f"the installed wheel cannot be imported: {_summary(proc.stderr)}")
        with open(os.path.join(built, READY), "w") as fh:
            fh.write(requirements_digest() + "\n")
        if not ready(venv):
            old = f"{venv}.old-{os.getpid()}"
            with contextlib.suppress(OSError):
                os.rename(venv, old)
            try:
                os.rename(built, venv)
            except OSError as exc:
                raise Unavailable(f"could not place the new venv: {exc.strerror or exc}") from exc
            shutil.rmtree(old, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.unlink(stamp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return venv_python(venv)


def _shape_ok(op: object, response: object) -> bool:
    if not isinstance(response, dict) or response.get("ok") is not True:
        return False
    if op == "eval":
        return isinstance(response.get("verdicts"), dict) and isinstance(response.get("errors"), dict)
    if op == "check":
        return isinstance(response.get("errors"), dict)
    if op == "tree":
        return isinstance(response.get("units"), list)
    return True


def running_in(venv: str) -> bool:
    return os.path.realpath(sys.prefix) == os.path.realpath(venv)


def _in_process(request: dict[str, Any], limit: float) -> Any:
    try:
        import astworker

        with watchdog.limit(limit):
            return astworker.handle(json.loads(json.dumps(request)))
    except ImportError as exc:
        raise Unavailable(f"ast_grep_py is not importable: {exc}") from exc
    except TimeoutError as exc:
        raise Unavailable(f"timed out after {limit:.1f}s") from exc
    except Exception as exc:
        raise Unavailable(f"the AST worker failed: {type(exc).__name__}: {exc}") from exc


def call(request: dict[str, Any], state_dir: str | None = None) -> dict[str, Any]:
    """The worker's response to request; raise Unavailable with a reason when it cannot answer.

    Under the venv's own interpreter (what the hook wrapper prefers) the worker runs in this process; otherwise it runs
    in a second process under the venv's python. A missing venv is built only when BUILD_ALLOWED (CLI commands).
    """
    started = time.monotonic()
    venv = venv_dir(state_dir)
    if INPROCESS or (running_in(venv) and ready(venv)):
        response = _in_process(request, DEADLINE)
    else:
        if ready(venv):
            python = venv_python(venv)
        elif BUILD_ALLOWED:
            python = ensure(state_dir)
        else:
            raise Unavailable("the ast-grep-py venv is not built yet (the session-start warm-up builds it)")
        left = DEADLINE - (time.monotonic() - started)
        if left < 0.5:
            raise Unavailable(f"timed out after {DEADLINE:.1f}s")
        proc = _run([python, "-I", WORKER], json.dumps(request), left, scrubbed(RUN_ENV), workdir(state_dir))
        try:
            response = json.loads(proc.stdout)
        except ValueError:
            raise Unavailable(f"the venv python failed (exit {proc.returncode}): {_summary(proc.stderr)}") from None
    if not _shape_ok(request.get("op"), response):
        detail = response.get("error") if isinstance(response, dict) else None
        raise Unavailable(f"the AST worker failed: {detail or 'unexpected reply'}")
    return response


def maintain(state_dir: str | None) -> None:
    """Reap stale build leftovers when nobody is building."""
    cwd = workdir(state_dir)
    with contextlib.suppress(OSError, Unavailable), locked(cwd, time.monotonic() + 2.0):
        reap(cwd)


def warm(state_dir: str | None = None) -> None:
    """Build the venv (and reap leftovers) in a detached process; never raises."""
    if INPROCESS:
        return
    with contextlib.suppress(OSError):
        if _recent(os.path.join(workdir(state_dir), STAMP)):
            return
        subprocess.Popen([sys.executable, os.path.join(HERE, "guard.py"), "warm-install", state_dir or ""],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=scrubbed(INSTALL_ENV))
