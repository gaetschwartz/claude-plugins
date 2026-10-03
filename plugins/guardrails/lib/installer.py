"""Build the runtime: the hash-checked uv wheel, a managed Python and ast-grep-py. Stdlib only, Python 3.9 syntax."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from bootstrap import KEEP_SECONDS, LIB, Pins, Wheel

INSTALL_SECONDS = 60.0
NETWORK_SECONDS = 10.0
DOWNLOAD_PREFIX = "https://files.pythonhosted.org/"
ENV_ALLOWED = ("HOME", "LANG", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
               "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")
SELF_TEST = ("import importlib.metadata as m, sys\nsys.path.insert(0, sys.argv[1])\nimport scanner\n"
             "print(sys.version.split()[0], m.version('ast-grep-py'), scanner.self_test() or 'ok')\n")
DNS = "a download host could not be resolved (DNS)"
CONNECT = "a connection to a download host failed"
TIMEOUT = "a download or install step timed out"
TLS = "a TLS certificate check failed"
HTTP = "a download host answered with an error status"
HASH = "a downloaded file did not match its pinned hash and was discarded"
DISK = "a file could not be written (disk space or permissions)"
TOOL = "an install step failed"
CRASH = "the installed library crashes on a trivial command"


class InstallError(Exception):
    """An install step failed: `why` is a fixed phrase for notices, the message is the raw detail for install.log."""

    def __init__(self, why: str, detail: str = "") -> None:
        super().__init__(detail)
        self.why = why


def download(wheel: Wheel, deadline: float) -> bytes:
    """The wheel's bytes, refusing any other host and anything whose sha256 differs."""
    import hashlib
    import socket
    import ssl
    import urllib.error
    import urllib.request

    if not wheel["url"].startswith(DOWNLOAD_PREFIX):
        raise InstallError(TOOL, "the manifest names another host")
    data = bytearray()
    try:
        with urllib.request.urlopen(wheel["url"], timeout=NETWORK_SECONDS) as response:
            while chunk := response.read(1 << 20):
                data += chunk
                if time.monotonic() > deadline:
                    raise InstallError(TIMEOUT, "out of time")
                if len(data) > 2 * wheel["size"]:
                    break
    except OSError as exc:
        inner = getattr(exc, "reason", exc)
        raise InstallError(HTTP if isinstance(exc, urllib.error.HTTPError) else DNS if isinstance(inner, socket.gaierror)
                           else TLS if isinstance(inner, ssl.SSLError) else TIMEOUT if isinstance(inner, (socket.timeout, TimeoutError))
                           else CONNECT, repr(exc)) from exc
    if hashlib.sha256(data).hexdigest() != wheel["sha256"]:
        raise InstallError(HASH, "sha256 differs from the pin")
    return bytes(data)


def run_tool(argv: list[str], rt: Path, env: dict[str, str], deadline: float) -> str:
    try:
        done = subprocess.run(argv, cwd=rt, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=max(deadline - time.monotonic(), 0.1), start_new_session=True, check=False)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(TIMEOUT, "killed at the deadline") from exc
    except OSError as exc:
        raise InstallError(TOOL, repr(exc)) from exc
    if done.returncode:
        raise InstallError(TOOL, (done.stderr or done.stdout)[-1500:])
    return done.stdout


def build(rt: Path, pins: Pins, plat: str) -> None:
    deadline = time.monotonic() + INSTALL_SECONDS
    (rt / "bin").mkdir(parents=True, mode=0o700)
    wheel, uv, python = pins.wheels[plat], rt / "bin" / "uv", rt / "venv" / "bin" / "python"
    env = {key: value for key, value in os.environ.items() if key in ENV_ALLOWED}
    env.update(PATH="/usr/bin:/bin", UV_CACHE_DIR=str(rt / "uv-cache"), UV_PYTHON_INSTALL_DIR=str(rt / "python"),
               UV_PYTHON_BIN_DIR=str(rt / "python-bin"), UV_NO_CONFIG="1", UV_PYTHON_PREFERENCE="only-managed",
               UV_LINK_MODE="copy", UV_HTTP_TIMEOUT=str(int(NETWORK_SECONDS)), UV_HTTP_RETRIES="0")
    archive = download(wheel, deadline)
    try:
        uv.write_bytes(zipfile.ZipFile(io.BytesIO(archive)).read(wheel["member"]))
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise InstallError(DISK, repr(exc)) from exc
    uv.chmod(0o700)
    run_tool([str(uv), "python", "install", pins.python], rt, env, deadline)
    run_tool([str(uv), "venv", "--python", pins.python, "--quiet", str(rt / "venv")], rt, env, deadline)
    run_tool([str(uv), "pip", "install", "--python", str(python), "--only-binary", ":all:", "--no-deps",
              "--require-hashes", "--quiet", "-r", str(LIB / "runtime-requirements.txt")], rt, env, deadline)
    shutil.rmtree(rt / "uv-cache", ignore_errors=True)
    version, library, verdict = run_tool([str(python), "-I", "-c", SELF_TEST, str(LIB)], rt, {}, deadline).split()
    if library != pins.ast_grep_py or verdict != "ok":
        raise InstallError(CRASH, f"{library} {verdict}")
    files = [python, *sorted((rt / "venv").glob("lib/python*/site-packages/ast_grep_py/ast_grep_py*.so"))]
    (rt / "marker.json").write_text(json.dumps({"runtimeId": pins.runtime_id, "python": version,
                                                "files": [str(path.relative_to(rt)) for path in files]}))


def install(link: Path, pins: Pins, plat: str) -> None:
    """Build in a fresh directory next to `link`, self-test it, then point `link` at it and remove the old build. The
    runtime in use is never touched before its replacement works; a failed build leaves nothing behind."""
    rt = Path(tempfile.mkdtemp(prefix=f"{link.name}.", dir=link.parent))
    try:
        build(rt, pins, plat)
        previous = link.resolve() if link.is_symlink() else None
        swap = link.with_name(f".{rt.name}.link")
        swap.symlink_to(rt.name)
        swap.replace(link)
    except BaseException:
        shutil.rmtree(rt, ignore_errors=True)
        raise
    if previous is not None and previous != rt:
        shutil.rmtree(previous, ignore_errors=True)


def clean_up(data: Path, keep: Path) -> None:
    """With the lock held: leftovers of interrupted installs go at once, other pins' runtimes after KEEP_SECONDS."""
    current = keep.resolve() if keep.is_symlink() else None
    for entry in (data / "runtime").iterdir():
        if entry in (keep, current) or entry.name in ("install.log", "lock"):
            continue
        if entry.name.startswith((".", f"{keep.name}.")) or entry.lstat().st_mtime < time.time() - KEEP_SECONDS:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
