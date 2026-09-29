from __future__ import annotations  # noqa: I001

import datetime
import os
import stat
import subprocess
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from helpers import Isolated

import store

UTC = datetime.timezone.utc
_real_default = store.default_managed_path


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
    RULE: ClassVar[dict[str, Any]] = {"match": {"program": "x"}, "message": "m"}

    def test_override_is_a_second_source_below_the_default(self) -> None:
        self.assertEqual(store.managed_paths(), [str(self.dpath), str(self.mpath)])
        self.assertEqual(store.managed_write_path(), str(self.mpath))

    def test_no_override_means_only_the_default(self) -> None:
        del os.environ["GUARDRAILS_MANAGED_PATH"]
        self.assertEqual(store.managed_paths(), [str(self.dpath)])
        self.assertEqual(store.managed_write_path(), str(self.dpath))

    def test_override_equal_to_default_is_loaded_once(self) -> None:
        os.environ["GUARDRAILS_MANAGED_PATH"] = str(self.dpath)
        self.assertEqual(store.managed_paths(), [str(self.dpath)])

    def test_empty_override_is_ignored(self) -> None:
        os.environ["GUARDRAILS_MANAGED_PATH"] = ""
        self.assertEqual(store.managed_paths(), [str(self.dpath)])
        self.assertEqual(store.managed_write_path(), str(self.dpath))

    def test_platform_defaults(self) -> None:
        expected = {"darwin": "/Library/Application Support/ClaudeCode/guardrails.json",
                    "linux": "/etc/claude-code/guardrails.json",
                    "freebsd13": "/etc/claude-code/guardrails.json"}
        for platform, path in expected.items():
            with self.subTest(platform=platform), mock.patch.object(store.sys, "platform", platform), \
                    mock.patch.object(store, "default_managed_path", _real_default):
                self.assertEqual(store.default_managed_path(), path)

    def test_missing_files_are_an_empty_layer(self) -> None:
        self.assertEqual(store.load_managed(), ({"rules": {}, "modes": {}}, []))

    def test_valid_file_loads(self) -> None:
        self.put(self.mpath, {"rules": {"x": self.RULE}})
        state, problems = store.load_managed()
        self.assertEqual((sorted(state["rules"]), problems), (["x"], []))

    def test_default_outranks_override_and_override_only_adds(self) -> None:
        self.put(self.dpath, {"rules": {"x": {**self.RULE, "message": "default", "retry": "none"}}})
        self.put(self.mpath, {"rules": {"x": {**self.RULE, "message": "override", "action": "warn",
                                              "retry": "same-command", "enabled": False},
                                        "y": self.RULE}})
        state, problems = store.load_managed()
        self.assertEqual(problems, [])
        x = state["rules"]["x"]
        self.assertEqual((x["message"], x["action"], x["retry"], x["enabled"]), ("default", "deny", "none", True))
        self.assertIn("y", state["rules"])

    def test_override_cannot_disable_the_default_layer(self) -> None:
        self.put(self.dpath, {"rules": {"x": self.RULE}})
        for override in ("/nonexistent/dir/x.json", str(self.tmp / "empty.json")):
            with self.subTest(override=override):
                (self.tmp / "empty.json").write_text("{}")
                os.environ["GUARDRAILS_MANAGED_PATH"] = override
                state, problems = store.load_managed()
                self.assertEqual((list(state["rules"]), problems), (["x"], []))

    def test_unusable_file_is_reported_not_raised(self) -> None:
        for content in ("{nope", "[]"):
            with self.subTest(content=content):
                self.put(self.mpath, content)
                state, problems = store.load_managed()
                self.assertEqual(state, {"rules": {}, "modes": {}})
                self.assertEqual(len(problems), 1)
                self.assertIn(str(self.mpath), problems[0])
                self.assertIn("NOT enforced", problems[0])
                self.assertIn("by hand", problems[0])

    def test_one_unusable_source_leaves_the_other(self) -> None:
        self.put(self.dpath, {"rules": {"x": self.RULE}})
        self.put(self.mpath, "{nope")
        state, problems = store.load_managed()
        self.assertEqual(list(state["rules"]), ["x"])
        self.assertEqual(len(problems), 1)

    def test_unreadable_file_is_reported(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        self.put(self.mpath, {})
        self.mpath.chmod(0)
        self.addCleanup(self.mpath.chmod, 0o644)
        self.assertEqual(len(store.load_managed()[1]), 1)

    def test_unreadable_parent_directory_is_reported_not_absent(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        self.put(self.mpath, {"rules": {"x": self.RULE}})
        self.mpath.parent.chmod(0)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        with self.assertRaises(store.StateError):
            store.load(str(self.mpath))
        problems = store.load_managed()[1]
        self.assertEqual(len(problems), 1)
        self.assertIn("NOT enforced", problems[0])
        self.assertEqual(store.presence(str(self.mpath)), " (unreadable)")

    def test_absent_paths(self) -> None:
        self.assertEqual(store.presence(str(self.mpath)), " (absent)")
        self.put(self.tmp / "file", "x")
        self.assertEqual(store.presence(str(self.tmp / "file" / "sub")), " (absent)")
        self.assertEqual(store.load(str(self.tmp / "file" / "sub")), {})
        self.put(self.mpath, {})
        self.assertEqual(store.presence(str(self.mpath)), "")

    def test_wrong_shape_is_reported(self) -> None:
        for content, needle in (({"rules": [1]}, "'rules' must be an object"), ({"modes": "x"}, "'modes' must be an object"),
                                ({"rules": {"a": 1}}, "rules entry 'a' is not an object")):
            with self.subTest(content=content):
                self.put(self.mpath, content)
                problems = store.load_managed()[1]
                self.assertEqual(len(problems), 1)
                self.assertIn(needle, problems[0])
                self.assertIn(str(self.mpath), problems[0])

    def test_invalid_rules_and_undeclared_modes_are_reported(self) -> None:
        self.put(self.mpath, {"rules": {"bad": {"match": {"regex": "("}, "message": "x"},
                                        "ghosty": {**self.RULE, "modes": ["ghost", "real"]}},
                              "modes": {"real": {}}})
        problems = store.load_managed()[1]
        self.assertEqual(len(problems), 2)
        self.assertIn("managed rule bad is invalid and ignored", problems[0])
        self.assertIn("managed rule ghosty lists mode 'ghost'", problems[1])

    def test_trust_problems(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("everything is owned by root")
        self.put(self.mpath, {})
        problems = store.trust_problems(str(self.mpath))
        self.assertEqual(len([p for p in problems if "not owned by root" in p]), 2)
        self.mpath.chmod(0o666)
        self.mpath.parent.chmod(0o777)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        problems = store.trust_problems(str(self.mpath))
        self.assertEqual(len([p for p in problems if "writable by group or others" in p]), 2)
        self.assertEqual(store.trust_problems(str(self.tmp / "nope" / "x.json")), [])


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

    def test_created_directories_ignore_umask(self) -> None:
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        nested = self.tmp / "u" / "v" / "guardrails.json"
        store.mutate(str(nested), lambda s: s.update(rules={}), store.MANAGED_MODE)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o644)
        for directory in (nested.parent, nested.parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)

    def test_existing_directory_mode_is_left_alone(self) -> None:
        self.mpath.parent.mkdir(mode=0o700)
        store.mutate(str(self.mpath), lambda s: s.update(rules={}), store.MANAGED_MODE)
        self.assertEqual(stat.S_IMODE(self.mpath.parent.stat().st_mode), 0o700)

    def test_lock_file_is_not_followed_through_a_symlink(self) -> None:
        target = self.tmp / "victim"
        self.mpath.parent.mkdir()
        (self.tmp / "managed" / "guardrails.json.lock").symlink_to(target)
        with self.assertRaises(OSError), store.locked(str(self.mpath)):
            pass
        self.assertFalse(target.exists())

    def test_write_syncs_file_and_directory(self) -> None:
        with mock.patch.object(store.os, "fsync") as fsync:
            store.write(str(self.gpath), {})
        self.assertEqual(fsync.call_count, 2)

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
