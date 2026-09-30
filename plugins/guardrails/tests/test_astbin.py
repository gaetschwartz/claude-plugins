from __future__ import annotations  # noqa: I001

import copy
import hashlib
import io
import json
import os
import stat
import time
import unittest
import warnings
import zipfile
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from helpers import LIB, ROOT, AstIsolated, Isolated

import astbin
import astcli
import astrun

STUB = "#!/bin/sh\necho 'ast-grep {pin}'\n"
PLATFORMS = [
    ("Darwin", "arm64", ("unknown", ""), "darwin-arm64", "darwin-universal2"),
    ("Darwin", "x86_64", ("unknown", ""), "darwin-x64", "darwin-universal2"),
    ("Linux", "x86_64", ("glibc", "2.35"), "linux-x64-gnu", "linux-x86_64-gnu"),
    ("Linux", "aarch64", ("glibc", "2.28"), "linux-arm64-gnu", "linux-aarch64-gnu"),
]


def uname(system: str, machine: str) -> Any:
    return mock.Mock(sysname=system, machine=machine)


class Pins(unittest.TestCase):
    """One version everywhere: package.json, package-lock.json, the manifest, and no second lockfile."""

    def test_the_same_version_is_pinned_in_every_place(self) -> None:
        pin = json.loads((ROOT / "package.json").read_text())
        self.assertTrue(pin["private"])
        self.assertNotIn("scripts", pin)
        version = pin["dependencies"]["@ast-grep/cli"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(astbin.pin(), version)
        lock = json.loads((ROOT / "package-lock.json").read_text())["packages"]
        self.assertEqual(lock["node_modules/@ast-grep/cli"]["version"], version)
        for platform in ("darwin-arm64", "darwin-x64", "linux-x64-gnu", "linux-arm64-gnu"):
            entry = lock[f"node_modules/@ast-grep/cli-{platform}"]
            self.assertEqual(entry["version"], version)
            self.assertTrue(entry["integrity"].startswith("sha512-"))
            self.assertEqual(astbin.manifest()["npm"][platform]["package"], f"@ast-grep/cli-{platform}")
        for name in ("bun.lock", "bun.lockb", "yarn.lock", "pnpm-lock.yaml"):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertIn("node_modules/", (ROOT / ".gitignore").read_text())

    def test_the_manifest_lists_every_supported_wheel_with_hashes(self) -> None:
        wheels = astbin.manifest()["wheels"]
        self.assertEqual(sorted(wheels), ["darwin-universal2", "linux-aarch64-gnu", "linux-x86_64-gnu"])
        for key, entry in wheels.items():
            with self.subTest(wheel=key):
                self.assertTrue(entry["url"].startswith("https://files.pythonhosted.org/"))
                self.assertIn(f"ast_grep_cli-{astbin.pin()}-py3-none-", entry["filename"])
                self.assertTrue(entry["url"].endswith(entry["filename"]))
                self.assertEqual(entry["member"], f"ast_grep_cli-{astbin.pin()}.data/scripts/ast-grep")
                for field in ("sha256", "binarySha256"):
                    self.assertRegex(entry[field], r"^[0-9a-f]{64}$")
        for entry in astbin.manifest()["npm"].values():
            self.assertRegex(entry["binarySha256"], r"^[0-9a-f]{64}$")

    def test_a_manifest_entry_off_the_allowed_host_is_refused(self) -> None:
        bad = copy.deepcopy(json.loads((LIB / "engine-manifest.json").read_text()))
        bad["wheels"]["darwin-universal2"]["url"] = "https://evil.example/x.whl"
        path = LIB.parent / "tests" / "__bad-manifest.json"
        path.write_text(json.dumps(bad))
        self.addCleanup(path.unlink)
        with mock.patch.object(astbin, "MANIFEST_PATH", str(path)), mock.patch.object(astbin, "_MANIFEST", []), \
                self.assertRaises(ValueError):
            astbin.manifest()


class PlatformDetection(unittest.TestCase):
    def test_supported_platforms_map_to_their_packages_and_wheels(self) -> None:
        for system, machine, libc, npm, wheel in PLATFORMS:
            with self.subTest(system=system, machine=machine), mock.patch.object(astbin.os, "uname",
                                                                                  return_value=uname(system, machine)), \
                    mock.patch.object(astbin, "libc", return_value=libc):
                plat = astbin.detect()
                self.assertEqual((plat.npm, plat.wheel), (npm, wheel))
                self.assertIn(plat.npm, astbin.manifest()["npm"])
                self.assertIn(plat.wheel, astbin.manifest()["wheels"])

    def test_musl_unknown_arch_and_other_systems_are_unsupported(self) -> None:
        for system, machine, libc in (("Linux", "x86_64", ("musl", "")), ("Linux", "aarch64", ("unknown", "")),
                                      ("Linux", "riscv64", ("glibc", "2.39")), ("FreeBSD", "amd64", ("unknown", "")),
                                      ("Darwin", "ppc", ("unknown", ""))):
            with self.subTest(system=system, machine=machine, libc=libc[0]), \
                    mock.patch.object(astbin.os, "uname", return_value=uname(system, machine)), \
                    mock.patch.object(astbin, "libc", return_value=libc), self.assertRaises(astbin.Missing) as ctx:
                astbin.detect()
            self.assertIn("unsupported platform", str(ctx.exception))

    def test_an_old_glibc_keeps_npm_but_not_the_wheel(self) -> None:
        with mock.patch.object(astbin.os, "uname", return_value=uname("Linux", "x86_64")), \
                mock.patch.object(astbin, "libc", return_value=("glibc", "2.17")):
            plat = astbin.detect()
        self.assertIn("older than the 2.28", plat.wheel_problem or "")


class World(Isolated):
    """A fake engine release: a wheel holding a stub script, with a manifest that describes it."""

    def setUp(self) -> None:
        super().setUp()
        self.plat = astbin.detect()
        self.stub = STUB.format(pin=astbin.pin()).encode()
        self.wheel = self.build_wheel(self.stub)
        manifest = copy.deepcopy(astbin.manifest())
        manifest["wheels"][self.plat.wheel].update(
            sha256=hashlib.sha256(self.wheel).hexdigest(), size=len(self.wheel), binarySha256=hashlib.sha256(self.stub).hexdigest())
        manifest["npm"][self.plat.npm]["binarySha256"] = hashlib.sha256(self.stub).hexdigest()
        for patch in (mock.patch.object(astbin, "_MANIFEST", [manifest]),
                      mock.patch.object(astbin, "BINARY_LIMITS", (1, astbin.BINARY_LIMITS[1]))):
            patch.start()
            self.addCleanup(patch.stop)
        self.entry = manifest["wheels"][self.plat.wheel]
        self.fetched: list[str] = []
        self.serve(self.wheel)
        self.state = str(self.data)

    def build_wheel(self, binary: bytes, member: str | None = None, extra: dict[str, bytes] | None = None,
                    mode: int = 0o100755) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            info = zipfile.ZipInfo(member or self.entry_member())
            info.external_attr = mode << 16
            zf.writestr(info, binary)
            for name, data in (extra or {}).items():
                zf.writestr(name, data)
        return buffer.getvalue()

    @staticmethod
    def entry_member() -> str:
        return f"ast_grep_cli-{astbin.pin()}.data/scripts/ast-grep"

    def serve(self, wheel: bytes, size: int | None = None) -> None:
        def fake(url: str, dest: str, _size: int, _sha: str, deadline: float) -> None:
            self.fetched.append(url)
            Path(dest).write_bytes(wheel)
            if hashlib.sha256(wheel).hexdigest() != self.entry["sha256"] or len(wheel) != self.entry["size"]:
                raise astbin.InstallError("the download does not match the pinned sha256 (wrong size or content); it "
                                          "was discarded")

        patch = mock.patch.object(astbin, "fetch", fake)
        patch.start()
        self.addCleanup(patch.stop)

    def folder(self) -> Path:
        return Path(astbin.install_dir(self.state))

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.folder().iterdir() if p.name.startswith((".dl-", ".bin-", ".marker-")))


class Install(World):
    def test_a_good_install_places_the_binary_and_writes_the_marker_last(self) -> None:
        said: list[str] = []
        result = astbin.install(self.state, said.append)
        binary = self.folder() / "ast-grep"
        self.assertIn(str(binary), result)
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)
        self.assertEqual(self.leftovers(), [])
        marker = json.loads((self.folder() / astbin.MARKER).read_text())
        info = binary.stat()
        self.assertEqual({k: marker[k] for k in ("pin", "wheelSha256", "binarySha256", "size", "mtimeNs", "inode")},
                         {"pin": astbin.pin(), "wheelSha256": self.entry["sha256"],
                          "binarySha256": self.entry["binarySha256"], "size": info.st_size,
                          "mtimeNs": info.st_mtime_ns, "inode": info.st_ino})
        self.assertTrue(any("self-test" in line for line in said))
        self.assertEqual(self.fetched, [self.entry["url"]])
        engine = astbin.locate(self.state)
        self.assertEqual((engine.how, engine.binary), ("wheel", str(binary)))

    def test_a_valid_install_is_never_replaced(self) -> None:
        astbin.install(self.state)
        before = (self.folder() / "ast-grep").stat().st_ino
        self.assertIn("already installed", astbin.install(self.state))
        self.assertEqual(len(self.fetched), 1)
        self.assertEqual((self.folder() / "ast-grep").stat().st_ino, before)

    def test_every_failure_is_stamped_and_the_warm_up_backs_off_but_an_explicit_install_retries(self) -> None:
        def broken(*_: Any) -> None:
            raise astbin.InstallError("the download timed out")

        with mock.patch.object(astbin, "fetch", broken), self.assertRaises(astbin.InstallError):
            astbin.install(self.state)
        stamp = astbin.failure(self.state)
        self.assertEqual(stamp[0] if stamp else None, "the download timed out")
        self.assertGreater(astbin.backoff_left(self.state), 590)
        self.assertFalse(astbin.wanted(self.state))
        with self.assertRaisesRegex(astbin.InstallError, "within the last ten minutes"):
            astbin.install(self.state, respect_backoff=True)
        self.assertEqual(self.fetched, [])
        astbin.install(self.state)
        self.assertIsNone(astbin.failure(self.state))
        self.assertEqual(self.leftovers(), [])

    def test_an_old_stamp_expires(self) -> None:
        astbin.record_failure(self.state, "offline")
        with mock.patch.object(astbin.time, "time", return_value=time.time() + 601):
            self.assertEqual(astbin.backoff_left(self.state), 0.0)
            self.assertTrue(astbin.wanted(self.state))

    def test_the_wheel_is_unpacked_only_as_the_one_expected_member(self) -> None:
        member = self.entry_member()
        link = stat.S_IFLNK | 0o777
        cases = {
            "a symlink member": self.build_wheel(self.stub, mode=link),
            "a different member name": self.build_wheel(self.stub, member="../../evil/ast-grep"),
            "an absolute member name": self.build_wheel(self.stub, member="/tmp/ast-grep"),
            "no member at all": self.build_wheel(self.stub, member="other/file"),
            "a duplicate member": self.duplicated(member),
            "not a zip": b"PK-not-really",
            "a binary of the wrong hash": self.build_wheel(b"#!/bin/sh\necho evil\n"),
        }
        for label, wheel in cases.items():
            with self.subTest(case=label):
                folder = self.tmp / "unpack"
                folder.mkdir(exist_ok=True)
                source = folder / "w.whl"
                source.write_bytes(wheel)
                dest = folder / "out"
                with self.assertRaises(astbin.InstallError):
                    astbin.unpack(str(source), member, str(dest), self.entry["binarySha256"])
                self.assertFalse(dest.exists() and dest.stat().st_size > 0 and label != "a binary of the wrong hash")

    def duplicated(self, member: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            zf.writestr(member, self.stub)
            zf.writestr(member, self.stub + b" ")
        return buffer.getvalue()

    def test_a_binary_that_does_not_run_or_reports_another_version_is_discarded(self) -> None:
        for script in (b"#!/bin/sh\nexit 3\n", b"#!/bin/sh\necho 'ast-grep 9.9.9'\n"):
            with self.subTest(script=script):
                wheel = self.build_wheel(script)
                self.entry.update(sha256=hashlib.sha256(wheel).hexdigest(), size=len(wheel),
                                  binarySha256=hashlib.sha256(script).hexdigest())
                self.serve(wheel)
                with self.assertRaises(astbin.InstallError):
                    astbin.install(self.state)
                self.assertFalse((self.folder() / "ast-grep").exists())
                self.assertEqual(self.leftovers(), [])

    def test_the_lock_is_honoured_and_a_busy_install_is_not_a_failure(self) -> None:
        import fcntl

        root = Path(astbin.engine_root(self.state))
        root.mkdir(parents=True)
        fd = os.open(root / astbin.LOCK, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(astbin.Busy):
                astbin.install(self.state, budget=0.3)
        finally:
            os.close(fd)
        self.assertEqual(self.fetched, [])
        self.assertIsNone(astbin.failure(self.state))

    def test_stale_temporary_files_are_reaped_and_fresh_ones_kept(self) -> None:
        root = Path(astbin.engine_root(self.state))
        self.folder().mkdir(parents=True)
        old, new = self.folder() / ".dl-old.whl", self.folder() / ".bin-new"
        keep = root / "unrelated"
        for path in (old, new, keep):
            path.write_text("x")
        past = time.time() - 3600
        os.utime(old, (past, past))
        os.utime(keep, (past, past))
        astbin.install(self.state)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists() and keep.exists())


class RuntimeChecks(World):
    def setUp(self) -> None:
        super().setUp()
        astbin.install(self.state)
        self.binary = self.folder() / "ast-grep"

    def why(self) -> str | None:
        return astbin.wheel_probe(self.plat, self.state).why

    def test_the_cheap_check_passes_then_notices_each_kind_of_change(self) -> None:
        self.assertIsNone(self.why())
        marker = self.folder() / astbin.MARKER
        original = marker.read_text()
        data = json.loads(original)
        for key, value in (("pin", "0.0.1"), ("wheelSha256", "0" * 64), ("binarySha256", "1" * 64), ("size", 1),
                           ("mtimeNs", 1), ("inode", 1)):
            with self.subTest(changed=key):
                marker.write_text(json.dumps({**data, key: value}))
                self.assertTrue(self.why())
        marker.write_text("not json")
        self.assertIn("marker", self.why() or "")
        marker.unlink()
        self.assertIn("marker", self.why() or "")
        marker.write_text(original)
        self.assertIsNone(self.why())

    def test_a_rewritten_or_swapped_binary_is_rejected(self) -> None:
        os.utime(self.binary, (1, 1))
        self.assertIn("changed after", self.why() or "")
        with self.assertRaises(astbin.Missing) as ctx:
            astbin.locate(self.state)
        self.assertIn("downloaded engine rejected", str(ctx.exception))
        self.assertTrue(any("ignored the downloaded binary" in r for r in astbin.take_rejected()))

    def test_permissions_and_directories_are_checked(self) -> None:
        self.binary.chmod(0o775)
        self.assertIn("writable by group or others", self.why() or "")
        self.binary.chmod(0o755)
        self.folder().chmod(0o777)
        self.assertIn("writable by everyone", self.why() or "")
        self.folder().chmod(0o700)
        self.assertIsNone(self.why())

    def test_a_symlinked_binary_is_rejected(self) -> None:
        real = self.folder() / "real"
        os.rename(self.binary, real)
        self.binary.symlink_to(real)
        self.assertIn("symlink", self.why() or "")

    def test_verify_rehashes_against_the_manifest(self) -> None:
        self.assertTrue(all(ok for ok, _ in astbin.verify(self.state)))
        content = self.binary.read_bytes()
        stat_before = self.binary.stat()
        self.binary.write_bytes(content.replace(b"echo", b"ecHo"))
        os.utime(self.binary, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        results = astbin.verify(self.state)
        self.assertFalse(results[0][0])
        self.assertIn("DIFFERS", results[0][1])

    def test_status_reports_the_active_mechanism(self) -> None:
        info = astbin.status(self.state)
        self.assertEqual((info["active"], info["pin"]), ("wheel", astbin.pin()))
        self.assertTrue(info["wheel"]["usable"] and not info["npm"]["present"])


class Fetch(Isolated):
    """The download itself, against a stubbed urlopen."""

    def response(self, body: bytes, url: str = "https://files.pythonhosted.org/x.whl") -> Any:
        class Response(io.BytesIO):
            def geturl(self) -> str:
                return url

        return Response(body)

    def fetch(self, body: bytes, size: int | None = None, sha: str | None = None, url: str | None = None) -> Path:
        dest = self.tmp / "dl.whl"
        with mock.patch("urllib.request.urlopen", return_value=self.response(body, url or
                                                                              "https://files.pythonhosted.org/x.whl")):
            astbin.fetch("https://files.pythonhosted.org/x.whl", str(dest), len(body) if size is None else size,
                         sha or hashlib.sha256(body).hexdigest(), time.monotonic() + 30)
        return dest

    def test_a_matching_download_is_written_mode_0600(self) -> None:
        dest = self.fetch(b"abc" * 1000)
        self.assertEqual(dest.read_bytes(), b"abc" * 1000)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)

    def test_wrong_content_size_redirects_and_timeouts_fail_closed(self) -> None:
        body = b"abc" * 1000
        cases: dict[str, Any] = {
            "wrong sha": lambda: self.fetch(body, sha="0" * 64),
            "too long": lambda: self.fetch(body, size=10),
            "too short": lambda: self.fetch(body, size=len(body) + 5),
            "plain http after a redirect": lambda: self.fetch(body, url="http://files.pythonhosted.org/x.whl"),
        }
        for label, run in cases.items():
            with self.subTest(case=label), self.assertRaises(astbin.InstallError):
                self.tmp.joinpath("dl.whl").unlink() if self.tmp.joinpath("dl.whl").exists() else None
                run()
        for error in (TimeoutError("slow"), OSError("refused"), ValueError("bad url")):
            with self.subTest(error=error), mock.patch("urllib.request.urlopen", side_effect=error), \
                    self.assertRaises(astbin.InstallError):
                self.tmp.joinpath("dl.whl").unlink() if self.tmp.joinpath("dl.whl").exists() else None
                astbin.fetch("https://files.pythonhosted.org/x.whl", str(self.tmp / "dl.whl"), 1, "0" * 64,
                             time.monotonic() + 5)

    def test_an_exhausted_total_budget_stops_the_download(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=self.response(b"x" * 100)), \
                self.assertRaisesRegex(astbin.InstallError, "took longer"):
            astbin.fetch("https://files.pythonhosted.org/x.whl", str(self.tmp / "late.whl"), 100, "0" * 64,
                         time.monotonic() - 1)


class NpmPath(World):
    """The platform package Claude Code's npm ci installs, simulated with a stub binary."""

    def setUp(self) -> None:
        super().setUp()
        self.folder_npm = self.plugin_root / "node_modules" / "@ast-grep" / f"cli-{self.plat.npm}"

    def put_npm(self, version: str | None = None, script: bytes | None = None) -> Path:
        self.folder_npm.mkdir(parents=True, exist_ok=True)
        binary = self.folder_npm / "ast-grep"
        binary.write_bytes(script or self.stub)
        binary.chmod(0o755)
        (self.folder_npm / "package.json").write_text(json.dumps({"name": f"@ast-grep/cli-{self.plat.npm}",
                                                                  "version": version or astbin.pin()}))
        return binary

    def test_a_valid_npm_binary_wins_over_the_wheel_and_nothing_is_downloaded(self) -> None:
        binary = self.put_npm()
        astbin.install(self.state)
        self.fetched.clear()
        engine = astbin.locate(self.state)
        self.assertEqual((engine.how, engine.binary), ("npm", str(binary)))
        self.assertEqual(self.fetched, [])
        self.assertFalse(astbin.wanted(self.state))

    def test_the_wheel_is_used_when_npm_is_absent_or_rejected(self) -> None:
        astbin.install(self.state)
        self.assertEqual(astbin.locate(self.state).how, "wheel")
        binary = self.put_npm(version="0.0.1")
        self.assertEqual(astbin.locate(self.state).how, "wheel")
        self.assertIn("version 0.0.1", astbin.npm_probe(self.plat).why or "")
        binary.chmod(0o777)
        self.assertIn("writable by group or others", astbin.npm_probe(self.plat).why or "")

    def test_reasons_when_nothing_is_usable(self) -> None:
        with self.assertRaises(astbin.Missing) as ctx:
            astbin.locate(self.state)
        self.assertIn("npm install missing", str(ctx.exception))
        self.assertIn("has not been downloaded", str(ctx.exception))
        astbin.record_failure(self.state, "the download failed: connection refused")
        with self.assertRaises(astbin.Missing) as ctx:
            astbin.locate(self.state)
        self.assertRegex(str(ctx.exception), r"download failed: the download failed: connection refused \(retry after \d\d:\d\d\)")
        self.put_npm(version="0.0.1")
        with self.assertRaises(astbin.Missing) as ctx:
            astbin.locate(self.state)
        self.assertIn("npm install rejected", str(ctx.exception))

    def test_verify_uses_the_lockfile_record_for_npm(self) -> None:
        self.put_npm()
        lock = {"packages": {f"node_modules/@ast-grep/cli-{self.plat.npm}": {"integrity": "sha512-abc"}}}
        (self.plugin_root / "package-lock.json").write_text(json.dumps(lock))
        (self.plugin_root / "node_modules" / ".package-lock.json").write_text(json.dumps(lock))
        self.assertTrue(all(ok for ok, _ in astbin.verify(self.state)))
        lock["packages"][f"node_modules/@ast-grep/cli-{self.plat.npm}"]["integrity"] = "sha512-other"
        (self.plugin_root / "node_modules" / ".package-lock.json").write_text(json.dumps(lock))
        self.assertFalse(all(ok for ok, _ in astbin.verify(self.state)))


class EngineCli(World):
    def test_install_status_and_verify_need_no_as_user_and_work_for_agents(self) -> None:
        code, out, err = self.cli("engine", "status", agent=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("active: none", out)
        self.assertIn("guardrails engine install", out)
        self.assertIn("npm ci --ignore-scripts", out)
        code, out, err = self.cli("engine", "install", agent=True)
        self.assertEqual(code, 0, err)
        self.assertIn("installed", out)
        self.assertIn("self-test: ast-grep", out)
        code, out, _ = self.cli("engine", "status")
        self.assertIn(f"active: wheel ({self.folder() / 'ast-grep'})", out)
        self.assertNotIn("guardrails engine install", out)
        code, out, _ = self.cli("engine", "verify")
        self.assertEqual(code, 0)
        self.assertIn("ok: wheel binary", out)

    def test_a_failed_install_exits_2_and_status_shows_the_backoff(self) -> None:
        with mock.patch.object(astbin, "fetch", side_effect=astbin.InstallError("the download failed: offline")):
            code, _, err = self.cli("engine", "install")
        self.assertEqual(code, 2)
        self.assertIn("engine install failed: the download failed: offline", err)
        out = self.cli("engine", "status")[1]
        self.assertRegex(out, r"last download failure: the download failed: offline \(retry after \d\d:\d\d;")

    def test_an_unsupported_platform_exits_2_with_the_platform_matrix(self) -> None:
        with mock.patch.object(astbin, "libc", return_value=("musl", "")), \
                mock.patch.object(astbin.os, "uname", return_value=uname("Linux", "x86_64")):
            code, _, err = self.cli("engine", "install")
            status = self.cli("engine", "status")[1]
        self.assertEqual(code, 2)
        self.assertIn("unsupported platform", err)
        self.assertIn("macOS arm64/x86_64 and Linux glibc x86_64/aarch64", status)

    def test_verify_fails_with_exit_2_when_there_is_no_engine(self) -> None:
        code, out, _ = self.cli("engine", "verify")
        self.assertEqual(code, 2)
        self.assertIn("FAIL: no engine to verify", out)


class RealEngine(AstIsolated):
    def test_the_installed_binary_is_the_pin_and_verifies(self) -> None:
        self.assertEqual(self.call({"op": "ping"})["version"], astbin.pin())
        code, out, _ = self.cli("engine", "verify")
        self.assertEqual(code, 0, out)
        self.assertIn("self-test: ast-grep " + astbin.pin(), out)

    def test_a_planted_sgconfig_in_the_working_directory_or_its_parents_is_never_read(self) -> None:
        nested = self.tmp / "a" / "b"
        nested.mkdir(parents=True)
        (self.tmp / "a" / "sgconfig.yml").write_text("customLanguages:\n  evil:\n    libraryPath: /nonexistent/evil.dylib\n"
                                                     "    extensions: [zz]\n    expandoChar: x\n")
        (nested / "sgconfig.yml").write_text("ruleDirs: [does-not-exist]\n")
        previous = os.getcwd()
        os.chdir(nested)
        self.addCleanup(os.chdir, previous)
        request = {"op": "eval", "command": "sudo pkill x", "rules": {"r": {"pattern": "pkill $$$"}},
                   "wrappers": {"sudo": {}}}
        self.assertEqual(self.call(request)["verdicts"], {"r": "wrapped"})

    def test_the_child_gets_a_minimal_environment_and_a_neutral_directory(self) -> None:
        seen: list[dict[str, Any]] = []
        real = astcli.subprocess.run

        def spy(cmd: list[str], **kwargs: Any) -> Any:
            seen.append({"cmd": cmd, **kwargs})
            return real(cmd, **kwargs)

        hostile = {"UV_CACHE_DIR": "/evil", "NODE_OPTIONS": "--require /evil", "PYTHONPATH": "/evil",
                   "HOME": "/evil", "LD_PRELOAD": "/evil", "DYLD_INSERT_LIBRARIES": "/evil"}
        with mock.patch.dict(os.environ, hostile), mock.patch.object(astcli.subprocess, "run", spy):
            self.call({"op": "eval", "command": "pkill x", "rules": {"r": {"pattern": "pkill $$$"}}})
        self.assertTrue(seen)
        for call in seen:
            self.assertEqual(call["env"], {"PATH": "/usr/bin:/bin"})
            self.assertEqual(call["cwd"], os.sep)
            self.assertIn(astcli.CONFIG, call["cmd"])
            self.assertLess(call["timeout"], astrun.DEADLINE + 0.1)
        self.assertEqual(Path(astcli.CONFIG).read_text().strip(), "ruleDirs: []")

    def test_a_dead_binary_degrades_instead_of_raising(self) -> None:
        with mock.patch.object(astcli.Cli, "_run", side_effect=astcli.Unavailable("timed out")), \
                self.assertRaises(astrun.Unavailable):
            self.call({"op": "eval", "command": "pkill x", "rules": {"r": {"pattern": "pkill $$$"}}})


class BrokenBinary(World):
    """An installed binary that misbehaves degrades the rules by name; it is not reported as missing."""

    def use(self, script: str) -> None:
        stub = script.encode()
        wheel = self.build_wheel(stub)
        self.entry.update(sha256=hashlib.sha256(wheel).hexdigest(), size=len(wheel),
                          binarySha256=hashlib.sha256(stub).hexdigest())
        self.serve(wheel)
        with mock.patch.object(astbin, "self_test", return_value="stub"):
            astbin.install(self.state)
        self.put(self.gpath, {"rules": {"k": {"match": {"ast": {"pattern": "pkill $$$"}}, "message": "No pkill."}}})

    def test_garbage_failures_and_hangs_degrade_with_the_usual_note(self) -> None:
        for label, script in (("garbage", "#!/bin/sh\necho garbage\n"),
                              ("failure", "#!/bin/sh\necho 'Error: boom' >&2\nexit 7\n"),
                              ("not json", "#!/bin/sh\necho '[{\"file\": 1}]'\n")):
            with self.subTest(case=label):
                self.use(script)
                out = self.hook("pkill x", session=label)
                assert out is not None
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("the AST matcher is unavailable", out["systemMessage"])
                self.assertNotIn("ENGINE MISSING", out["systemMessage"])
                self.assertIn("this rule applied because the command mentions", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_hung_binary_is_cut_off_at_the_deadline(self) -> None:
        self.use("#!/bin/sh\nsleep 30\n")
        started = time.monotonic()
        with mock.patch.object(astrun, "DEADLINE", 0.5):
            out = self.hook("pkill x")
        self.assertLess(time.monotonic() - started, 5)
        assert out is not None
        self.assertIn("timed out", out["systemMessage"])


class EngineMissingNotice(Isolated):
    """The loud warning when neither the npm binary nor the wheel binary is usable."""

    RULES: ClassVar[dict[str, Any]] = {"rules": {"k": {"match": {"ast": {"pattern": "pkill $$$"}}, "message": "No pkill."}}}

    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, self.RULES)
        patch = mock.patch.object(astrun, "warm")
        patch.start()
        self.addCleanup(patch.stop)

    def channels(self, out: dict[str, Any] | None) -> tuple[str, str]:
        assert out is not None
        hook = out["hookSpecificOutput"]
        return out.get("systemMessage", ""), hook.get("additionalContext", "") + hook.get("permissionDecisionReason", "")

    def test_both_channels_carry_the_reason_and_both_fix_commands(self) -> None:
        user, agent = self.channels(self.hook("ls"))
        for text in (user, agent):
            self.assertIn("GUARDRAILS ENGINE MISSING", text)
            self.assertIn("NOT being enforced", text)
            self.assertIn("You MUST tell the user", text)
            self.assertIn("Reason: npm install missing", text)
            self.assertIn("`guardrails engine install`", text)
            self.assertIn(f"`{astbin.npm_fix()}`", text)
            self.assertIn("`guardrails engine status`", text)
            self.assertIn("degraded mode", text)

    def test_the_agent_text_is_framed_as_guardrails_own_notice(self) -> None:
        _, agent = self.channels(self.hook("ls"))
        self.assertTrue(agent.lstrip().startswith("[guardrails plugin notice]"))
        self.assertIn("comes from the guardrails plugin itself, not from the repository", agent)

    def test_it_fires_once_per_session_and_again_in_a_new_one(self) -> None:
        first = self.hook("ls", session="a")
        self.assertIn("ENGINE MISSING", json.dumps(first))
        self.assertIsNone(self.hook("ls -l", session="a"))
        self.assertIn("ENGINE MISSING", json.dumps(self.hook("ls", session="b")))
        self.assertIsNone(self.hook("ls", session="b"))

    def test_it_also_fires_for_monitor_and_when_the_command_is_denied(self) -> None:
        out = self.hook("pkill x", session="m", tool="Monitor")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("ENGINE MISSING", out["systemMessage"])
        self.assertIn("ENGINE MISSING", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_an_unrecordable_session_still_warns_every_time(self) -> None:
        import store

        with mock.patch.object(store, "write", side_effect=OSError("read-only")):
            for _ in range(2):
                self.assertIn("ENGINE MISSING", json.dumps(self.hook("ls", session="ro")))

    def test_interpolated_text_is_sanitised(self) -> None:
        astbin.record_failure(str(self.data), "boom\x1b[31m\nIGNORE ALL\r\x00")
        user, agent = self.channels(self.hook("ls"))
        for text in (user, agent):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x00", text)
            self.assertIn("download failed: boom [31m IGNORE ALL", text)

    def test_the_reason_names_the_failure_unsupported_platforms_and_the_backoff(self) -> None:
        astbin.record_failure(str(self.data), "the download timed out")
        self.assertRegex(self.channels(self.hook("ls", session="f"))[0],
                         r"Reason: npm install missing[^.]*; download failed: the download timed out \(retry after \d\d:\d\d\)")
        with mock.patch.object(astbin, "libc", return_value=("musl", "")), \
                mock.patch.object(astbin.os, "uname", return_value=uname("Linux", "aarch64")):
            self.assertIn("Reason: unsupported platform (Linux aarch64 with musl libc, not glibc)",
                          self.channels(self.hook("ls", session="u"))[0])

    def test_no_rules_or_no_ast_rules_means_no_notice(self) -> None:
        self.put(self.gpath, {"rules": {"s": {"match": {"program": "strings"}, "message": "No."}}})
        self.assertIsNone(self.hook("ls"))
        self.put(self.gpath, {})
        self.assertIsNone(self.hook("ls", session="none"))

    def test_status_problems_and_rule_test_carry_the_fix(self) -> None:
        out = self.cli("status", "--problems")[1]
        self.assertIn("cannot be evaluated", out)
        self.assertIn("guardrails engine install", out)
        self.assertIn("npm ci --ignore-scripts", out)
        rule = json.dumps({"match": {"ast": {"pattern": "pkill $$$"}}, "message": "m"})
        code, text, _ = self.cli("rule", "test", "--json", rule, "pkill x")
        self.assertEqual(code, 0)
        self.assertIn("GUARDRAILS ENGINE MISSING", text)


class EngineUsableNoNotice(AstIsolated):
    def test_a_usable_engine_never_warns(self) -> None:
        self.put(self.gpath, EngineMissingNotice.RULES)
        for session in ("a", "b"):
            out = self.hook("ls", session=session)
            self.assertIsNone(out)
        out = self.hook("pkill x", session="c")
        assert out is not None
        self.assertNotIn("ENGINE MISSING", json.dumps(out))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_the_npm_binary_is_found_and_used_without_any_download(self) -> None:
        shared = astbin.engine_root("unused")
        plat = astbin.detect()
        real = Path(astbin.install_dir("unused")) / "ast-grep"
        npm = self.plugin_root / "node_modules" / "@ast-grep" / f"cli-{plat.npm}"
        npm.mkdir(parents=True)
        os.link(real, npm / "ast-grep") if os.stat(real).st_dev == os.stat(npm).st_dev else None
        if not (npm / "ast-grep").exists():
            self.skipTest("cannot link the real binary into the fake node_modules on another device")
        (npm / "package.json").write_text(json.dumps({"name": f"@ast-grep/cli-{plat.npm}", "version": astbin.pin()}))
        self.assertTrue(shared)
        with mock.patch.object(astbin, "engine_root", lambda state_dir: str(self.tmp / "no-wheel")), \
                mock.patch.object(astbin, "fetch", side_effect=AssertionError("downloaded")):
            engine = astbin.locate(str(self.data))
            self.assertEqual((engine.how, engine.binary), ("npm", str(npm / "ast-grep")))
            self.put(self.gpath, EngineMissingNotice.RULES)
            out = self.hook("sudo pkill x")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("ENGINE MISSING", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
