from __future__ import annotations  # noqa: I001

import datetime
import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from helpers import Isolated

import store

UTC = datetime.UTC
RULE = '{"match": {"command": "strings"}, "message": "No strings."}'
APP = Path("dev.gaetans.guardrails") / "claude-plugin" / "config.json"


class Paths(Isolated):
    def test_global_config_lives_under_an_absolute_xdg_config_home(self) -> None:
        self.assertEqual(store.global_config_path(), self.tmp / "xdg" / APP)
        self.assertEqual(store.global_config_path(), self.gpath)

    def test_a_relative_or_empty_xdg_config_home_is_ignored_for_home_dot_config(self) -> None:
        for value in ("relative/xdg", "./xdg", "", None):
            with self.subTest(value=value):
                if value is None:
                    del os.environ["XDG_CONFIG_HOME"]
                else:
                    os.environ["XDG_CONFIG_HOME"] = value
                self.assertEqual(store.global_config_path(), self.home / ".config" / APP)

    def test_state_lives_in_the_plugin_data_dir_or_its_default(self) -> None:
        self.assertEqual(store.state_path(), self.spath)
        self.assertEqual(store.config_lock_path(), self.data / "config.lock")
        del os.environ["CLAUDE_PLUGIN_DATA"]
        expected = self.home / ".claude" / "plugins" / "data" / "guardrails-gaetans-claude-plugins"
        self.assertEqual((store.state_path(), store.config_lock_path()),
                         (expected / "state.json", expected / "config.lock"))

    def test_project_config_from_env(self) -> None:
        self.assertEqual(store.project_config_path(store.project_root()), self.proj / ".claude" / "guardrails.json")
        self.assertEqual(store.project_config_path(store.project_root()), self.ppath)

    def test_project_root_from_git(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        repo = self.tmp / "repo"
        (repo / "sub").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.assertEqual(store.project_root(repo / "sub"), repo)
        self.assertEqual(store.project_config_path(store.project_root(repo / "sub")),
                         repo / ".claude" / "guardrails.json")

    def test_a_project_at_the_home_directory_is_no_project(self) -> None:
        link = self.tmp / "home-link"
        link.symlink_to(self.home)
        self.put(self.home / ".claude" / "guardrails.json", {"rules": {"home": {"match": {"command": "nm"}, "message": "m"}}})
        for project in (self.home, link):
            with self.subTest(project=str(project)):
                os.environ["CLAUDE_PROJECT_DIR"] = str(project)
                self.assertIsNone(store.project_config_path(store.project_root()))
                self.assertEqual(self.cli("rule", "add", "r", "--json", RULE)[0], 0)
                code, _, err = self.cli("rule", "add", "r", "--json", RULE, "--scope", "project")
                self.assertEqual(code, 2)
                self.assertIn("home directory", err)
                status = self.cli("status")[1]
                self.assertIn("**Project** none", status)
                self.assertIn("`r` deny · global · enabled", status)
                self.assertNotIn("`home`", status)
                self.assertIsNone(self.hook("nm x", session=f"home-{project.name}"),
                                  "the user's own ~/.claude/guardrails.json is not a project layer")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.proj)
        self.assertEqual(store.project_config_path(store.project_root()), self.ppath)

    def test_a_project_config_that_is_the_global_config_is_no_project(self) -> None:
        link = self.tmp / "proj-link"
        link.symlink_to(self.proj)
        with mock.patch.object(store, "global_config_path", lambda: link / ".claude" / "guardrails.json"):
            self.assertIsNone(store.project_config_path(store.project_root()))

    def test_no_project_outside_git_or_without_a_cwd(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        for cwd in (self.tmp, self.tmp / "gone"):
            self.assertIsNone(store.project_config_path(store.project_root(cwd)))


class LoadWrite(Isolated):
    def test_missing_is_empty(self) -> None:
        self.assertEqual(store.load(self.gpath), {})
        self.assertEqual(store.load(None), {})

    def test_corrupt_or_non_object_raises_and_is_never_overwritten(self) -> None:
        for path, change in ((self.gpath, lambda: store.mutate_config(self.gpath, lambda c: None)),
                             (self.spath, lambda: store.mutate_state(lambda s: None))):
            for content in ("{nope", "[]"):
                with self.subTest(path=path.name, content=content):
                    self.put(path, content)
                    with self.assertRaises(store.StoreError):
                        store.load(path)
                    with self.assertRaises(store.StoreError):
                        change()
                    self.assertEqual(path.read_text(), content)

    def test_a_config_write_is_atomic_unstamped_and_locks_in_the_data_dir(self) -> None:
        store.mutate_config(self.gpath, lambda c: c.update(rules={"x": {}}))
        self.assertEqual(self.get(self.gpath), {"rules": {"x": {}}})
        self.assertEqual(sorted(p.name for p in self.gpath.parent.iterdir()), ["config.json"])
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["config.lock"])
        store.mutate_config(self.ppath, lambda c: c.update(enabled=False))
        self.assertEqual(sorted(p.name for p in self.ppath.parent.iterdir()), ["guardrails.json"])
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["config.lock"])

    def test_the_first_rule_add_creates_the_config_dir_and_a_private_file(self) -> None:
        self.assertFalse((self.tmp / "xdg").exists())
        self.assertEqual(self.cli("rule", "add", "r", "--json", RULE)[0], 0)
        self.assertEqual(stat.S_IMODE(self.gpath.stat().st_mode), 0o600)
        self.assertEqual(sorted(self.get(self.gpath)), ["rules"])
        self.assertEqual(self.cli("rule", "add", "p", "--json", RULE, "--scope", "project")[0], 0)
        self.assertEqual(stat.S_IMODE(self.ppath.stat().st_mode), 0o600)

    def test_exception_in_mutation_writes_nothing(self) -> None:
        self.put(self.gpath, {"rules": {}})

        def boom(config: store.Doc) -> None:
            config["rules"]["x"] = {}
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            store.mutate_config(self.gpath, boom)
        self.assertEqual(self.get(self.gpath), {"rules": {}})

    def test_a_state_write_stamps_and_prunes_sessions_under_its_own_lock(self) -> None:
        old = (datetime.datetime.now(UTC) - datetime.timedelta(days=30)).isoformat()
        fresh = store.now()
        self.put(self.spath, {"sessions": {"old": {"seenAt": old}, "new": {"seenAt": fresh}}})
        store.mutate_state(lambda s: None)
        state = self.get(self.spath)
        self.assertEqual(sorted(state), ["sessions", "updatedAt"])
        self.assertEqual(state["sessions"], {"new": {"seenAt": fresh}})
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["state.json", "state.json.lock"])

    def test_rule_and_mode_changes_write_config_never_state(self) -> None:
        self.assertEqual(self.cli("rule", "add", "r", "--json", RULE)[0], 0)
        self.assertEqual(self.cli("rule", "set", "r", "--json", '{"action": "warn"}')[0], 0)
        self.assertEqual(self.cli("rule", "add", "p", "--json", RULE, "--scope", "project")[0], 0)
        self.assertEqual(self.cli("mode", "declare", "m")[0], 0)
        self.assertEqual(self.cli("mode", "on", "m", "--scope", "global")[0], 0)
        self.assertEqual(self.cli("preset", "install", "process-safety")[0], 0)
        self.assertEqual(self.cli("disable", "--scope", "project")[0], 0)
        self.assertEqual(self.cli("rule", "rm", "p", "--scope", "project")[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["r"]["action"], "warn")
        self.assertIn("setBy", self.get(self.gpath)["rules"]["r"])
        self.assertEqual(self.get(self.ppath)["enabled"], False)
        self.assertNotIn("updatedAt", self.get(self.gpath))
        self.assertFalse(self.spath.exists())
        self.assertEqual(self.cli("rule", "rm", "r")[0], 0)
        self.assertNotIn("r", self.get(self.gpath)["rules"])
        self.assertFalse(self.spath.exists())
        self.assertEqual(self.cli("mode", "on", "m", "--session-id", "s1")[0], 0)
        self.assertEqual(sorted(self.get(self.spath)), ["sessions", "updatedAt"])
        self.assertNotIn("sessions", self.get(self.gpath))


class MisplacedConfig(Isolated):
    """Configuration left in the state file or in a project's old state file is reported loudly, never applied."""

    def old_project_file(self) -> Path:
        return self.proj / ".claude" / "plugins" / "data" / "guardrails-gaetans-claude-plugins" / "state.json"

    def test_rules_left_in_the_state_file_are_named_once_per_session_and_in_status(self) -> None:
        self.put(self.spath, {"rules": {"no-strings": json.loads(RULE)}, "modes": {}, "sessions": {}})
        out = self.hook("strings x")
        assert out is not None
        self.assertNotIn("hookSpecificOutput", out, "rules left in the state file are not enforced")
        for needle in (str(self.spath), "still holds rules, modes", "NOT applied", str(self.gpath)):
            self.assertIn(needle, out["systemMessage"])
        self.assertIsNone(self.hook("strings y"))
        other = self.hook("ls", session="s2")
        assert other is not None
        self.assertIn("still holds rules", other["systemMessage"])
        for argv in (("status", "--problems"), ("status", "--problems", "--scope", "global")):
            problems = self.cli(*argv)[1]
            self.assertIn(f"the state file {self.spath} still holds rules, modes", problems)
            self.assertIn(f"Move it to {self.gpath}", problems)
        self.assertEqual(self.cli("status", "--problems", "--scope", "project")[1].strip(), "No problems.")
        self.assertIn("rules", self.get(self.spath), "the hook never strips the stray keys")

    def test_the_problem_goes_away_once_the_rules_move_to_config(self) -> None:
        self.put(self.spath, {"rules": {"no-strings": json.loads(RULE)}})
        state = self.get(self.spath)
        self.put(self.gpath, {"rules": state.pop("rules")})
        self.put(self.spath, state)
        self.assertEqual(self.cli("status", "--problems")[1].strip(), "No problems.")
        out = self.hook("strings x")
        assert out is not None
        self.assertNotIn("systemMessage", out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_an_old_project_state_file_is_named_in_the_hook_and_in_status(self) -> None:
        self.put(self.old_project_file(), {"rules": {"no-strings": json.loads(RULE)}})
        out = self.hook("strings x")
        assert out is not None
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn(f"the old project state file {self.old_project_file()} is no longer read", out["systemMessage"])
        self.assertIn(str(self.ppath), out["systemMessage"])
        problems = self.cli("status", "--problems", "--scope", "project")[1]
        self.assertIn("old project state file", problems)
        self.old_project_file().unlink()
        self.assertEqual(self.cli("status", "--problems")[1].strip(), "No problems.")

    def test_an_unreadable_state_file_is_reported_and_the_hook_still_enforces_without_writing_it(self) -> None:
        self.put(self.gpath, {"rules": {"no-strings": {**json.loads(RULE), "retry": "same-command"}}})
        self.put(self.spath, "{nope")
        for _ in range(2):
            out = self.hook("strings x")
            assert out is not None
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.spath.read_text(), "{nope")
        self.assertIn("unreadable state file", self.cli("status", "--problems")[1])


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
        rule = '{"match": {"command": "x"}, "message": "m"}'
        for key, verb in (("rules", ("rule", "add", "r", "--json", rule)), ("modes", ("mode", "declare", "m")),
                          ("sessions", ("mode", "on", "m", "--session-id", "s"))):
            with self.subTest(key=key):
                path = self.spath if key == "sessions" else self.gpath
                self.put(self.gpath, {"modes": {"m": {"description": "d"}}})
                self.put(path, {**(self.get(path) if path.exists() else {}), key: [1, 2]})
                before = path.read_text()
                code, _, err = self.cli(*verb)
                self.assertEqual(code, 2)
                self.assertIn(str(path), err)
                self.assertIn(f"'{key}' must be an object", err)
                self.assertEqual(path.read_text(), before, "the file is untouched")


class ManagedPath(Isolated):
    RULE: ClassVar[dict[str, Any]] = {"match": {"command": "x"}, "message": "m"}

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
        with self.assertRaises(store.StoreError):
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
        with self.assertRaises(OSError), store.locked(self.tmp / "managed" / "guardrails.json.lock"):
            pass
        self.assertFalse(target.exists())

    def test_write_is_private_unless_public(self) -> None:
        store.write(self.gpath, {})
        store.write(self.mpath, {}, public=True)
        self.assertEqual((stat.S_IMODE(self.gpath.stat().st_mode), stat.S_IMODE(self.mpath.stat().st_mode)), (0o600, 0o644))
