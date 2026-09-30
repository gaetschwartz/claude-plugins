"""Run the AST worker from a hash-pinned ast-grep-py venv kept in the plugin data dir, never from repo-controlled config."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import Any

PIN = "0.45.3"
DEADLINE = 4.0
RETRY_AFTER = 600.0
UV_ENV = "GUARDRAILS_UV"
INPROCESS_ENV = "GUARDRAILS_AST_INPROCESS"
BOOTSTRAP_ENV = "GUARDRAILS_AST_BOOTSTRAP"
STAMP = "ast-download-failed"
READY = "guardrails-ast-ready"
INDEX = "https://pypi.org/simple"
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "astworker.py")
REQUIREMENTS = os.path.join(HERE, "ast-requirements.txt")
RUN_ENV = ("HOME", "LANG", "TMPDIR")
CLEAN_PATH = "/usr/bin:/bin"
WHEEL_PYTHONS = (10, 14)
PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy")
INSTALL_ENV = (*RUN_ENV, "UV_CACHE_DIR", "XDG_CACHE_HOME", "PIP_CACHE_DIR", *PROXIES)
UV_PROBES = ("~/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv")


class Unavailable(Exception):
    """The AST engine could not produce an answer; the reason is meant for the user."""


def uv_path() -> str | None:
    override = os.environ.get(UV_ENV)
    if override is not None:
        return override if override and shutil.which(override) else None
    for probe in UV_PROBES:
        path = os.path.expanduser(probe)
        if os.access(path, os.X_OK):
            return path
    return shutil.which("uv")


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
    return " ".join(lines[:2])[:160] if lines else "no output"


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


def find_interpreter() -> str:
    """An interpreter ast-grep-py has a wheel for: the running one, else the newest python3.N found in usual places."""
    if suitable(sys.version_info[:2]):
        return sys.executable
    dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", *os.environ.get("PATH", "").split(os.pathsep)]
    if sys.platform.startswith("linux"):
        dirs.insert(0, "/home/linuxbrew/.linuxbrew/bin")
    for minor in range(WHEEL_PYTHONS[1], WHEEL_PYTHONS[0] - 1, -1):
        for directory in dirs:
            candidate = os.path.join(directory, f"python3.{minor}")
            if directory and os.access(candidate, os.X_OK):
                return candidate
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


def ensure(state_dir: str | None, budget: float = DEADLINE) -> str:
    """The interpreter of the ready venv, building it when missing or stale: uv when found, else venv + pip."""
    venv = venv_dir(state_dir)
    if ready(venv):
        return venv_python(venv)
    mode = os.environ.get(BOOTSTRAP_ENV)
    if mode == "none":
        raise Unavailable("ast-grep-py is not installed and bootstrapping is disabled")
    cwd = workdir(state_dir)
    stamp = os.path.join(cwd, STAMP)
    if _recent(stamp):
        raise Unavailable("ast-grep-py is not installed and installing it failed within the last ten minutes")
    interpreter = find_interpreter()
    uv = uv_path()
    deadline = time.monotonic() + budget
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
            with contextlib.suppress(OSError), open(stamp, "w"):
                pass
            missing = " Install the python venv/ensurepip package or uv." if "ensurepip" in failure else ""
            raise Unavailable(f"installing ast-grep-py=={PIN} with {how} failed: {failure}.{missing}")
        proc = _run([venv_python(built), "-I", "-c", "import ast_grep_py"], None, 10.0, scrubbed(RUN_ENV), cwd)
        if proc.returncode != 0:
            raise Unavailable(f"the installed wheel cannot be imported: {_summary(proc.stderr)}")
        with open(os.path.join(built, READY), "w") as fh:
            fh.write(requirements_digest() + "\n")
        old = f"{venv}.old-{os.getpid()}"
        with contextlib.suppress(OSError):
            os.rename(venv, old)
        try:
            os.rename(built, venv)
        except OSError as exc:
            if not ready(venv):
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


@contextlib.contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    """Raise TimeoutError in the main thread after this long (a no-op where signals cannot do it)."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expire(*_: object) -> None:
        raise TimeoutError

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def running_in(venv: str) -> bool:
    return os.path.realpath(sys.prefix) == os.path.realpath(venv)


def _in_process(request: dict[str, Any], limit: float) -> Any:
    try:
        import astworker

        with time_limit(limit):
            return astworker.handle(json.loads(json.dumps(request)))
    except ImportError as exc:
        raise Unavailable(f"ast_grep_py is not importable: {exc}") from exc
    except TimeoutError as exc:
        raise Unavailable(f"timed out after {limit:.1f}s") from exc
    except Exception as exc:
        raise Unavailable(f"the AST worker failed: {type(exc).__name__}: {exc}") from exc


def call(request: dict[str, Any], state_dir: str | None = None) -> dict[str, Any]:
    """The worker's response to request; raise Unavailable with a reason when it cannot answer.

    Under the venv's own interpreter (what the hook wrapper prefers) the worker runs in this process; otherwise the
    venv is built if needed and a second process runs the worker.
    """
    started = time.monotonic()
    if os.environ.get(INPROCESS_ENV) == "1" or (running_in(venv_dir(state_dir)) and ready(venv_dir(state_dir))):
        response = _in_process(request, DEADLINE)
    else:
        python = ensure(state_dir)
        left = DEADLINE - (time.monotonic() - started)
        if left < 0.5:
            raise Unavailable(f"timed out after {DEADLINE:.1f}s")
        proc = _run([python, "-I", WORKER], json.dumps(request), left, scrubbed(RUN_ENV), workdir(state_dir))
        try:
            response = json.loads(proc.stdout)
        except ValueError:
            raise Unavailable(f"the AST worker failed: {_summary(proc.stderr)}") from None
    if not _shape_ok(request.get("op"), response):
        detail = response.get("error") if isinstance(response, dict) else None
        raise Unavailable(f"the AST worker failed: {detail or 'unexpected reply'}")
    return response


def warm(state_dir: str | None = None) -> None:
    """Build the venv in a detached process when it is missing; never raises."""
    if os.environ.get(INPROCESS_ENV) == "1" or os.environ.get(BOOTSTRAP_ENV) == "none":
        return
    with contextlib.suppress(OSError):
        if ready(venv_dir(state_dir)) or _recent(os.path.join(workdir(state_dir), STAMP)):
            return
        subprocess.Popen([sys.executable, os.path.join(HERE, "guard.py"), "warm-install", state_dir or ""],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=scrubbed((*INSTALL_ENV, UV_ENV, BOOTSTRAP_ENV)))
