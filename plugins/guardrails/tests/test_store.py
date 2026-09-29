from __future__ import annotations  # noqa: I001

import datetime
import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import Isolated

import store

UTC = datetime.timezone.utc


class Paths(Isolated):
    def test_global_uses_plugin_data(self) -> None:
        self.assertEqual(store.global_state_path(), str(self.gpath))

    def test_global_without_plugin_data_uses_derived_id(self) -> None:
        del os.environ["CLAUDE_PLUGIN_DATA"]
        expected = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data",
                                "guardrails-gaetans-claude-plugins", "state.json")
        self.assertEqual(store.global_state_path(), expected)

    def test_plugin_id_from_cache_location(self) -> None:
        here = "/u/.claude/plugins/cache/my.market/guardrails/0.2.0/lib"
        self.assertEqual(store.plugin_id(here), "guardrails-my-market")

    def test_plugin_id_outside_cache(self) -> None:
        self.assertEqual(store.plugin_id("/src/claude-plugins/plugins/guardrails/lib"),
                         "guardrails-gaetans-claude-plugins")

    def test_project_path_from_env(self) -> None:
        self.assertEqual(store.project_state_path(), str(self.ppath))

    def test_project_root_from_git(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        repo = self.tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.assertEqual(store.project_root(str(repo / "sub")), str(repo))

    def test_no_project_outside_git(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        self.assertIsNone(store.project_state_path(str(self.tmp)))

    def test_missing_cwd_means_no_project(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        self.assertIsNone(store.project_state_path(str(self.tmp / "gone")))


class LoadWrite(Isolated):
    def test_missing_is_empty(self) -> None:
        self.assertEqual(store.load(str(self.gpath)), {})
        self.assertEqual(store.load(None), {})

    def test_corrupt_raises(self) -> None:
        self.put(self.gpath, "{nope")
        with self.assertRaises(store.StateError):
            store.load(str(self.gpath))

    def test_non_object_raises(self) -> None:
        self.put(self.gpath, "[]")
        with self.assertRaises(store.StateError):
            store.load(str(self.gpath))

    def test_mutate_roundtrip_leaves_no_temp_files(self) -> None:
        store.mutate(str(self.gpath), lambda s: s.update(rules={"x": {}}))
        data = self.get(self.gpath)
        self.assertEqual(data["rules"], {"x": {}})
        self.assertIn("updatedAt", data)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["state.json", "state.json.lock"])

    def test_mutate_refuses_corrupt_file(self) -> None:
        self.put(self.gpath, "{nope")
        with self.assertRaises(store.StateError):
            store.mutate(str(self.gpath), lambda s: None)
        self.assertEqual(self.gpath.read_text(), "{nope")

    def test_exception_in_mutation_writes_nothing(self) -> None:
        self.put(self.gpath, {"rules": {}})

        def boom(state: store.State) -> None:
            state["rules"]["x"] = {}
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            store.mutate(str(self.gpath), boom)
        self.assertEqual(self.get(self.gpath), {"rules": {}})

    def test_write_prunes_sessions(self) -> None:
        old = (datetime.datetime.now(UTC) - datetime.timedelta(days=30)).isoformat()
        store.write(str(self.gpath), {"sessions": {"old": {"seenAt": old}}})
        self.assertEqual(self.get(self.gpath)["sessions"], {})


class Prune(unittest.TestCase):
    def test_drops_stale_and_malformed(self) -> None:
        now = datetime.datetime.now(UTC)
        kept = store.prune({
            "a": {"seenAt": now.isoformat()},
            "b": {"seenAt": (now - datetime.timedelta(days=8)).isoformat()},
            "c": "junk",
            "d": {"seenAt": "garbage"},
            "e": {},
        })
        self.assertEqual(list(kept), ["a"])

    def test_caps_to_newest(self) -> None:
        now = datetime.datetime.now(UTC)
        sessions = {f"s{i}": {"seenAt": (now - datetime.timedelta(minutes=i)).isoformat()} for i in range(60)}
        kept = store.prune(sessions)
        self.assertEqual(len(kept), 50)
        self.assertIn("s0", kept)
        self.assertNotIn("s59", kept)

    def test_naive_timestamp_treated_as_utc(self) -> None:
        naive = datetime.datetime.now(UTC).replace(tzinfo=None).isoformat()
        self.assertIn("a", store.prune({"a": {"seenAt": naive}}))

    def test_non_dict_is_empty(self) -> None:
        self.assertEqual(store.prune(["x"]), {})


class ManagedPath(Isolated):
    def test_env_override(self) -> None:
        self.assertEqual(store.managed_path(), str(self.mpath))

    def test_platform_defaults(self) -> None:
        del os.environ["GUARDRAILS_MANAGED_PATH"]
        expected = {"darwin": "/Library/Application Support/ClaudeCode/guardrails.json",
                    "linux": "/etc/claude-code/guardrails.json",
                    "freebsd13": "/etc/claude-code/guardrails.json",
                    "win32": r"C:\Program Files\ClaudeCode\guardrails.json"}
        for platform, path in expected.items():
            with self.subTest(platform=platform), mock.patch.object(store.sys, "platform", platform):
                self.assertEqual(store.managed_path(), path)

    def test_empty_override_falls_back_to_default(self) -> None:
        os.environ["GUARDRAILS_MANAGED_PATH"] = ""
        self.assertNotEqual(store.managed_path(), "")

    def test_missing_file_is_empty_layer(self) -> None:
        self.assertEqual(store.load_managed(), ({}, None))

    def test_valid_file_loads(self) -> None:
        self.put(self.mpath, {"rules": {"x": {}}})
        self.assertEqual(store.load_managed(), ({"rules": {"x": {}}}, None))

    def test_unusable_file_is_reported_not_raised(self) -> None:
        for content in ("{nope", "[]"):
            with self.subTest(content=content):
                self.put(self.mpath, content)
                state, error = store.load_managed()
                self.assertEqual(state, {})
                self.assertIn(str(self.mpath), error or "")

    def test_unreadable_file_is_reported(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        self.put(self.mpath, {})
        self.mpath.chmod(0)
        self.addCleanup(self.mpath.chmod, 0o644)
        self.assertIsNotNone(store.load_managed()[1])


class ManagedWrite(Isolated):
    def read_only(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o555)
        self.addCleanup(path.chmod, 0o755)

    def test_write_mode_and_directory_creation(self) -> None:
        nested = self.tmp / "a" / "b" / "guardrails.json"
        store.mutate(str(nested), lambda s: s.update(rules={}), store.MANAGED_MODE)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(nested.parent.stat().st_mode) & 0o022, 0)
        self.assertEqual(sorted(p.name for p in nested.parent.iterdir()), ["guardrails.json", "guardrails.json.lock"])

    def test_default_write_keeps_private_mode(self) -> None:
        store.write(str(self.gpath), {})
        self.assertEqual(stat.S_IMODE(self.gpath.stat().st_mode), 0o600)

    def test_writable_cases(self) -> None:
        store.ensure_writable(str(self.mpath))
        self.put(self.mpath, {})
        store.ensure_writable(str(self.mpath))

    def test_unwritable_directory_of_absent_file(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        self.read_only(self.tmp / "ro")
        target = self.tmp / "ro" / "deeper" / "guardrails.json"
        with self.assertRaises(store.NotWritable) as ctx:
            store.ensure_writable(str(target))
        self.assertIn(str(target), str(ctx.exception))
        self.assertIsInstance(ctx.exception, store.StateError)

    def test_unwritable_directory_of_existing_file(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        self.put(self.tmp / "ro" / "guardrails.json", {})
        (self.tmp / "ro").chmod(0o555)
        self.addCleanup((self.tmp / "ro").chmod, 0o755)
        with self.assertRaises(store.NotWritable):
            store.ensure_writable(str(self.tmp / "ro" / "guardrails.json"))

    def test_read_only_file(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        self.put(self.mpath, {})
        self.mpath.chmod(0o444)
        self.addCleanup(self.mpath.chmod, 0o644)
        with self.assertRaises(store.NotWritable):
            store.ensure_writable(str(self.mpath))
