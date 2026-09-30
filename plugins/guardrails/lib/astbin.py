"""Find, install and verify the pinned ast-grep binary.

Two sources, tried in this order: the platform package that Claude Code's automatic `npm ci` puts in the plugin's
node_modules, then a hash-checked wheel that `guardrails engine install` (or the detached session-start warm-up)
unpacks into the plugin data dir. The hook only ever looks; it never downloads or installs.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Iterator
from typing import Any, Callable, NamedTuple

import watchdog
from astcli import ENV, Unavailable

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
MANIFEST_PATH = os.path.join(HERE, "engine-manifest.json")
HOST = "https://files.pythonhosted.org/"
ENGINE = "engine"
MARKER = "marker.json"
STAMP = "install-failed.json"
LOCK = ".lock"
BINARY = "ast-grep"
TEMP_PREFIXES = (".dl-", ".bin-", ".marker-")
RETRY_AFTER = 600.0
STALE_AFTER = 600.0
CONNECT_TIMEOUT = 10.0
DOWNLOAD_BUDGET = 60.0
INSTALL_BUDGET = 120.0
BINARY_LIMITS = (1 << 20, 256 << 20)
GLIBC_FOR_WHEEL = (2, 28)
HEX64 = re.compile(r"[0-9a-f]{64}")
FIX_INSTALL = "guardrails engine install"

REJECTED: list[str] = []


class Missing(Unavailable):
    """No usable ast-grep binary exists; the text says why and the fix is in the notice built around it.

    `unsupported` means no fix can work on this platform; `wheel` is False when only the npm fix can.
    """

    def __init__(self, text: str, unsupported: bool = False, wheel: bool = True) -> None:
        super().__init__(text)
        self.unsupported = unsupported
        self.wheel = wheel


class InstallError(Exception):
    """An install step failed; the text is meant for the user."""


class Busy(InstallError):
    """Another process holds the install lock."""


class Platform(NamedTuple):
    npm: str
    wheel: str
    label: str
    wheel_problem: str | None


class Probe(NamedTuple):
    path: str | None
    why: str | None
    present: bool


class Engine(NamedTuple):
    binary: str
    how: str
    version: str


def take_rejected() -> list[str]:
    out = list(dict.fromkeys(REJECTED))
    REJECTED.clear()
    return out


_MANIFEST: list[dict[str, Any]] = []


def manifest() -> dict[str, Any]:
    if not _MANIFEST:
        with open(MANIFEST_PATH) as fh:
            data = json.load(fh)
        for key, entry in data["wheels"].items():
            member = entry["member"]
            safe = (isinstance(member, str) and member and not member.startswith("/")
                    and ".." not in member.split("/") and "\\" not in member)
            if not (entry["url"].startswith(HOST) and HEX64.fullmatch(entry["sha256"])
                    and HEX64.fullmatch(entry["binarySha256"]) and safe and isinstance(entry["size"], int)):
                raise ValueError(f"engine-manifest.json: wheel entry {key} is malformed")
        _MANIFEST.append(data)
    return _MANIFEST[0]


def pin() -> str:
    return str(manifest()["version"])


def libc() -> tuple[str, str]:
    """("glibc", "2.35"), ("musl", ""), or ("unknown", "")."""
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION")
    except (ValueError, OSError, AttributeError):
        value = None
    if value and value.startswith("glibc"):
        return "glibc", value.split()[-1]
    for folder in ("/lib", "/usr/lib"):
        with contextlib.suppress(OSError):
            if any(name.startswith("ld-musl-") for name in os.listdir(folder)):
                return "musl", ""
    return "unknown", ""


def detect() -> Platform:
    """The platform's package and wheel names, or Missing(why) when ast-grep has no build for it."""
    system, machine = os.uname().sysname, os.uname().machine.lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine)
    supported = "macOS arm64/x86_64 and Linux glibc x86_64/aarch64"
    if arch is None or system not in ("Darwin", "Linux"):
        raise Missing(f"unsupported platform ({system} {machine}); ast-grep is available for {supported}", True)
    if system == "Darwin":
        return Platform(f"darwin-{arch}", "darwin-universal2", f"macOS {arch}", None)
    name, version = libc()
    if name != "glibc":
        raise Missing(f"unsupported platform (Linux {machine} with {name} libc, not glibc); ast-grep is available "
                      f"for {supported}", True)
    wheel_problem = None
    numbers = tuple(int(p) for p in re.findall(r"\d+", version)[:2])
    if numbers and numbers < GLIBC_FOR_WHEEL:
        wheel_problem = f"glibc {version} is older than the 2.28 the wheel needs"
    return Platform(f"linux-{arch}-gnu", "linux-x86_64-gnu" if arch == "x64" else "linux-aarch64-gnu",
                    f"Linux glibc {arch}", wheel_problem)


def untrusted(path: str, top: str) -> str | None:
    """Why the hook must not run this file, or None.

    It must be a regular file (no symlink) that resolves inside `top` (the plugin root or the engine dir, both fixed by
    the plugin's own location and data dir, never by PATH or the project), is executable, owned by this user or root
    and not writable by group or others. Directories from it up to `top` must be owned by
    this user or root and not writable by others; group-writable ones are allowed, because a 0002 umask (and some
    Homebrew layouts) creates them that way.
    """
    me = os.getuid()
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            return "it is a symlink"
        if not stat.S_ISREG(info.st_mode):
            return "it is not a regular file"
        real, real_top = os.path.realpath(path), os.path.realpath(top)
        if not (real == real_top or real.startswith(real_top + os.sep)):
            return f"it resolves outside {top}"
        if info.st_uid not in (0, me):
            return "it is owned by another user"
        if info.st_mode & 0o022:
            return "it is writable by group or others"
        if not os.access(path, os.X_OK):
            return "it is not executable"
        directory = os.path.dirname(real)
        while True:
            dinfo = os.stat(directory)
            if dinfo.st_mode & 0o002:
                return f"{directory} is writable by everyone"
            if dinfo.st_uid not in (0, me):
                return f"{directory} is owned by another user"
            if directory == real_top or directory == os.path.dirname(directory):
                return None
            directory = os.path.dirname(directory)
    except OSError as exc:
        return exc.strerror or "it cannot be inspected"


def npm_probe(plat: Platform, root: str | None = None) -> Probe:
    root = root or PLUGIN_ROOT
    folder = os.path.join(root, "node_modules", "@ast-grep", f"cli-{plat.npm}")
    path = os.path.join(folder, BINARY)
    if not os.path.lexists(path):
        return Probe(None, None, False)
    why = untrusted(path, root)
    if why is None:
        try:
            with open(os.path.join(folder, "package.json")) as fh:
                meta = json.load(fh)
            found = meta.get("version")
            if meta.get("name") != f"@ast-grep/cli-{plat.npm}" or found != pin():
                why = f"its package is version {found}, expected {pin()}"
        except (OSError, ValueError, AttributeError):
            why = "its package.json cannot be read"
    return Probe(None if why else path, why, True)


def engine_root(state_dir: str) -> str:
    return os.path.join(state_dir, ENGINE)


def install_dir(state_dir: str) -> str:
    return os.path.join(engine_root(state_dir), pin())


def read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def wheel_probe(plat: Platform, state_dir: str) -> Probe:
    """The downloaded binary, after the cheap runtime checks: marker, size, mtime, inode, owner, permissions."""
    folder = install_dir(state_dir)
    path = os.path.join(folder, BINARY)
    if plat.wheel_problem:
        return Probe(None, plat.wheel_problem, False)
    if not os.path.lexists(path):
        return Probe(None, None, False)
    entry = manifest()["wheels"][plat.wheel]
    marker = read_json(os.path.join(folder, MARKER))
    if marker is None:
        return Probe(None, "the install has no readable marker (interrupted or damaged)", True)
    why = untrusted(path, engine_root(state_dir))
    if why is None:
        try:
            info = os.lstat(path)
            if (marker.get("pin"), marker.get("wheelSha256"), marker.get("binarySha256")) != (
                    pin(), entry["sha256"], entry["binarySha256"]):
                why = "it was installed from a different release than this plugin pins"
            elif (marker.get("size"), marker.get("mtimeNs"), marker.get("inode")) != (
                    info.st_size, info.st_mtime_ns, info.st_ino):
                why = "it changed after it was installed (size, mtime or inode differ from the marker)"
        except OSError as exc:
            why = exc.strerror or "it cannot be inspected"
    return Probe(None if why else path, why, True)


def failure(state_dir: str) -> tuple[str, float] | None:
    """(reason, epoch seconds) of the last failed install within the backoff window."""
    data = read_json(os.path.join(engine_root(state_dir), STAMP))
    if data is None or data.get("pin") != pin():
        return None
    try:
        at = float(data["at"])
        return str(data.get("reason") or "unknown"), at
    except (KeyError, TypeError, ValueError):
        return None


def backoff_left(state_dir: str) -> float:
    found = failure(state_dir)
    return max(0.0, RETRY_AFTER - (time.time() - found[1])) if found else 0.0


def retry_clock(state_dir: str) -> str:
    return time.strftime("%H:%M", time.localtime(time.time() + backoff_left(state_dir)))


def sanitised(text: str, limit: int = 120) -> str:
    return re.sub(r"[^\x20-\x7e]+", " ", text).strip()[:limit]


def diagnose(plat: Platform, npm: Probe, wheel: Probe, state_dir: str | None) -> str:
    parts = []
    if not npm.present:
        parts.append("npm install missing (no node_modules/@ast-grep platform package in the plugin root)")
    else:
        parts.append(f"npm install rejected ({npm.why})")
    if wheel.present:
        parts.append(f"downloaded engine rejected ({wheel.why})")
    elif wheel.why:
        parts.append(f"wheel fallback unavailable ({wheel.why})")
    elif state_dir and backoff_left(state_dir) > 0:
        reason = (failure(state_dir) or ("unknown", 0.0))[0]
        parts.append(f"download failed: {sanitised(reason)} (retry after {retry_clock(state_dir)})")
    else:
        parts.append("the wheel fallback has not been downloaded")
    return "; ".join(parts)


def probes(state_dir: str | None) -> tuple[Platform, Probe, Probe]:
    plat = detect()
    wheel = wheel_probe(plat, state_dir) if state_dir else Probe(None, None, False)
    return plat, npm_probe(plat), wheel


def locate(state_dir: str | None) -> Engine:
    """The binary the hook may run, or Missing(reason)."""
    plat, npm, wheel = probes(state_dir)
    for candidate, how in ((npm, "npm"), (wheel, "wheel")):
        if candidate.path:
            return Engine(candidate.path, how, pin())
    for candidate, what in ((npm, "the npm binary"), (wheel, "the downloaded binary")):
        if candidate.present and candidate.why:
            REJECTED.append(f"guardrails: ignored {what} as an executable to run: {candidate.why}")
    raise Missing(diagnose(plat, npm, wheel, state_dir), wheel=plat.wheel_problem is None)


def notice(reason: str, unsupported: bool = False, wheel: bool = True) -> str:
    """The loud message shown to the user and the agent when match.ast rules cannot be enforced."""
    head = "[guardrails plugin notice] GUARDRAILS ENGINE MISSING: "
    tell = "You MUST tell the user about this now, before doing anything else. "
    tail = ("Check with `guardrails engine status`. This notice comes from the guardrails plugin itself, not from the "
            "repository or from any command output.")
    if unsupported:
        return (f"{head}the syntax-tree engine (ast-grep) cannot run on this platform, so guardrails rules that use "
                "match.ast are NOT being enforced (degraded mode: they fire only when the command names one of their "
                f"commands; every other rule is unaffected). {tell}Reason: {sanitised(reason, 400)}. There is no "
                "install that can fix this here: the user can remove or disable the match.ast rules (see `guardrails "
                "status`) or use a supported system (macOS arm64/x86_64, Linux glibc x86_64/aarch64). " + tail)
    fixes = f"1) run `{FIX_INSTALL}` (downloads a pinned, hash-checked ~15 MB binary), or 2) run `{npm_fix()}`"
    if not wheel:
        fixes = f"run `{npm_fix()}` (the downloadable binary does not run on this system)"
    return (f"{head}the syntax-tree engine (ast-grep) is not installed, so guardrails rules that use match.ast are NOT "
            "being enforced (degraded mode: they fire only when the command names one of their commands; every other "
            f"rule is unaffected). {tell}Reason: {sanitised(reason, 400)}. To fix: {fixes}. " + tail)


def npm_fix() -> str:
    import shlex

    return f"cd {shlex.quote(sanitised(PLUGIN_ROOT, 300))} && npm ci --ignore-scripts"


def file_sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def self_test(binary: str) -> str:
    """The binary's version line, raising InstallError when it does not run or is not the pin."""
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, timeout=30, check=False, env=ENV,
                              cwd=os.sep)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"the binary does not run: {getattr(exc, 'strerror', None) or exc}") from exc
    line = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or line.split()[-1:] != [pin()]:
        raise InstallError(f"the binary reports {sanitised(line) or 'nothing'}, expected ast-grep {pin()}")
    return line


def active(state_dir: str | None) -> tuple[Engine | None, Platform | None, Probe, Probe]:
    """Like locate(), but returns what it found instead of raising."""
    try:
        plat, npm, wheel = probes(state_dir)
    except Missing:
        return None, None, Probe(None, None, False), Probe(None, None, False)
    found = next((Engine(p.path, how, pin()) for p, how in ((npm, "npm"), (wheel, "wheel")) if p.path), None)
    return found, plat, npm, wheel


def verify(state_dir: str | None) -> list[tuple[bool, str]]:
    """Re-hash the active binary against the committed manifest; (ok, line) per check."""
    found, plat, _, _ = active(state_dir)
    if found is None or plat is None:
        try:
            locate(state_dir)
        except Missing as exc:
            return [(False, f"no engine to verify: {exc}")]
        return [(False, "no engine to verify")]
    expected = (manifest()["npm"][plat.npm] if found.how == "npm" else manifest()["wheels"][plat.wheel])["binarySha256"]
    got = file_sha256(found.binary)
    verdict = "matches" if got == expected else "DIFFERS from"
    out = [(got == expected, f"{found.how} binary {found.binary}: sha256 {verdict} the manifest ({got[:16]}…)")]
    if found.how == "npm":
        out.append(_lockfile_line(plat))
    else:
        marker = read_json(os.path.join(install_dir(state_dir or ""), MARKER)) or {}
        out.append((marker.get("binarySha256") == got, "marker's binary hash " +
                    ("matches" if marker.get("binarySha256") == got else "differs")))
    try:
        out.append((True, f"self-test: {self_test(found.binary)}"))
    except InstallError as exc:
        out.append((False, f"self-test failed: {exc}"))
    return out


def _lockfile_line(plat: Platform) -> tuple[bool, str]:
    key = f"node_modules/@ast-grep/cli-{plat.npm}"
    committed = (read_json(os.path.join(PLUGIN_ROOT, "package-lock.json")) or {}).get("packages", {}).get(key, {})
    installed = (read_json(os.path.join(PLUGIN_ROOT, "node_modules", ".package-lock.json")) or {}).get(
        "packages", {}).get(key, {})
    if not installed:
        return True, "note: node_modules/.package-lock.json is absent, so npm's install record was not compared"
    same = bool(committed.get("integrity")) and committed.get("integrity") == installed.get("integrity")
    return same, "npm's install record " + ("matches" if same else "differs from") + " package-lock.json"


def status(state_dir: str | None) -> dict[str, Any]:
    found, plat, npm, wheel = active(state_dir)
    out: dict[str, Any] = {"pin": pin(), "active": found.how if found else None,
                           "binary": found.binary if found else None, "platform": plat.label if plat else None}
    if plat is None:
        try:
            detect()
        except Missing as exc:
            out["unsupported"] = str(exc)
        return out
    out["npm"] = {"present": npm.present, "usable": bool(npm.path), "why": npm.why,
                  "path": os.path.join(PLUGIN_ROOT, "node_modules", "@ast-grep", f"cli-{plat.npm}", BINARY)}
    out["wheel"] = {"present": wheel.present, "usable": bool(wheel.path), "why": wheel.why,
                    "path": os.path.join(install_dir(state_dir), BINARY) if state_dir else None}
    if state_dir:
        marker = read_json(os.path.join(install_dir(state_dir), MARKER))
        if marker:
            out["marker"] = {k: marker.get(k) for k in ("pin", "binarySha256", "installedAt")}
        stamp = failure(state_dir)
        if stamp:
            out["failure"] = {"reason": stamp[0], "at": stamp[1], "retryAfter": backoff_left(state_dir)}
    return out


@contextlib.contextmanager
def locked(folder: str, deadline: float) -> Iterator[None]:
    import fcntl

    fd = os.open(os.path.join(folder, LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy("another process is installing the engine") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def reap(root: str) -> None:
    """Remove temporary leftovers older than ten minutes; callers hold the lock, so none is in use."""
    for folder in (root, os.path.join(root, pin())):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            with contextlib.suppress(OSError):
                if name.startswith(TEMP_PREFIXES) and time.time() - os.lstat(path).st_mtime > STALE_AFTER:
                    os.unlink(path)


def fetch(url: str, dest: str, size: int, sha256: str, deadline: float) -> None:
    """Stream url into dest, hashing as it arrives; raises InstallError unless the result is exactly the pinned wheel."""
    import hashlib
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "guardrails-engine-install"})
    digest = hashlib.sha256()
    got = 0
    try:
        with contextlib.closing(urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT)) as response:
            if not response.geturl().startswith("https://"):
                raise InstallError("the download was redirected away from https")
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                while True:
                    if time.monotonic() > deadline:
                        raise InstallError(f"the download took longer than {DOWNLOAD_BUDGET:.0f}s")
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > size:
                        raise InstallError("the download is larger than the manifest says")
                    digest.update(chunk)
                    out.write(chunk)
    except InstallError:
        raise
    except TimeoutError as exc:
        raise InstallError("the download timed out") from exc
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or getattr(exc, "strerror", None) or exc
        raise InstallError(f"the download failed: {sanitised(str(reason))}") from exc
    if got != size or digest.hexdigest() != sha256:
        raise InstallError("the download does not match the pinned sha256 (wrong size or content); it was discarded")


def unpack(wheel: str, member: str, dest: str, expected: str) -> None:
    """Write exactly one named member of the wheel to dest; nothing else in the archive is read."""
    import hashlib
    import zipfile

    try:
        with zipfile.ZipFile(wheel) as zf:
            matches = [i for i in zf.infolist() if i.filename == member]
            if len(matches) != 1:
                raise InstallError(f"the wheel has {len(matches)} entries named {member}, expected one")
            info = matches[0]
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or not BINARY_LIMITS[0] <= info.file_size \
                    <= BINARY_LIMITS[1]:
                raise InstallError(f"the wheel's {member} is not a regular file of a plausible size")
            digest = hashlib.sha256()
            written = 0
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out, zf.open(info) as source:
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    written += len(chunk)
                    if written > info.file_size:
                        raise InstallError("the wheel member is larger than it declares")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
    except zipfile.BadZipFile as exc:
        raise InstallError("the download is not a valid wheel") from exc
    if written != info.file_size or digest.hexdigest() != expected:
        raise InstallError("the unpacked binary does not match the manifest's sha256; it was discarded")


def write_marker(folder: str, plat: Platform, binary: str) -> None:
    info = os.lstat(binary)
    entry = manifest()["wheels"][plat.wheel]
    data = {"pin": pin(), "platform": plat.wheel, "wheelSha256": entry["sha256"], "binarySha256": entry["binarySha256"],
            "size": info.st_size, "mtimeNs": info.st_mtime_ns, "inode": info.st_ino,
            "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    import tempfile

    tmp = tempfile.mkstemp(dir=folder, prefix=".marker-")
    with os.fdopen(tmp[0], "w") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp[1], os.path.join(folder, MARKER))


def record_failure(state_dir: str, reason: str) -> None:
    import tempfile

    with contextlib.suppress(OSError):
        root = engine_root(state_dir)
        os.makedirs(root, mode=0o700, exist_ok=True)
        tmp = tempfile.mkstemp(dir=root, prefix=".marker-")
        with os.fdopen(tmp[0], "w") as fh:
            json.dump({"pin": pin(), "at": time.time(), "reason": sanitised(reason, 200)}, fh)
        os.replace(tmp[1], os.path.join(root, STAMP))


def install(state_dir: str, say: Callable[[str], None] = lambda _: None, respect_backoff: bool = False,
            budget: float = INSTALL_BUDGET) -> str:
    """Download, verify and place the wheel's binary; returns a one-line result, raises InstallError.

    Never replaces an install that still passes its runtime checks. A failure of any kind is stamped so the detached
    warm-up backs off for ten minutes; an explicit run (respect_backoff False) ignores the stamp.
    """
    plat = detect()
    if plat.wheel_problem:
        raise InstallError(plat.wheel_problem)
    if respect_backoff and backoff_left(state_dir) > 0:
        raise InstallError("an install failed within the last ten minutes")
    entry = manifest()["wheels"][plat.wheel]
    deadline = time.monotonic() + budget
    root = engine_root(state_dir)
    folder = install_dir(state_dir)
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        with locked(root, deadline):
            reap(root)
            found = wheel_probe(plat, state_dir)
            if found.path:
                return f"already installed: {found.path}"
            say(f"platform: {plat.label} (wheel {plat.wheel})")
            say(f"downloading {entry['filename']} ({entry['size'] / 1e6:.1f} MB) from files.pythonhosted.org")
            tmp_wheel = os.path.join(folder, f".dl-{os.getpid()}-{int(time.time())}.whl")
            tmp_binary = os.path.join(folder, f".bin-{os.getpid()}-{int(time.time())}")
            try:
                with watchdog.limit(DOWNLOAD_BUDGET):
                    fetch(entry["url"], tmp_wheel, entry["size"], entry["sha256"],
                          min(deadline, time.monotonic() + DOWNLOAD_BUDGET))
                say("sha256 of the wheel matches the pinned value")
                unpack(tmp_wheel, entry["member"], tmp_binary, entry["binarySha256"])
                os.chmod(tmp_binary, 0o755)
                say(f"self-test: {self_test(tmp_binary)}")
                os.replace(tmp_binary, os.path.join(folder, BINARY))
                write_marker(folder, plat, os.path.join(folder, BINARY))
            finally:
                for leftover in (tmp_wheel, tmp_binary):
                    with contextlib.suppress(OSError):
                        os.unlink(leftover)
    except TimeoutError:
        record_failure(state_dir, "timed out")
        raise InstallError("the download timed out") from None
    except Busy:
        raise
    except InstallError as exc:
        record_failure(state_dir, str(exc))
        raise
    except OSError as exc:
        record_failure(state_dir, exc.strerror or str(exc))
        raise InstallError(f"cannot write {folder}: {exc.strerror or exc}") from exc
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(root, STAMP))
    return f"installed {os.path.join(folder, BINARY)}"


def wanted(state_dir: str | None) -> bool:
    """True when neither source is usable, the platform is supported and no failure is in its backoff window."""
    try:
        plat, npm, wheel = probes(state_dir)
    except Missing:
        return False
    if npm.path or wheel.path or plat.wheel_problem or not state_dir:
        return False
    return backoff_left(state_dir) <= 0
