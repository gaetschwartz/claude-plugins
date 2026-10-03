"""The runtime bootstrap: stdlib only, so these tests also run under the oldest supported host interpreter (3.9)."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.error
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import bootstrap

STUB_UV = """#!/bin/sh
log="${0%/*}/uv.log"
echo "cwd=$PWD PATH=$PATH CACHE=$UV_CACHE_DIR INDEX=${UV_INDEX_URL-unset} PYPATH=${PYTHONPATH-unset} ARGS=$*" >> "$log"
for last; do :; done
case "$1 $2" in
  "python install") mkdir -p "$UV_PYTHON_INSTALL_DIR/cpython" "$UV_CACHE_DIR"; touch "$UV_CACHE_DIR/blob" ;;
  "venv --no-config")
    mkdir -p "$last/bin"
    reported=0.45.3; [ -f "${0%/*}/../../../wrong-version" ] && reported=0.1.0
    printf '#!/bin/sh\\necho 3.13.99 %s\\n' "$reported" > "$last/bin/python"
    chmod 755 "$last/bin/python" ;;
  "pip install")
    case "$*" in *"--require-hashes"*"--only-binary"*|*"--only-binary"*"--require-hashes"*) ;; *) exit 9 ;; esac
    while [ "$1" != "--python" ]; do shift; done
    site="${2%/bin/python}/lib/python3.13/site-packages/ast_grep_py"
    mkdir -p "$site"; touch "$site/ast_grep_py.cpython-313-stub.so"
    [ -f "${0%/*}/../../../fail-pip" ] && { echo "boom: no network" >&2; exit 1; } ;;
esac
exit 0
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wheel_bytes(script: str = STUB_UV) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("uv-9.9.9.data/scripts/uv", script)
        zf.writestr("uv-9.9.9.data/scripts/uvx", "other member")
    return buffer.getvalue()


class Clock:
    """Stands in for the time module inside bootstrap: wall time comes from a list the test moves."""

    def __init__(self, now: list[float]) -> None:
        self.now = now
        self.monotonic, self.sleep, self.strftime, self.localtime = (
            time.monotonic, time.sleep, time.strftime, time.localtime)

    def time(self) -> float:
        return self.now[0]


class Pinned(unittest.TestCase):
    """A temp data dir, project dir and a fake uv wheel the downloads are served from."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.data, self.proj = self.tmp / "data", self.tmp / "proj"
        self.proj.mkdir()
        self.wheel = wheel_bytes()
        self.pins = self.make_pins(self.wheel)
        self.served: list[str] = []
        self.clock = [1_000_000.0]
        for patcher in (mock.patch.object(bootstrap, "load_pins", lambda: self.pins),
                        mock.patch.object(bootstrap, "platform_key", lambda: "darwin-arm64"),
                        mock.patch("urllib.request.urlopen", self.urlopen),
                        mock.patch.object(bootstrap, "time", Clock(self.clock))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_pins(self, wheel: bytes) -> bootstrap.Pins:
        url = bootstrap.DOWNLOAD_PREFIX + "packages/ab/cd/uv-9.9.9-py3-none-macosx_11_0_arm64.whl"
        entry = bootstrap.Wheel(url, sha(wheel), len(wheel), "uv-9.9.9.data/scripts/uv")
        return bootstrap.Pins(1, "3.13", "0.45.3", "9.9.9", {"darwin-arm64": entry}, "r1-test")

    @contextlib.contextmanager
    def urlopen(self, url: str, timeout: float = 0) -> Iterator[io.BytesIO]:
        self.served.append(url)
        yield io.BytesIO(self.wheel)

    @property
    def rt(self) -> Path:
        return bootstrap.runtime_dir(self.data, self.pins)

    def ensure(self, wait: float = 120.0, retry_now: bool = False) -> bootstrap.Outcome:
        return bootstrap.ensure(self.data, wait=wait, retry_now=retry_now, project=self.proj, cwd=self.proj)

    def uv_log(self) -> str:
        return (self.rt / "bin" / "uv.log").read_text()


class Manifest(unittest.TestCase):
    def test_the_manifest_requirements_and_id_agree_with_the_generator_pins(self) -> None:
        spec = importlib.util.spec_from_file_location("gen", ROOT / "scripts" / "gen-runtime-manifest.py")
        assert spec is not None and spec.loader is not None
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        manifest = json.loads((ROOT / "lib" / "runtime-manifest.json").read_text())
        requirements = (ROOT / "lib" / "runtime-requirements.txt").read_text()
        document = (ROOT / "lib" / "runtime-manifest.json").read_text()
        wanted = f"r{gen.BOOTSTRAP_VERSION}-{sha((document + requirements).encode())[:12]}"
        self.assertEqual((manifest["python"], manifest["astGrepPy"], manifest["uv"]["version"],
                          manifest["bootstrapVersion"]), (gen.PYTHON, gen.AST_GREP_PY, gen.UV, gen.BOOTSTRAP_VERSION))
        self.assertEqual((ROOT / "lib" / "runtime-id").read_text().strip(), wanted)
        self.assertIn("ast-grep-py==" + gen.AST_GREP_PY, requirements)
        self.assertEqual(len(re.findall(r"--hash=sha256:[0-9a-f]{64}", requirements)), 4)
        self.assertEqual(set(manifest["uv"]["wheels"]), {"darwin-arm64", "darwin-x86_64", "linux-x86_64", "linux-aarch64"})
        for entry in manifest["uv"]["wheels"].values():
            self.assertTrue(entry["url"].startswith(bootstrap.DOWNLOAD_PREFIX))
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(entry["member"], f"uv-{gen.UV}.data/scripts/uv")
            self.assertIn(f"uv-{gen.UV}-", entry["url"])

    def test_the_loaded_pins_are_the_committed_ones(self) -> None:
        pins = bootstrap.load_pins()
        self.assertEqual((pins.python, pins.runtime_id), ("3.13", (ROOT / "lib" / "runtime-id").read_text().strip()))


class PlatformKey(unittest.TestCase):
    def test_supported_and_unsupported_platforms(self) -> None:
        cases = [
            ("Darwin", "arm64", None, "darwin-arm64"), ("Darwin", "x86_64", None, "darwin-x86_64"),
            ("Linux", "x86_64", "glibc 2.36", "linux-x86_64"), ("Linux", "aarch64", "glibc 2.28", "linux-aarch64"),
            ("Linux", "x86_64", "glibc 2.27", "older than glibc 2.28"), ("Linux", "x86_64", None, "musl"),
            ("Linux", "armv7l", "glibc 2.36", "unsupported platform Linux armv7l"),
            ("Windows", "AMD64", None, "unsupported platform Windows"),
            ("FreeBSD", "amd64", None, "unsupported platform FreeBSD"),
        ]
        for system, machine, libc, expected in cases:
            with self.subTest(system=system, machine=machine, libc=libc), \
                    mock.patch("platform.system", return_value=system), \
                    mock.patch("platform.machine", return_value=machine), \
                    mock.patch.object(bootstrap.os, "confstr", lambda name, libc=libc: libc, create=True):
                if expected.startswith(("darwin", "linux")):
                    self.assertEqual(bootstrap.platform_key(), expected)
                else:
                    with self.assertRaisesRegex(bootstrap.Unsupported, expected):
                        bootstrap.platform_key()


class Ready(Pinned):
    def install_fake(self) -> None:
        self.assertIn(self.ensure().state, ("installed", "ready"))

    def test_a_valid_marker_is_the_fast_path_and_touches_neither_network_nor_lock(self) -> None:
        self.install_fake()
        self.served.clear()
        with mock.patch.object(bootstrap, "install", side_effect=AssertionError("installed again")):
            self.assertEqual(self.ensure().state, "ready")
        self.assertEqual(self.served, [])

    def test_marker_problems_make_the_runtime_not_ready(self) -> None:
        python, marker = "venv/bin/python", "marker.json"

        def break_marker(edit: dict[str, object]) -> None:
            path = self.rt / marker
            document = json.loads(path.read_text())
            document.update(edit)
            path.write_text(json.dumps(document))

        cases = {
            "marker for other pins": lambda: break_marker({"runtimeId": "r9-other"}),
            "no files listed": lambda: break_marker({"files": {}}),
            "marker missing": lambda: (self.rt / marker).unlink(),
            "marker is not json": lambda: (self.rt / marker).write_text("{"),
            "python missing": lambda: (self.rt / python).unlink(),
            "python group-writable": lambda: (self.rt / python).chmod(0o775),
            "python grew": lambda: (self.rt / python).write_text("#!/bin/sh\necho changed and longer\n"),
        }
        for name, damage in cases.items():
            with self.subTest(name):
                self.install_fake()
                self.assertIsNone(bootstrap.marker_problem(self.rt, self.pins, self.proj, self.proj))
                damage()
                self.assertIsNotNone(bootstrap.marker_problem(self.rt, self.pins, self.proj, self.proj))
                self.ensure(retry_now=True)
                self.assertIsNone(bootstrap.marker_problem(self.rt, self.pins, self.proj, self.proj), "repaired")

    def test_a_runtime_inside_the_project_or_cwd_is_refused_not_installed(self) -> None:
        inside = self.proj / "data"
        for project, cwd in ((inside, None), (None, inside), (self.proj, None)):
            with self.subTest(project=project, cwd=cwd):
                outcome = bootstrap.ensure(inside, project=project, cwd=cwd)
                self.assertEqual(outcome.state, "failed")
                self.assertIn("inside the project", outcome.reason)
        self.assertFalse((inside / "runtime").exists())

    def test_a_symlinked_file_leaving_the_runtime_is_not_trusted(self) -> None:
        self.install_fake()
        link = self.rt / "venv" / "bin" / "python"
        outside = self.tmp / "elsewhere"
        outside.write_bytes(link.read_bytes())
        outside.chmod(0o755)
        mtime = link.stat().st_mtime_ns
        os.utime(outside, ns=(mtime, mtime))
        link.unlink()
        link.symlink_to(outside)
        self.assertIsNotNone(bootstrap.marker_problem(self.rt, self.pins, self.proj, self.proj))


class Install(Pinned):
    def test_the_install_runs_uv_from_the_runtime_with_a_scrubbed_environment(self) -> None:
        hostile = {"UV_CACHE_DIR": "/evil/cache", "UV_INDEX_URL": "http://evil.example/simple", "PYTHONPATH": "/evil",
                   "UV_PYTHON": "/evil/python", "PATH": str(self.tmp / "evilbin") + ":/usr/bin:/bin",
                   "UV_PYTHON_INSTALL_MIRROR": "http://evil.example", "HOME": str(self.tmp)}
        (self.tmp / "evilbin").mkdir()
        (self.tmp / "evilbin" / "uv").write_text("#!/bin/sh\ntouch " + str(self.tmp / "pwned") + "\n")
        (self.tmp / "evilbin" / "uv").chmod(0o755)
        (self.proj / "uv.toml").write_text('index-url = "http://evil.example/simple"\n')
        with mock.patch.dict(os.environ, hostile):
            outcome = self.ensure()
        self.assertEqual(outcome.state, "installed", outcome.reason)
        self.assertFalse((self.tmp / "pwned").exists())
        lines = self.uv_log().splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertIn(f"cwd={self.rt}", line)
            self.assertIn("PATH=/usr/bin:/bin ", line)
            self.assertIn(f"CACHE={self.rt}/uv-cache", line)
            self.assertIn("INDEX=unset PYPATH=unset", line)
        self.assertTrue(all("--no-config" in line for line in lines))
        self.assertTrue(lines[2].endswith(f"-r {ROOT / 'lib' / 'runtime-requirements.txt'}"))
        self.assertIn("--only-binary :all: --no-deps --require-hashes", lines[2])

    def test_a_successful_install_cleans_up_after_itself(self) -> None:
        old, stale = self.data / "runtime" / "r0-old", self.data / "runtime" / "r1-test.tmp-1"
        for path in (old, stale):
            path.mkdir(parents=True)
            (path / "file").write_text("x")
        (self.data / "runtime" / "failure.json").write_text(json.dumps({"at": 1.0, "reason": "old"}))
        self.assertEqual(self.ensure(retry_now=True).state, "installed")
        self.assertFalse(old.exists() or stale.exists())
        self.assertFalse((self.rt / "uv-cache").exists())
        self.assertFalse((self.rt / "uv.whl").exists())
        self.assertFalse((self.data / "runtime" / "failure.json").exists())
        marker = json.loads((self.rt / "marker.json").read_text())
        self.assertEqual((marker["uv"], marker["python"], marker["astGrepPy"], marker["platform"]),
                         ("9.9.9", "3.13.99", "0.45.3", "darwin-arm64"))
        self.assertEqual(oct((self.rt / "bin" / "uv").stat().st_mode & 0o777), "0o700")

    def test_a_failed_step_leaves_no_marker_and_a_later_install_recovers(self) -> None:
        self.data.mkdir(parents=True)
        (self.data / "fail-pip").write_text("")
        first = self.ensure()
        self.assertEqual(first.state, "failed")
        self.assertIn("boom: no network", first.reason)
        self.assertFalse((self.rt / "marker.json").exists())
        self.assertIsNotNone(bootstrap.read_failure(self.data))
        (self.data / "fail-pip").unlink()
        self.assertEqual(self.ensure().state, "backoff")
        self.assertEqual(self.ensure(retry_now=True).state, "installed")

    def test_the_hash_of_the_downloaded_wheel_is_checked_before_anything_is_read(self) -> None:
        self.wheel = wheel_bytes(STUB_UV + "# tampered\n")
        with mock.patch("zipfile.ZipFile", side_effect=AssertionError("read before verifying")):
            outcome = self.ensure()
        self.assertEqual(outcome.state, "failed")
        self.assertIn("does not match the pinned sha256", outcome.reason)
        self.assertFalse((self.rt / "bin" / "uv").exists())

    def test_downloads_only_come_from_the_pinned_host_and_stay_in_size(self) -> None:
        cases = {
            "other host": (self.pins.wheels["darwin-arm64"]._replace(url="https://evil.example/uv.whl"), "other than"),
            "too large": (self.pins.wheels["darwin-arm64"], "larger than expected"),
        }
        for name, (entry, why) in cases.items():
            limit = 100 if name == "too large" else 10**9
            with self.subTest(name), mock.patch.object(bootstrap, "UV_MAX_BYTES", limit), \
                    self.assertRaisesRegex(bootstrap.InstallError, why):
                bootstrap.download(entry, self.tmp / "w.whl", bootstrap.Deadline(30))

    def test_offline_fails_once_then_backs_off_until_the_wait_passes(self) -> None:
        def offline(url: str, timeout: float = 0) -> io.BytesIO:
            self.served.append(url)
            raise urllib.error.URLError("nodename nor servname provided")

        with mock.patch("urllib.request.urlopen", offline):
            first = self.ensure()
            self.assertEqual((first.state, first.retry_at), ("failed", self.clock[0] + bootstrap.BACKOFF_SECONDS))
            self.assertIn("cannot download uv", first.reason)
            self.clock[0] += 60
            again = self.ensure()
            self.assertEqual((again.state, again.retry_at), ("backoff", first.retry_at))
            self.assertEqual(len(self.served), 1)
            self.ensure(retry_now=True)
            self.assertEqual(len(self.served), 2)
            self.clock[0] += bootstrap.BACKOFF_SECONDS + 1
            self.ensure()
            self.assertEqual(len(self.served), 3)

    def test_a_timeout_is_a_failure_too(self) -> None:
        with mock.patch.object(bootstrap, "run_tool", side_effect=bootstrap.InstallError("uv venv timed out")):
            outcome = self.ensure()
        self.assertEqual((outcome.state, outcome.reason), ("failed", "uv venv timed out"))
        failure = bootstrap.read_failure(self.data)
        self.assertEqual(failure and failure.reason, "uv venv timed out")

    def test_the_installed_version_must_be_the_pinned_one(self) -> None:
        self.data.mkdir(parents=True)
        (self.data / "wrong-version").write_text("")
        outcome = self.ensure()
        self.assertEqual(outcome.state, "failed")
        self.assertIn("not the pinned version", outcome.reason)

    def test_an_unsupported_platform_is_reported_without_touching_anything(self) -> None:
        with mock.patch.object(bootstrap, "platform_key", side_effect=bootstrap.Unsupported("unsupported platform X")):
            self.assertEqual(self.ensure(), bootstrap.Outcome("unsupported", "unsupported platform X"))
        self.assertFalse(self.data.exists())

    def test_a_second_ensure_waits_for_the_lock_instead_of_installing_twice(self) -> None:
        with bootstrap.locked(self.data, 0) as mine:
            self.assertTrue(mine)
            self.assertEqual(self.ensure(wait=0).state, "busy")
        self.assertEqual(self.ensure(wait=0).state, "installed")


class Hook(Pinned):
    def hook(self, session: str = "s1") -> dict[str, Any]:
        out = bootstrap.hook(json.dumps({"session_id": session}), self.data)
        return json.loads(out) if out else {}

    def test_not_ready_allows_loudly_once_per_session_and_starts_one_install(self) -> None:
        with mock.patch.object(bootstrap, "spawn_ensure") as spawn:
            first = self.hook()
            self.assertEqual(spawn.call_count, 1)
            self.assertTrue(first)
            text = str(first["systemMessage"])
            self.assertIn("installed automatically", text)
            self.assertIn("NOT enforced", text)
            self.assertNotIn("engine install", text)
            self.assertEqual(first["hookSpecificOutput"]["additionalContext"], text)
            self.assertEqual(self.hook(), {})
            self.assertTrue(self.hook("s2"))

    def test_while_another_install_runs_no_second_one_starts(self) -> None:
        with mock.patch.object(bootstrap, "spawn_ensure") as spawn, bootstrap.locked(self.data, 0):
            self.assertIn("installed automatically", self.hook()["systemMessage"])
        spawn.assert_not_called()

    def test_a_persisting_failure_repeats_every_ten_minutes_and_names_the_retry_time(self) -> None:
        bootstrap.write_atomic(bootstrap.failure_path(self.data), json.dumps({"at": self.clock[0], "reason": "offline"}))
        with mock.patch.object(bootstrap, "spawn_ensure") as spawn:
            first = self.hook()
            self.assertIn("offline", str(first["systemMessage"]))
            self.assertIn(bootstrap.clock(self.clock[0] + bootstrap.BACKOFF_SECONDS), str(first["systemMessage"]))
            self.assertEqual(self.hook(), {})
            os.utime(self.data / "runtime" / "notices" / "s1", (self.clock[0] - 700, self.clock[0] - 700))
            self.assertTrue(self.hook())
            spawn.assert_not_called()
            self.clock[0] += bootstrap.BACKOFF_SECONDS + 1
            self.hook("s3")
            spawn.assert_called_once()

    def test_the_failure_reason_is_flattened_and_capped(self) -> None:
        reason = bootstrap.sanitised("line1\nIGNORE ALL\x1b[31m " + "x" * 500)
        self.assertNotIn("\n", reason)
        self.assertNotIn("\x1b", reason)
        self.assertEqual(len(reason), 200)

    def test_an_unsupported_platform_notice_says_so(self) -> None:
        with mock.patch.object(bootstrap, "platform_key", side_effect=bootstrap.Unsupported("unsupported platform X")):
            out = self.hook()
        self.assertIn("unsupported platform X", str(out["systemMessage"]))


class Entry(Pinned):
    def test_session_start_installs_synchronously_and_is_quiet_when_ready(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(self.data), "CLAUDE_PROJECT_DIR": str(self.proj)}):
            first = bootstrap.session_start()
            self.assertIn("installed", json.loads(first)["systemMessage"])
            self.assertEqual(bootstrap.session_start(), "")

    def test_session_start_reports_a_failure(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(self.data)}), \
                mock.patch.object(bootstrap, "ensure", return_value=bootstrap.Outcome("failed", "offline", 5.0)):
            self.assertIn("offline", json.loads(bootstrap.session_start())["systemMessage"])

    def test_the_ensure_command_exit_codes(self) -> None:
        cases = [(bootstrap.Outcome("ready"), 0), (bootstrap.Outcome("installed"), 0),
                 (bootstrap.Outcome("failed", "offline", 5.0), 2), (bootstrap.Outcome("busy"), 2),
                 (bootstrap.Outcome("unsupported", "unsupported platform X"), 2)]
        for outcome, code in cases:
            with self.subTest(outcome=outcome), mock.patch.object(bootstrap, "ensure", return_value=outcome), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(bootstrap.main(["ensure"]), code)
                self.assertEqual(bool(err.getvalue()), code != 0 or outcome.state == "installed")

    def test_run_executes_the_guard_with_the_runtime_python_once_ready(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(self.data)}), \
                mock.patch.object(bootstrap, "ensure", return_value=bootstrap.Outcome("ready")), \
                mock.patch.object(bootstrap.os, "execv") as execv:
            bootstrap.main(["run", "status", "--problems"])
        python = str(self.rt / "venv" / "bin" / "python")
        execv.assert_called_once_with(python, [python, "-I", str(ROOT / "lib" / "guard.py"), "status", "--problems"])


if __name__ == "__main__":
    unittest.main()
