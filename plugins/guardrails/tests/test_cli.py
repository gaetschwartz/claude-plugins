from __future__ import annotations

import io
import json
import os
import shlex
import stat
import unicodedata
from unittest import mock

import store
from helpers import AstIsolated, caught

RULE = ('{"match": {"program": "strings"}, "message": "Read the docs.", "retry": "same-command", '
        '"modes": ["reverse-engineering"]}')
BARE = '{"match": {"program": "pkill"}, "message": "No pkill."}'


def stdin(text: str) -> mock._patch:  # type: ignore[type-arg]
    return mock.patch("sys.stdin", io.StringIO(text))


class RuleCommands(AstIsolated):
    def test_add_set_rm_round_trip_with_the_actor_recorded(self) -> None:
        code, out, _ = self.cli("rule", "add", "no-strings", "--json", RULE, "--reason", "user asked")
        self.assertEqual(code, 0)
        self.assertIn("added rule no-strings", out)
        rule = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual((rule["setBy"]["by"], rule["setBy"]["reason"]), ("user", "user asked"))
        self.assertEqual(self.set_rule("no-strings", {"action": "warn", "program": ["strings", "otool"]})[0], 0)
        self.assertEqual(self.set_rule("no-strings", {"messageShort": "short", "requires": ["fd", "fdfind"]})[0], 0)
        rule = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual((rule["action"], rule["match"]["program"], rule["messageShort"], rule["requires"]),
                         ("warn", ["strings", "otool"], "short", ["fd", "fdfind"]))
        self.assertEqual(self.set_rule("no-strings", {"messageShort": None})[0], 0)
        self.assertNotIn("messageShort", self.get(self.gpath)["rules"]["no-strings"])
        self.assertEqual(self.cli("rule", "rm", "no-strings")[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"], {})

    def test_bad_input_exits_2_and_changes_nothing(self) -> None:
        for argv in (("rule", "add", "x", "--json", '{"match": {}, "message": "m"}'), ("rule", "add", "x", "--json", "{"),
                     ("rule", "add", "bad id", "--json", RULE), ("rule", "rm", "ghost"),
                     ("rule", "set", "ghost", "--json", '{"action": "deny"}')):
            with self.subTest(argv=argv):
                code, _, err = self.cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn("error:", err)
        self.assertFalse(self.gpath.exists())
        self.cli("rule", "add", "no-strings", "--json", RULE)
        for fields in ({"colour": "red"}, {"regex": "("}, {"enabled": "maybe"}, {"modes": [1]}, {"message": 3}, [1]):
            with self.subTest(fields=fields):
                self.assertEqual(self.set_rule("no-strings", fields)[0], 2)
        self.assertNotIn("regex", self.get(self.gpath)["rules"]["no-strings"]["match"])
        self.put(self.gpath, "{nope")
        self.assertEqual(self.cli("rule", "add", "x", "--json", RULE)[0], 2)
        self.assertEqual(self.gpath.read_text(), "{nope")

    def test_project_scope_writes_the_project_file_and_overrides_only_tighten(self) -> None:
        self.assertEqual(self.cli("rule", "add", "p", "--json", RULE, "--scope", "project")[0], 0)
        self.assertIn("p", self.get(self.ppath)["rules"])
        self.assertFalse(self.gpath.exists())
        self.cli("rule", "add", "no-strings", "--json", RULE)
        code, out, _ = self.set_rule("no-strings", {"action": "deny", "enabled": False, "args": "-a"}, "--scope", "project")
        self.assertEqual(code, 0)
        entry = self.get(self.ppath)["rules"]["no-strings"]
        self.assertEqual((entry["action"], entry["match"], "message" in entry), ("deny", {"args": "-a"}, False))
        self.assertIn("enabled=false", out)
        self.assertIn('args="-a"', out)
        del os.environ["CLAUDE_PROJECT_DIR"]
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.assertEqual(self.cli("rule", "add", "x", "--json", RULE, "--scope", "project")[0], 2)

    def test_agents_need_as_user_for_changes_and_cannot_toggle_the_hook(self) -> None:
        code, _, err = self.cli("rule", "add", "no-strings", "--json", RULE, agent=True)
        self.assertEqual(code, 3)
        self.assertIn("--as-user", err)
        self.assertFalse(self.gpath.exists())
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE, "--as-user", agent=True)[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["no-strings"]["setBy"]["by"], "agent")
        self.assertEqual(self.cli("rule", "rm", "no-strings", agent=True)[0], 3)
        for verb in ("enable", "disable"):
            self.assertEqual([self.cli(verb, agent=True)[0], self.cli(verb, "--as-user", agent=True)[0]], [3, 3])
        for argv in (("status",), ("preset", "list"), ("rule", "test", "--json", RULE, "strings x")):
            self.assertEqual(self.cli(*argv, agent=True)[0], 0)

    def test_disable_and_enable_the_hook_or_just_the_project(self) -> None:
        self.assertEqual(self.cli("disable", "--reason", "noisy")[0], 0)
        state = self.get(self.gpath)
        self.assertEqual((state["enabled"], state["disabledReason"], state["setBy"]["by"]), (False, "noisy", "user"))
        self.assertEqual(self.cli("enable")[0], 0)
        state = self.get(self.gpath)
        self.assertEqual((state["enabled"], "disabledReason" in state), (True, False))
        self.assertEqual(self.cli("disable", "--scope", "project")[0], 0)
        self.assertFalse(self.get(self.ppath)["enabled"])


class ModeCommands(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"

    def declare(self, *extra: str) -> int:
        return self.cli("mode", "declare", "reverse-engineering", "--description", "RE", *extra)[0]

    def on(self, *extra: str, agent: bool = False) -> int:
        return self.cli("mode", "on", "reverse-engineering", *extra, agent=agent)[0]

    def test_declare_redeclare_and_undeclare(self) -> None:
        self.assertEqual(self.declare("--agent-may-enable"), 0)
        mode = self.get(self.gpath)["modes"]["reverse-engineering"]
        self.assertEqual((mode["description"], mode["agentMayEnable"], mode["active"]), ("RE", True, False))
        self.assertEqual(self.on("--scope", "global"), 0)
        self.assertEqual(self.cli("mode", "declare", "reverse-engineering", "--agent-may-enable")[0], 0)
        mode = self.get(self.gpath)["modes"]["reverse-engineering"]
        self.assertEqual((mode["active"], mode["description"]), (True, "RE"))
        self.assertEqual(self.cli("mode", "undeclare", "reverse-engineering")[0], 0)
        self.assertEqual(self.get(self.gpath)["modes"], {})
        self.assertEqual(self.cli("mode", "undeclare", "reverse-engineering")[0], 2)
        self.assertEqual(self.cli("mode", "declare", "x", agent=True)[0], 3)
        self.assertEqual(self.cli("mode", "declare", "x", "--as-user", agent=True)[0], 0)

    def test_session_modes_need_a_declared_mode_a_session_id_and_for_agents_permission_and_a_reason(self) -> None:
        self.assertEqual(self.cli("mode", "on", "ghost")[0], 2)
        self.declare("--agent-may-enable")
        self.assertEqual(self.on(agent=True), 2, "an agent must say why")
        self.assertEqual(self.on("--reason", "user said RE", agent=True), 0)
        record = self.get(self.gpath)["sessions"]["s1"]["modes"]["reverse-engineering"]
        self.assertEqual((record["by"], record["reason"]), ("agent", "user said RE"))
        self.assertEqual(self.cli("mode", "off", "reverse-engineering", agent=True)[0], 0)
        self.assertNotIn("reverse-engineering", self.get(self.gpath)["sessions"]["s1"]["modes"])
        del os.environ["CLAUDE_CODE_SESSION_ID"]
        self.assertEqual(self.on(), 2)
        self.assertEqual(self.on("--session-id", "s9"), 0)
        self.assertIn("s9", self.get(self.gpath)["sessions"])
        self.assertEqual(self.cli("mode", "declare", "reverse-engineering", "--scope", "project")[0], 0)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
        self.assertEqual(self.on("--reason", "x", agent=True), 3, "a project can forbid agent enabling")

    def test_an_agent_cannot_switch_on_a_mode_that_forbids_it(self) -> None:
        self.declare()
        self.assertEqual(self.on("--reason", "x", "--as-user", agent=True), 3)
        self.assertEqual(self.on(), 0)

    def test_persistent_scopes_are_user_only_and_a_project_can_stub_a_global_mode(self) -> None:
        self.assertEqual(self.on("--scope", "project"), 2, "declared nowhere")
        self.declare("--agent-may-enable")
        self.assertEqual(self.on("--scope", "global", agent=True), 3)
        self.assertEqual(self.on("--scope", "global", "--as-user", agent=True), 0)
        self.assertTrue(self.get(self.gpath)["modes"]["reverse-engineering"]["active"])
        self.assertEqual(self.cli("mode", "off", "reverse-engineering", "--scope", "global")[0], 0)
        self.assertFalse(self.get(self.gpath)["modes"]["reverse-engineering"]["active"])
        self.assertEqual(self.on("--scope", "project", "--as-user", agent=True), 0)
        mode = self.get(self.ppath)["modes"]["reverse-engineering"]
        self.assertEqual(("agentMayEnable" in mode, mode["active"], mode["setBy"]["by"]), (False, True, "agent"))
        self.assertIn("`reverse-engineering` on (persistent) · agent may enable: yes", self.cli("status")[1])
        self.assertEqual(self.cli("mode", "off", "reverse-engineering", "--scope", "project")[0], 0)
        self.assertFalse(self.get(self.ppath)["modes"]["reverse-engineering"]["active"])

    def test_a_session_mode_suspends_the_rule_end_to_end(self) -> None:
        self.cli("rule", "add", "no-strings", "--json", RULE)
        self.declare("--agent-may-enable")
        self.on("--reason", "user: RE libfoo", agent=True)
        out = self.hook("strings a", session="s1")
        assert out is not None
        self.assertIn("suspended by mode reverse-engineering", out["systemMessage"])


class ManagedScope(AstIsolated):
    def managed(self, *argv: str, agent: bool = False) -> tuple[int, str, str]:
        return self.cli(*argv, "--scope", "managed", agent=agent)

    def test_rules_round_trip_with_the_always_enforced_note_and_the_umask_ignored(self) -> None:
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        code, out, _ = self.managed("rule", "add", "no-pkill", "--json", BARE, "--reason", "policy")
        self.assertEqual(code, 0)
        self.assertIn(f"added rule no-pkill in {self.mpath}", out)
        self.assertIn("note: managed rule no-pkill lists no modes, so it is always enforced", out)
        self.assertEqual((stat.S_IMODE(self.mpath.stat().st_mode), stat.S_IMODE(self.mpath.parent.stat().st_mode)),
                         (0o644, 0o755))
        self.assertEqual(self.get(self.mpath)["rules"]["no-pkill"]["setBy"]["reason"], "policy")
        self.assertFalse(self.gpath.exists())
        self.assertNotIn("always enforced", self.managed("rule", "add", "modal", "--json", RULE)[1])
        self.assertIn("always enforced", self.managed("rule", "set", "modal", "--json", '{"modes": []}')[1])
        self.assertNotIn("always enforced", self.cli("rule", "add", "local", "--json", BARE)[1])
        self.assertEqual(self.managed("rule", "set", "no-pkill", "--json", '{"action": "warn", "modes": []}')[0], 0)
        self.assertEqual(self.get(self.mpath)["rules"]["no-pkill"]["action"], "warn")
        for verb in (("set", "ghost", "--json", '{"action": "deny"}'), ("rm", "ghost")):
            self.assertEqual(self.managed("rule", *verb)[0], 2)
        self.assertEqual(self.managed("rule", "rm", "no-pkill")[0], 0)
        self.assertNotIn("no-pkill", self.get(self.mpath)["rules"])

    def test_the_default_scope_is_never_managed(self) -> None:
        self.cli("rule", "add", "g", "--json", RULE)
        self.cli("mode", "declare", "m")
        self.cli("preset", "install", "docs-first")
        self.assertFalse(self.mpath.exists())

    def test_other_scopes_cannot_change_a_managed_rule_but_can_edit_their_own_entry_for_the_id(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        before = self.mpath.read_text()
        for scope in ("global", "project"):
            for argv in (("rule", "set", "no-strings", "--json", '{"action": "warn"}'), ("rule", "rm", "no-strings")):
                code, _, err = self.cli(*argv, "--scope", scope)
                self.assertEqual(code, 3, (scope, argv))
                self.assertIn("--scope managed", err)
        self.assertEqual((self.mpath.read_text(), self.gpath.exists(), self.ppath.exists()), (before, False, False))
        code, out, _ = self.cli("rule", "add", "no-strings", "--json", RULE, "--scope", "project")
        self.assertEqual(code, 0)
        self.assertIn("also a managed rule", out)
        self.assertEqual(self.cli("rule", "set", "no-strings", "--json", '{"message": "mine"}', "--scope", "project")[0], 0)
        self.assertEqual(self.get(self.ppath)["rules"]["no-strings"]["message"], "mine")
        self.assertEqual(self.cli("rule", "rm", "no-strings", "--scope", "project")[0], 0)
        self.assertEqual(self.cli("rule", "rm", "no-strings", "--scope", "project")[0], 3)
        self.put(self.gpath, {"rules": {"no-strings": {**json.loads(RULE), "action": "warn"}}})
        self.assertEqual(self.cli("rule", "set", "no-strings", "--json", '{"action": "deny"}')[0], 0)
        self.assertEqual(self.cli("rule", "rm", "no-strings")[0], 0)
        self.cli("rule", "add", "no-strings", "--json", RULE.replace("Read", "Ignore"))
        self.assertIn("Read the docs", self.cli("rule", "test", "--id", "no-strings", "strings a")[1])
        self.assertIn("no-strings", self.get(self.mpath)["rules"])

    def test_modes_declare_activate_and_layer_over_the_managed_declaration(self) -> None:
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
        code, out, _ = self.managed("mode", "declare", "incident", "--description", "firefighting")
        self.assertEqual(code, 0)
        self.assertIn(str(self.mpath), out)
        self.assertNotIn("also declared", out)
        self.assertEqual(self.managed("mode", "on", "incident")[0], 0)
        self.assertTrue(self.get(self.mpath)["modes"]["incident"]["active"])
        self.assertEqual(self.managed("mode", "off", "incident")[0], 0)
        self.assertEqual(self.managed("mode", "on", "incident")[0], 0)
        code, out, _ = self.cli("mode", "off", "incident", "--scope", "global")
        self.assertIn("managed scope keeps mode incident on", out)
        self.assertIn("`incident` on (persistent)", self.cli("status")[1])
        self.managed("mode", "off", "incident")
        self.assertEqual(self.cli("mode", "on", "incident", "--scope", "global")[0], 0)
        self.assertTrue(self.get(self.gpath)["modes"]["incident"]["active"])
        self.cli("mode", "off", "incident", "--scope", "global")
        code, out, _ = self.cli("mode", "on", "incident", "--scope", "project")
        self.assertIn("a project cannot switch it on", out)
        status = self.cli("status")[1]
        self.assertIn("`incident` off · agent may enable: no · managed+global+project", status)
        self.assertIn("project state switches on mode 'incident', which the managed file declares (ignored)", status)
        self.assertIn("also declared in the managed file, which wins",
                      self.cli("mode", "declare", "incident", "--agent-may-enable")[1])
        self.assertEqual(self.cli("mode", "on", "incident", "--reason", "x", "--as-user", agent=True)[0], 3)
        self.assertEqual(self.cli("mode", "on", "incident")[0], 0)
        self.assertIn("incident", self.get(self.gpath)["sessions"]["s1"]["modes"])
        self.assertEqual(self.managed("mode", "undeclare", "incident")[0], 0)
        self.assertEqual(self.get(self.mpath)["modes"], {})

    def test_a_managed_mode_must_be_declared_in_the_managed_file(self) -> None:
        self.cli("mode", "declare", "incident")
        code, _, err = self.managed("mode", "on", "incident")
        self.assertEqual(code, 2)
        self.assertIn("not declared", err)

    def test_preset_install_and_status_labels_origins(self) -> None:
        code, out, _ = self.managed("preset", "install", "process-safety")
        self.assertEqual(code, 0)
        self.assertIn(f"installed preset process-safety into {self.mpath}", out)
        self.assertIn("rule kill-9: added (no modes: always enforced)", out)
        self.assertNotIn("always enforced", out.split("no-pkill")[1].split("\n")[0])
        self.assertIn("rule kill-9: unchanged", self.managed("preset", "install", "process-safety")[1])
        self.cli("rule", "add", "no-pkill", "--json", BARE)
        self.cli("rule", "add", "mine", "--json", BARE, "--scope", "project")
        status = self.cli("status")[1]
        for expected in ("`kill-9  ` warn · managed · always enforced", "`no-pkill` deny · managed+global · enabled",
                         "`mine    ` deny · project · enabled"):
            self.assertIn(expected, status)
        self.assertIn("### no-pkill · deny · retry same-command · managed+global\n",
                      self.cli("rule", "test", "--id", "no-pkill", "pkill a")[1])
        self.cli("disable")
        self.assertIn("**Note** the global hook is disabled (disabled by user); managed rules stay enforced",
                      self.cli("status")[1])

    def test_a_corrupt_managed_file_is_never_overwritten_and_does_not_block_the_other_scopes(self) -> None:
        self.put(self.mpath, "{nope")
        code, _, err = self.managed("rule", "add", "x", "--json", RULE)
        self.assertEqual(code, 2)
        self.assertIn("fix or remove the managed file by hand", err)
        self.assertEqual(self.mpath.read_text(), "{nope")
        for argv in (("rule", "add", "x", "--json", RULE), ("rule", "set", "x", "--json", '{"action": "warn"}'),
                     ("rule", "test", "--id", "x", "strings a"), ("mode", "declare", "m"), ("rule", "rm", "x")):
            self.assertEqual(self.cli(*argv)[0], 0, argv)

    def test_status_reports_what_is_wrong_with_the_managed_file(self) -> None:
        self.put(self.mpath, {"rules": {"bad": {"match": {"builtin": "nope"}, "message": "x"},
                                        "no-strings": json.loads(RULE)}})
        out = self.cli("status")[1]
        self.assertEqual(out.count("managed rule bad is invalid and ignored"), 1)
        self.assertIn("managed rule no-strings lists mode 'reverse-engineering', which the managed file does not declare", out)
        self.put(self.mpath, {"rules": [1]})
        self.assertIn("'rules' must be an object", self.cli("status")[1])
        self.put(self.gpath, "{nope")
        self.assertIn("the hook fails open", self.cli("status")[1])
        self.put(self.mpath, {})
        self.managed("rule", "add", "bare", "--json", BARE)
        out = self.cli("status")[1]
        self.assertIn("global and project rules are not enforced, managed rules still are", out)
        self.assertNotIn("fails open", out)

    def test_status_names_an_unreadable_or_untrusted_managed_location(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything and owns everything")
        self.put(self.mpath, {})
        self.mpath.parent.chmod(0o777)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        out = self.cli("status")[1]
        self.assertIn("is not owned by root", out)
        self.assertIn("is writable by group or others", out)
        self.mpath.parent.chmod(0)
        out = self.cli("status")[1]
        self.assertIn(f"`{self.mpath}` unreadable", out)
        self.assertIn("unreadable managed state", out)

    def test_an_unwritable_managed_target_exits_2_with_a_sudo_hint_and_writes_nothing(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        self.put(self.mpath, {"rules": {}})
        self.mpath.parent.chmod(0o555)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        for argv in (("rule", "add", "x", "--json", RULE), ("mode", "declare", "m"), ("preset", "install", "docs-first")):
            with self.subTest(argv=argv):
                code, _, err = self.cli(*argv, "--scope", "managed")
                self.assertEqual(code, 2)
                self.assertIn(f"Re-run with sudo: sudo {store.CLI} {shlex.join([*argv, '--scope', 'managed'])}", err)
        self.assertEqual(self.get(self.mpath), {"rules": {}})
        self.tmp.joinpath("ro").mkdir(mode=0o555)
        self.addCleanup(self.tmp.joinpath("ro").chmod, 0o755)
        with mock.patch.object(store, "MANAGED_PATH", self.tmp / "ro" / "sub" / "guardrails.json"):
            self.assertEqual(self.cli("rule", "add", "x", "--json", RULE, "--scope", "managed")[0], 2)
        self.assertFalse((self.tmp / "ro" / "sub").exists())


class Status(AstIsolated):
    def status(self, *extra: str) -> str:
        code, out, err = self.cli("status", *extra)
        self.assertEqual((code, err), (0, ""))
        return out

    def rule(self, **fields: object) -> str:
        return json.dumps({"match": {"program": "x"}, "message": "m", **fields})

    def test_first_line_names_the_managed_file_and_whether_it_exists(self) -> None:
        first = f"**Managed** platform file `{self.mpath}` "
        self.assertEqual(self.status().splitlines()[0],
                         first + "absent · no managed file is present, so there are no managed rules")
        self.put(self.mpath, {})
        self.assertEqual(self.status().splitlines()[0], first + "present")

    def test_rule_rows_states_and_modes(self) -> None:
        self.put(self.mpath, {"rules": {"kill-9": json.loads(self.rule(action="warn"))}})
        self.cli("rule", "add", "no-strings", "--json", self.rule(modes=["re"]))
        self.cli("rule", "add", "old-rule", "--json", self.rule(enabled=False))
        self.cli("rule", "add", "plain", "--json", self.rule())
        self.cli("mode", "declare", "re", "--agent-may-enable", "--description", "RE work")
        self.cli("mode", "declare", "incident")
        self.cli("mode", "declare", "pm")
        self.cli("mode", "on", "pm", "--scope", "global")
        self.cli("mode", "on", "re", "--session-id", "s1", "--reason", "RE")
        out = self.status("--session-id", "s1")
        self.assertIn("### Guardrails · 4 rules · hook on\n", out)
        for row in ("- `kill-9    ` warn · managed · always enforced\n", "- `no-strings` deny · global · suspended by re\n",
                    "- `old-rule  ` deny · global · disabled\n", "- `plain     ` deny · global · enabled\n",
                    "- `incident` off · agent may enable: no · global\n",
                    "- `pm      ` on (persistent) · agent may enable: no · global\n",
                    "- `re      ` on (by user: RE) · agent may enable: yes · global\n"):
            self.assertIn(row, out)

    def test_header_notes_the_hook_and_project_switches_and_empty_states(self) -> None:
        out = self.status()
        self.assertIn("**Rules**\nNo rules installed. guardrails:setup installs presets.", out)
        self.assertNotIn("**Modes**", out)
        self.cli("rule", "add", "g", "--json", self.rule())
        self.cli("disable", "--reason", "testing")
        self.put(self.ppath, {"enabled": False})
        out = self.status()
        self.assertIn("### Guardrails · 1 rule · hook off · project rules off\n", out)
        self.assertIn("**Note** the global hook is disabled (testing)\n", out)

    def test_problems_are_listed_scoped_and_neutralised(self) -> None:
        self.put(self.gpath, {"rules": {"bad": {"match": {"builtin": "nope"}, "message": "x\x1b"},
                                        "g-bad": {"match": {"program": "x"}, "message": "m", "modes": ["a"]}}})
        self.put(self.ppath, {"rules": {"p-bad": {"match": {"program": "x"}, "message": "m", "modes": ["b\nc"]}}})
        out = self.status()
        self.assertIn("**Problems**\n- ", out)
        self.assertIn("rule bad:", out)
        self.assertIn("mode 'b c'", out)
        self.assertEqual([c for c in out if c != "\n" and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")], [])
        only = self.status("--problems")
        self.assertTrue(only.startswith("**Problems**\n"))
        self.assertNotIn("### Guardrails", only)
        for scope, present, absent in (("global", "g-bad", "p-bad"), ("project", "p-bad", "g-bad")):
            for extra in ((), ("--problems",)):
                scoped = self.status("--scope", scope, *extra)
                self.assertIn(present, scoped)
                self.assertNotIn(absent, scoped)
        self.assertEqual(self.status("--scope", "managed", "--problems"), "No problems.\n")
        self.put(self.gpath, "{nope")
        self.assertIn("unreadable global", self.status("--scope", "global"))
        self.assertNotIn("unreadable global", self.status("--scope", "project"))

    def test_the_scope_filter_and_the_rule_flag(self) -> None:
        self.put(self.mpath, {"rules": {"m-rule": json.loads(self.rule())}})
        self.cli("rule", "add", "g-rule", "--json", self.rule(action="warn"))
        self.cli("rule", "add", "p-rule", "--json", self.rule(), "--scope", "project")
        self.cli("mode", "declare", "gm")
        self.cli("mode", "declare", "pm", "--scope", "project")
        for scope, rule, mode in (("global", "g-rule", "gm"), ("project", "p-rule", "pm"), ("managed", "m-rule", None)):
            with self.subTest(scope=scope):
                out = self.status("--scope", scope)
                self.assertIn("1 rule ·", out)
                self.assertIn(f"`{rule}`", out)
                self.assertEqual(f"`{mode}`" in out, mode is not None)
        self.cli("rule", "rm", "p-rule", "--scope", "project")
        self.assertIn("No rules with a project entry.", self.status("--scope", "project"))
        self.assertEqual(self.status("--rule", "g-rule"), "- `g-rule` warn · global · enabled\n")
        code, _, err = self.cli("status", "--rule", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("no rule 'ghost'", err)

    def test_long_ids_are_capped_and_state_file_text_cannot_forge_output(self) -> None:
        long = "r" * 50
        self.cli("rule", "add", long, "--json", self.rule())
        self.cli("rule", "add", "short", "--json", self.rule())
        out = self.status()
        self.assertIn(f"- `{long}` deny", out)
        self.assertIn("- `short" + " " * 35 + "` deny", out)
        forged = "bad\n### Forged\n- `fake` deny\x1b[2J"
        self.put(self.gpath, {"rules": {forged: {"match": {"program": "x"}, "message": "m"}},
                              "modes": {forged: {"description": forged, "agentMayEnable": True}},
                              "sessions": {"s1": {"modes": {forged: {"by": "agent", "reason": forged}}}}})
        for argv in (("--session-id", "s1"), ("--rule", forged)):
            out = self.status(*argv)
            self.assertNotIn("\x1b", out)
            for line in out.splitlines():
                self.assertFalse(line.startswith(("### Forged", "- `fake")), line)


class RuleTest(AstIsolated):
    DRAFT = '{"match": {"program": "strings"}, "message": "docs"}'

    def test_draft_matches_and_misses_and_installed_reports_the_effective_rule(self) -> None:
        heredoc = "cat <<'EOF' > n.md\nstrings\nEOF"
        code, out, _ = self.cli("rule", "test", "--json", self.DRAFT, "sudo strings x", "bash -c 'strings a'",
                                "man strings", "echo strings", heredoc)
        self.assertEqual(code, 0)
        self.assertIn("### new-rule · deny · global", out)
        self.assertIn("**Message** docs", out)
        self.assertEqual(caught(out), {"sudo strings x": True, "bash -c 'strings a'": True, "man strings": False,
                                       "echo strings": False, "cat <<'EOF' > n.md⏎strings⏎EOF": False})
        self.cli("rule", "add", "no-strings", "--json", RULE.replace("deny", "warn"))
        self.cli("rule", "set", "no-strings", "--json", '{"action": "deny"}', "--scope", "project")
        code, out, _ = self.cli("rule", "test", "--id", "no-strings", "strings a")
        self.assertIn("### no-strings · deny · retry same-command · global+project", out)
        self.assertEqual(caught(out), {"strings a": True})

    def test_bad_input_exits_2(self) -> None:
        for argv in (("--json", '{"match": {}, "message": "m"}', "echo hi"), ("--json", "{", "echo hi"),
                     ("--id", "ghost", "echo hi"), ("--json", self.DRAFT), ("--json", self.DRAFT, "--id-name", "bad id ### x", "ls"),
                     ("--json", self.DRAFT, "  "), ("--json", self.DRAFT, "--examples", '[{"cmd": " "}]'),
                     ("--json", self.DRAFT, "--examples", '[" "]'), ("--json", "-", "--examples", "-")):
            with self.subTest(argv=argv):
                code, out, _ = self.cli("rule", "test", *argv)
                self.assertEqual((code, out), (2, ""))
        self.cli("rule", "add", "r", "--json", self.DRAFT)
        for label in ("--scope", "--id-name"):
            self.assertEqual(self.cli("rule", "test", "--id", "r", label, "project", "ls")[0], 2)

    def test_examples_forms_and_errors(self) -> None:
        def run(*argv: str) -> tuple[int, str, str]:
            return self.cli("rule", "test", "--json", self.DRAFT, *argv)

        code, out, _ = run("strings a", "ls", "--source", "yours", "--examples",
                           json.dumps([{"cmd": "strings b", "source": "you chose", "expect": "pass"}, "strings c"]))
        self.assertEqual(code, 0)
        self.assertEqual([r.split("`")[1].rstrip() for r in out.splitlines() if r.startswith(("- ✗", "- ✓"))],
                         ["strings a", "strings b", "strings c", "ls"])
        self.assertEqual(out.count(" yours\n"), 2)
        self.assertIn("you chose", out)
        path = self.tmp / "ex file.json"
        path.write_text(json.dumps([{"cmd": "strings a", "source": "yours"}]))
        from_file = run("--examples", f"@{path}")[1]
        with stdin(path.read_text()):
            self.assertEqual(run("--examples", "-")[1], from_file)
        for bad, fragment in (("{", "not valid JSON"), ('{"cmd": "x"}', "JSON list"), ('[{"source": "yours"}]', "'cmd'"),
                              ('[{"cmd": "x", "source": "me"}]', "source must be"),
                              ('[{"cmd": "x", "expect": "block"}]', "expect must be"),
                              ('[{"cmd": "x", "expects": "match"}]', "unknown keys"), ("[]", "at least one"),
                              ("@/no/such/file.json", "cannot read")):
            code, out, err = run("--examples", bad)
            self.assertEqual((code, out), (2, ""), bad)
            self.assertIn(fragment, err)
        self.assertIn("at least one command", run()[2])

    def test_the_envelope_carries_rule_and_examples_through_one_stdin(self) -> None:
        document = json.dumps({"rule": json.loads(self.DRAFT), "examples": [
            {"cmd": "strings a", "source": "yours", "expect": "match"}, {"cmd": "ls", "expect": "pass"}]})
        with stdin(document):
            code, out, _ = self.cli("rule", "test", "--json", "-", "--id-name", "no-strings")
        self.assertEqual(code, 0)
        self.assertIn("2 commands, 0 mismatches", out)
        with stdin(document):
            self.assertEqual(self.cli("rule", "add", "no-strings", "--json", "-")[0], 0)
        stored = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual(("examples" in stored, stored["match"]), (False, {"program": "strings"}))
        with stdin(json.dumps({"rule": json.loads(self.DRAFT), "examples": "x"})):
            self.assertEqual(self.cli("rule", "test", "--json", "-")[0], 2)

    def test_json_comes_from_a_literal_a_file_with_spaces_and_home_or_stdin(self) -> None:
        spaced = self.tmp / "my rules" / "rule.json"
        self.put(spaced, self.DRAFT)
        self.assertEqual(self.cli("rule", "add", "a", "--json", f"@{spaced}")[0], 0)
        with stdin(self.DRAFT):
            self.assertEqual(self.cli("rule", "add", "b", "--json", "-")[0], 0)
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "my rules")}):
            self.assertEqual(self.cli("rule", "add", "c", "--json", "@~/rule.json")[0], 0)
        for rid in "abc":
            self.assertEqual(self.get(self.gpath)["rules"][rid]["message"], "docs")
        code, out, _ = self.cli("rule", "test", "--json", f"@{spaced}", "strings x", "ls")
        self.assertIn("- ✗ `strings x` inferred", out)
        hard = {"match": {"regex": "echo `pkill $(x)` \"y\" 'z'"}, "message": "it's \"hard\""}
        self.put(self.tmp / "hard.json", json.dumps(hard))
        self.assertEqual(self.cli("rule", "add", "hard", "--json", f"@{self.tmp / 'hard.json'}")[0], 0)
        stored = self.get(self.gpath)["rules"]["hard"]
        self.assertEqual((stored["match"], stored["message"]), (hard["match"], hard["message"]))
        self.put(self.tmp / "set.json", json.dumps({"message": "It's `new`", "program": ["strings", "otool"], "regex": "a'b"}))
        self.assertEqual(self.cli("rule", "set", "a", "--json", f"@{self.tmp / 'set.json'}")[0], 0)
        rule = self.get(self.gpath)["rules"]["a"]
        self.assertEqual((rule["message"], rule["match"]), ("It's `new`", {"program": ["strings", "otool"], "regex": "a'b"}))
        with stdin('{"action": "warn"}'):
            self.assertEqual(self.cli("rule", "set", "a", "--json", "-")[0], 0)
        bad = self.tmp / "bad.json"
        self.put(bad, "{nope")
        for verb in (("rule", "add", "x"), ("rule", "test", "ls")):
            code, _, err = self.cli(*verb, "--json", f"@{self.tmp / 'missing.json'}")
            self.assertEqual(code, 2)
            self.assertIn("--json: cannot read", err)
            code, _, err = self.cli(*verb, "--json", f"@{bad}")
            self.assertEqual(code, 2)
            self.assertIn("not valid JSON", err)

    def test_notes_say_when_the_hook_would_not_act_on_a_match(self) -> None:
        def notes(*argv: str) -> str:
            code, out, _ = self.cli("rule", "test", *argv, "pkill x")
            self.assertEqual(code, 0)
            return out

        draft = '{"match": {"program": "pkill"}, "message": "m"'
        self.assertNotIn("**Note**", notes("--json", draft + "}"))
        self.assertIn("**Note** rule is disabled", notes("--json", draft + ', "enabled": false}'))
        self.assertIn("none of no-such-bin-xyz is installed here", notes("--json", draft + ', "requires": ["no-such-bin-xyz"]}'))
        self.cli("rule", "add", "r", "--json", draft + ', "modes": ["m"]}')
        self.cli("mode", "declare", "m")
        self.assertIn("suspends this rule while mode m is active (none is now)", notes("--id", "r"))
        self.cli("mode", "on", "m", "--session-id", "s1")
        out = notes("--id", "r", "--session-id", "s1")
        self.assertIn("mode m is active, so the hook suspends this rule right now", out)
        self.assertEqual(caught(out), {"pkill x": True})
        self.cli("rule", "add", "g", "--json", draft + "}")
        self.cli("rule", "add", "p", "--json", draft + "}", "--scope", "project")
        self.cli("disable")
        self.assertIn("the global hook is disabled", notes("--id", "g"))
        self.assertIn("the global hook is disabled", notes("--id", "p"))
        self.cli("rule", "add", "m", "--json", draft + "}", "--scope", "managed")
        self.assertNotIn("hook is disabled", notes("--id", "m"))
        self.cli("enable")
        self.put(self.ppath, {**self.get(self.ppath), "enabled": False})
        code, _, err = self.cli("rule", "test", "--id", "p", "pkill x")
        self.assertEqual(code, 2)
        self.assertIn("project rules are disabled, so project entries are not loaded", err)
        self.assertNotIn("hook is disabled", notes("--id", "g"))
