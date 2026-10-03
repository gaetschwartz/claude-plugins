from __future__ import annotations  # noqa: I001

import datetime
import os
import stat
import subprocess
import unittest
from pathlib import Path
from typing import Any, ClassVar

from helpers import Isolated

import store

UTC = datetime.UTC


class Paths(Isolated):
    def test_global_uses_plugin_data(self) -> None:
        self.assertEqual(store.global_state_path(), self.gpath)

    def test_global_without_plugin_data_uses_derived_id(self) -> None:
        del os.environ["CLAUDE_PLUGIN_DATA"]
        expected = Path.home() / ".claude" / "plugins" / "data" / "guardrails-gaetans-claude-plugins" / "state.json"
        self.assertEqual(store.global_state_path(), expected)

    def test_project_path_from_env(self) -> None:
        self.assertEqual(store.project_state_path(), self.ppath)

    def test_project_root_from_git(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        repo = self.tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.assertEqual(store.project_root(repo / "sub"), repo)

    def test_no_project_outside_git_or_without_a_cwd(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        for cwd in (self.tmp, self.tmp / "gone"):
            self.assertIsNone(store.project_state_path(cwd))


class LoadWrite(Isolated):
    def test_missing_is_empty(self) -> None:
        self.assertEqual(store.load(self.gpath), {})
        self.assertEqual(store.load(None), {})

    def test_corrupt_or_non_object_raises_and_is_never_overwritten(self) -> None:
        for content in ("{nope", "[]"):
            with self.subTest(content=content):
                self.put(self.gpath, content)
                with self.assertRaises(store.StateError):
                    store.load(self.gpath)
                with self.assertRaises(store.StateError):
                    store.mutate(self.gpath, lambda s: None)
                self.assertEqual(self.gpath.read_text(), content)

    def test_mutate_roundtrip_leaves_no_temp_files(self) -> None:
        store.mutate(self.gpath, lambda s: s.update(rules={"x": {}}))
        data = self.get(self.gpath)
        self.assertEqual(data["rules"], {"x": {}})
        self.assertIn("updatedAt", data)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["state.json", "state.json.lock"])

    def test_exception_in_mutation_writes_nothing(self) -> None:
        self.put(self.gpath, {"rules": {}})

        def boom(state: store.State) -> None:
            state["rules"]["x"] = {}
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            store.mutate(self.gpath, boom)
        self.assertEqual(self.get(self.gpath), {"rules": {}})

    def test_write_prunes_sessions(self) -> None:
        old = (datetime.datetime.now(UTC) - datetime.timedelta(days=30)).isoformat()
        store.write(self.gpath, {"sessions": {"old": {"seenAt": old}}})
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
        self.assertEqual(sorted(kept), ["a"])
        self.assertEqual(store.prune(["x"]), {})

    def test_caps_to_newest(self) -> None:
        now = datetime.datetime.now(UTC)
        sessions = {f"s{i}": {"seenAt": (now - datetime.timedelta(minutes=i)).isoformat()} for i in range(60)}
        kept = store.prune(sessions)
        self.assertEqual(len(kept), 50)
        self.assertIn("s0", kept)
        self.assertNotIn("s59", kept)


class CorruptTables(Isolated):
    def test_a_malformed_table_stops_a_write_and_names_the_file_and_key(self) -> None:
        rule = '{"match": {"program": "x"}, "message": "m"}'
        for key, verb in (("rules", ("rule", "add", "r", "--json", rule)), ("modes", ("mode", "declare", "m")),
                          ("sessions", ("mode", "on", "m", "--session-id", "s"))):
            with self.subTest(key=key):
                state: dict[str, object] = {"modes": {"m": {"description": "d"}}}
                state[key] = [1, 2]
                self.put(self.gpath, state)
                before = self.gpath.read_text()
                code, _, err = self.cli(*verb)
                self.assertEqual(code, 2)
                self.assertIn(str(self.gpath), err)
                self.assertIn(f"'{key}' must be an object", err)
                self.assertEqual(self.gpath.read_text(), before, "the file is untouched")


class ManagedPath(Isolated):
    RULE: ClassVar[dict[str, Any]] = {"match": {"program": "x"}, "message": "m"}

    def test_platform_defaults(self) -> None:
        self.assertEqual(store.managed_path_for("darwin"), Path("/Library/Application Support/ClaudeCode/guardrails.json"))
        for platform in ("linux", "freebsd13"):
            self.assertEqual(store.managed_path_for(platform), Path("/etc/claude-code/guardrails.json"))

    def test_the_layer_of_a_valid_absent_or_unusable_file(self) -> None:
        self.assertEqual(store.load_managed(), ({"rules": {}, "modes": {}}, []))
        self.assertEqual(store.presence(self.mpath), "absent")
        self.put(self.mpath, {"rules": {"x": self.RULE}})
        state, problems = store.load_managed()
        self.assertEqual((sorted(state["rules"]), problems, store.presence(self.mpath)), (["x"], [], "present"))
        for content in ("{nope", "[]"):
            self.put(self.mpath, content)
            state, problems = store.load_managed()
            self.assertEqual(state, {"rules": {}, "modes": {}})
            self.assertEqual(len(problems), 1)
            self.assertIn(str(self.mpath), problems[0])
            self.assertIn("NOT enforced", problems[0])
            self.assertIn("by hand", problems[0])

    def test_unreadable_file_or_directory_is_reported_not_absent(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        self.put(self.mpath, {"rules": {"x": self.RULE}})
        self.mpath.parent.chmod(0)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        with self.assertRaises(store.StateError):
            store.load(self.mpath)
        self.assertEqual((len(store.load_managed()[1]), store.presence(self.mpath)), (1, "unreadable"))

    def test_absent_paths(self) -> None:
        self.put(self.tmp / "file", "x")
        self.assertEqual(store.presence(self.tmp / "file" / "sub"), "absent")
        self.assertEqual(store.load(self.tmp / "file" / "sub"), {})

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
        self.put(self.mpath, {"rules": {"bad": {"match": {"builtin": "nope"}, "message": "x"},
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
        problems = store.trust_problems(self.mpath)
        self.assertEqual(len([p for p in problems if "not owned by root" in p]), 2)
        self.mpath.chmod(0o666)
        self.mpath.parent.chmod(0o777)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        problems = store.trust_problems(self.mpath)
        self.assertEqual(len([p for p in problems if "writable by group or others" in p]), 2)
        self.assertEqual(store.trust_problems(self.tmp / "nope" / "x.json"), [])


class ManagedWrite(Isolated):
    def test_lock_file_is_not_followed_through_a_symlink(self) -> None:
        target = self.tmp / "victim"
        self.mpath.parent.mkdir()
        (self.tmp / "managed" / "guardrails.json.lock").symlink_to(target)
        with self.assertRaises(OSError), store.locked(self.mpath):
            pass
        self.assertFalse(target.exists())

    def test_write_is_private_unless_public(self) -> None:
        store.write(self.gpath, {})
        store.write(self.mpath, {}, public=True)
        self.assertEqual((stat.S_IMODE(self.gpath.stat().st_mode), stat.S_IMODE(self.mpath.stat().st_mode)), (0o600, 0o644))
