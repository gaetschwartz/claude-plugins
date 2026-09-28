from __future__ import annotations

import datetime
import os
import subprocess
import unittest

import store
from helpers import Isolated

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
        here = "/u/.claude/plugins/cache/my.market/guardrails/0.2.0/hooks"
        self.assertEqual(store.plugin_id(here), "guardrails-my-market")

    def test_plugin_id_outside_cache(self) -> None:
        self.assertEqual(store.plugin_id("/src/claude-plugins/plugins/guardrails/hooks"),
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
