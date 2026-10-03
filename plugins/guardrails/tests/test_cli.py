from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path

import store
from helpers import AstIsolated, caught

RULE = ('{"match": {"program": "strings"}, "message": "Read the docs.", "retry": "same-command", '
        '"modes": ["reverse-engineering"]}')


class RuleCommands(AstIsolated):
    def test_add_set_rm_global(self) -> None:
        code, out, _ = self.cli("rule", "add", "no-strings", "--json", RULE, "--reason", "user asked")
        self.assertEqual(code, 0)
        self.assertIn("added rule no-strings", out)
        rule = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual((rule["setBy"]["by"], rule["setBy"]["reason"]), ("user", "user asked"))
        self.assertEqual(self.set_rule("no-strings", {"action": "warn", "program": ["strings", "otool"]})[0], 0)
        rule = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual((rule["action"], rule["match"]["program"]), ("warn", ["strings", "otool"]))
        self.assertEqual(self.set_rule("no-strings", {"messageShort": "short", "requires": ["fd", "fdfind"]})[0], 0)
        rule = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual((rule["messageShort"], rule["requires"]), ("short", ["fd", "fdfind"]))
        self.assertEqual(self.set_rule("no-strings", {"messageShort": None})[0], 0)
        self.assertNotIn("messageShort", self.get(self.gpath)["rules"]["no-strings"])
        self.assertEqual(self.cli("rule", "rm", "no-strings")[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"], {})

    def test_add_rejects_invalid(self) -> None:
        code, _, err = self.cli("rule", "add", "x", "--json", '{"match": {}, "message": "m"}')
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertEqual(self.cli("rule", "add", "x", "--json", "{")[0], 2)
        self.assertEqual(self.cli("rule", "add", "bad id", "--json", RULE)[0], 2)
        self.assertFalse(self.gpath.exists())

    def test_set_rejects_unknown_key_and_invalid_result(self) -> None:
        self.cli("rule", "add", "no-strings", "--json", RULE)
        for fields in ({"colour": "red"}, {"regex": "("}, {"enabled": "maybe"}, {"modes": [1]}, {"message": 3}, [1]):
            with self.subTest(fields=fields):
                self.assertEqual(self.set_rule("no-strings", fields)[0], 2)
        self.assertNotIn("regex", self.get(self.gpath)["rules"]["no-strings"]["match"])

    def test_unknown_rule(self) -> None:
        self.assertEqual(self.set_rule("ghost", {"action": "deny"})[0], 2)
        self.assertEqual(self.cli("rule", "rm", "ghost")[0], 2)

    def test_project_add_goes_to_project_file(self) -> None:
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE, "--scope", "project")[0], 0)
        self.assertIn("no-strings", self.get(self.ppath)["rules"])
        self.assertFalse(self.gpath.exists())

    def test_project_override_of_global_rule(self) -> None:
        self.cli("rule", "add", "no-strings", "--json", RULE)
        code, out, _ = self.set_rule("no-strings", {"action": "deny", "enabled": False, "args": "-a"},
                                     "--scope", "project")
        self.assertEqual(code, 0)
        entry = self.get(self.ppath)["rules"]["no-strings"]
        self.assertEqual(entry["action"], "deny")
        self.assertEqual(entry["match"], {"args": "-a"})
        self.assertNotIn("message", entry)
        self.assertIn("enabled=false", out)
        self.assertIn('args="-a"', out)

    def test_project_flag_outside_project(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE, "--scope", "project")[0], 2)

    def test_corrupt_state_is_not_overwritten(self) -> None:
        self.put(self.gpath, "{nope")
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE)[0], 2)
        self.assertEqual(self.gpath.read_text(), "{nope")


class Gate(AstIsolated):
    def test_agent_needs_as_user_for_rule_changes(self) -> None:
        code, _, err = self.cli("rule", "add", "no-strings", "--json", RULE, agent=True)
        self.assertEqual(code, 3)
        self.assertIn("--as-user", err)
        self.assertFalse(self.gpath.exists())
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE, "--as-user", agent=True)[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["no-strings"]["setBy"]["by"], "agent")
        self.assertEqual(self.cli("rule", "rm", "no-strings", agent=True)[0], 3)

    def test_agent_cannot_enable_or_disable_hook(self) -> None:
        for verb in ("enable", "disable"):
            with self.subTest(verb=verb):
                self.assertEqual(self.cli(verb, agent=True)[0], 3)
                self.assertEqual(self.cli(verb, "--as-user", agent=True)[0], 3)
        self.assertEqual(self.cli("disable", "--reason", "noisy")[0], 0)
        state = self.get(self.gpath)
        self.assertEqual((state["enabled"], state["disabledReason"]), (False, "noisy"))
        self.assertEqual(state["setBy"]["by"], "user")
        self.assertEqual(self.cli("enable")[0], 0)
        state = self.get(self.gpath)
        self.assertTrue(state["enabled"])
        self.assertNotIn("disabledReason", state)

    def test_project_disable_only_touches_project(self) -> None:
        self.assertEqual(self.cli("disable", "--scope", "project")[0], 0)
        self.assertFalse(self.get(self.ppath)["enabled"])
        self.assertFalse(self.gpath.exists())

    def test_read_only_verbs_allowed_for_agents(self) -> None:
        self.assertEqual(self.cli("status", agent=True)[0], 0)
        self.assertEqual(self.cli("preset", "list", agent=True)[0], 0)


class ModeCommands(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"

    def declare(self, *extra: str) -> int:
        return self.cli("mode", "declare", "reverse-engineering", "--description", "RE", *extra)[0]

    def test_declare_and_undeclare(self) -> None:
        self.assertEqual(self.declare("--agent-may-enable"), 0)
        mode = self.get(self.gpath)["modes"]["reverse-engineering"]
        self.assertEqual((mode["description"], mode["agentMayEnable"], mode["active"]), ("RE", True, False))
        self.assertEqual(self.cli("mode", "undeclare", "reverse-engineering")[0], 0)
        self.assertEqual(self.get(self.gpath)["modes"], {})
        self.assertEqual(self.cli("mode", "undeclare", "reverse-engineering")[0], 2)

    def test_declare_requires_user(self) -> None:
        self.assertEqual(self.cli("mode", "declare", "x", agent=True)[0], 3)
        self.assertEqual(self.cli("mode", "declare", "x", "--as-user", agent=True)[0], 0)

    def test_redeclare_keeps_active_and_description(self) -> None:
        self.declare()
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--scope", "global")[0], 0)
        self.assertEqual(self.cli("mode", "declare", "reverse-engineering", "--agent-may-enable")[0], 0)
        mode = self.get(self.gpath)["modes"]["reverse-engineering"]
        self.assertEqual((mode["active"], mode["description"], mode["agentMayEnable"]), (True, "RE", True))

    def test_agent_enables_session_mode_with_reason(self) -> None:
        self.declare("--agent-may-enable")
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", agent=True)[0], 2)
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--reason", "user said RE", agent=True)[0], 0)
        record = self.get(self.gpath)["sessions"]["s1"]["modes"]["reverse-engineering"]
        self.assertEqual((record["by"], record["reason"]), ("agent", "user said RE"))
        self.assertEqual(self.cli("mode", "off", "reverse-engineering", agent=True)[0], 0)
        self.assertNotIn("reverse-engineering", self.get(self.gpath)["sessions"]["s1"]["modes"])

    def test_agent_refused_for_modes_without_permission(self) -> None:
        self.declare()
        code = self.cli("mode", "on", "reverse-engineering", "--reason", "x", "--as-user", agent=True)[0]
        self.assertEqual(code, 3)
        self.assertEqual(self.cli("mode", "on", "reverse-engineering")[0], 0)

    def test_project_can_forbid_agent_enable(self) -> None:
        self.declare("--agent-may-enable")
        self.assertEqual(self.cli("mode", "declare", "reverse-engineering", "--scope", "project")[0], 0)
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--reason", "x", agent=True)[0], 3)

    def test_undeclared_mode(self) -> None:
        self.assertEqual(self.cli("mode", "on", "ghost")[0], 2)

    def test_session_id_required(self) -> None:
        self.declare()
        del os.environ["CLAUDE_CODE_SESSION_ID"]
        self.assertEqual(self.cli("mode", "on", "reverse-engineering")[0], 2)
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--session-id", "s9")[0], 0)
        self.assertIn("s9", self.get(self.gpath)["sessions"])

    def test_persistent_scopes_are_user_only(self) -> None:
        self.declare("--agent-may-enable")
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--scope", "global", agent=True)[0], 3)
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--scope", "global", "--as-user",
                                  agent=True)[0], 0)
        self.assertTrue(self.get(self.gpath)["modes"]["reverse-engineering"]["active"])
        self.assertEqual(self.cli("mode", "off", "reverse-engineering", "--scope", "global")[0], 0)
        self.assertFalse(self.get(self.gpath)["modes"]["reverse-engineering"]["active"])
        self.assertEqual(self.cli("mode", "declare", "reverse-engineering", "--scope", "project")[0], 0)
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--scope", "project")[0], 0)
        self.assertTrue(self.get(self.ppath)["modes"]["reverse-engineering"]["active"])

    def test_persistent_scope_needs_declaration_somewhere(self) -> None:
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--scope", "project")[0], 2)

    def test_persistent_scope_project_stubs_a_globally_declared_mode(self) -> None:
        self.declare("--agent-may-enable")
        self.assertEqual(self.cli("mode", "on", "reverse-engineering", "--scope", "project", "--as-user",
                                  agent=True)[0], 0)
        mode = self.get(self.ppath)["modes"]["reverse-engineering"]
        self.assertTrue(mode["active"])
        self.assertNotIn("agentMayEnable", mode)
        self.assertEqual(mode["setBy"]["by"], "agent")
        self.assertIn("agent may enable: yes", self.cli("status")[1])
        self.assertEqual(self.cli("mode", "off", "reverse-engineering", "--scope", "project")[0], 0)
        self.assertFalse(self.get(self.ppath)["modes"]["reverse-engineering"]["active"])

    def test_session_mode_suspends_rule_end_to_end(self) -> None:
        self.cli("rule", "add", "no-strings", "--json", RULE)
        self.declare("--agent-may-enable")
        self.cli("mode", "on", "reverse-engineering", "--reason", "user: RE libfoo", agent=True)
        out = self.hook("strings a", session="s1")
        assert out is not None
        self.assertIn("suspended by mode reverse-engineering", out["systemMessage"])


class Status(AstIsolated):
    def test_lists_rules_modes_and_problems(self) -> None:
        self.put(self.gpath, {"rules": {"no-strings": json.loads(RULE),
                                        "bad": {"match": {"builtin": "nope"}, "message": "x"}},
                              "modes": {"reverse-engineering": {"description": "RE", "agentMayEnable": True}}})
        self.put(self.ppath, {"rules": {"nm": {"match": {"program": "nm"}, "message": "m", "modes": ["ghost"]}}})
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        for expected in ("`no-strings` deny · global · enabled", "`nm        ` deny · project · enabled",
                         "`reverse-engineering` off · agent may enable: yes · global", "rule bad:",
                         "mode 'ghost' is not declared"):
            self.assertIn(expected, out)


class RuleTest(AstIsolated):
    DRAFT = '{"match": {"program": "strings"}, "message": "docs"}'

    def test_draft_matches_and_misses(self) -> None:
        heredoc = "cat <<'EOF' > n.md\nstrings\nEOF"
        code, out, _ = self.cli(
            "rule", "test", "--json", self.DRAFT,
            "sudo strings x", "bash -c 'strings a'", "man strings", "echo strings", heredoc,
        )
        self.assertEqual(code, 0)
        self.assertIn("### new-rule · deny · global", out)
        self.assertEqual(caught(out), {"sudo strings x": True, "bash -c 'strings a'": True, "man strings": False,
                                       "echo strings": False, "cat <<'EOF' > n.md⏎strings⏎EOF": False})
        self.assertIn("**Message** docs", out)

    def test_draft_invalid_rule(self) -> None:
        code, _, err = self.cli("rule", "test", "--json", '{"match": {}, "message": "m"}', "echo hi")
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_draft_invalid_json(self) -> None:
        self.assertEqual(self.cli("rule", "test", "--json", "{", "echo hi")[0], 2)

    def test_installed_reports_effective_rule(self) -> None:
        self.cli("rule", "add", "no-strings", "--json",
                 '{"match": {"program": "strings"}, "message": "docs", "action": "warn", '
                 '"retry": "same-command", "modes": ["reverse-engineering"]}')
        self.cli("rule", "set", "no-strings", "--json", '{"action": "deny"}', "--scope", "project")
        code, out, _ = self.cli("rule", "test", "--id", "no-strings", "strings a")
        self.assertEqual(code, 0)
        self.assertIn("### no-strings · deny · retry same-command · global+project", out)
        self.assertEqual(caught(out), {"strings a": True})

    def test_unknown_id(self) -> None:
        self.assertEqual(self.cli("rule", "test", "--id", "ghost", "echo hi")[0], 2)

    def test_agent_allowed_without_as_user(self) -> None:
        self.assertEqual(self.cli("rule", "test", "--json", self.DRAFT, "echo hi", agent=True)[0], 0)

    def test_is_read_only(self) -> None:
        self.cli("rule", "test", "--json", self.DRAFT, "echo hi")
        self.assertFalse(self.gpath.exists())


MANAGED_RULE = '{"match": {"program": "pkill"}, "message": "No pkill."}'


class ManagedScope(AstIsolated):
    def managed(self, *argv: str, agent: bool = False) -> tuple[int, str, str]:
        return self.cli(*argv, "--scope", "managed", agent=agent)

    def test_add_set_rm_round_trip(self) -> None:
        code, out, _ = self.managed("rule", "add", "no-pkill", "--json", RULE, "--reason", "policy")
        self.assertEqual(code, 0)
        self.assertIn(f"added rule no-pkill in {self.mpath}", out)
        self.assertFalse(self.gpath.exists())
        self.assertEqual(stat.S_IMODE(self.mpath.stat().st_mode), 0o644)
        self.assertEqual(self.get(self.mpath)["rules"]["no-pkill"]["setBy"]["reason"], "policy")
        self.assertEqual(self.managed("rule", "set", "no-pkill", "--json", '{"action": "warn", "modes": []}')[0], 0)
        rule = self.get(self.mpath)["rules"]["no-pkill"]
        self.assertEqual((rule["action"], rule["modes"]), ("warn", []))
        self.assertEqual(self.managed("rule", "rm", "no-pkill")[0], 0)
        self.assertEqual(self.get(self.mpath)["rules"], {})

    def test_creates_missing_directory(self) -> None:
        self.assertFalse(self.mpath.parent.exists())
        self.assertEqual(self.managed("rule", "add", "x", "--json", RULE)[0], 0)
        self.assertTrue(self.mpath.is_file())

    def test_set_and_rm_need_an_existing_managed_rule(self) -> None:
        self.assertEqual(self.managed("rule", "set", "ghost", "--json", '{"action": "deny"}')[0], 2)
        self.assertEqual(self.managed("rule", "rm", "ghost")[0], 2)

    def test_always_enforced_note_only_without_modes(self) -> None:
        _, out, _ = self.managed("rule", "add", "bare", "--json", MANAGED_RULE)
        self.assertIn("note: managed rule bare lists no modes, so it is always enforced", out)
        _, out, _ = self.managed("rule", "add", "modal", "--json", RULE)
        self.assertNotIn("always enforced", out)
        _, out, _ = self.managed("rule", "set", "modal", "--json", '{"modes": []}')
        self.assertIn("always enforced", out)
        _, out, _ = self.cli("rule", "add", "local", "--json", MANAGED_RULE)
        self.assertNotIn("always enforced", out)

    def test_set_and_rm_refused_from_other_scopes(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        before = self.mpath.read_text()
        for scope in ("global", "project"):
            for argv in (("rule", "set", "no-strings", "--json", '{"action": "warn"}'), ("rule", "rm", "no-strings")):
                with self.subTest(scope=scope, verb=argv[1]):
                    code, _, err = self.cli(*argv, "--scope", scope)
                    self.assertEqual(code, 3)
                    self.assertIn("--scope managed", err)
        self.assertEqual(self.cli("rule", "set", "no-strings", "--json", '{"action": "warn"}', "--scope", "project")[0], 3)
        self.assertEqual(self.mpath.read_text(), before)
        self.assertFalse(self.gpath.exists())
        self.assertFalse(self.ppath.exists())

    def test_add_from_other_scope_notes_it_can_only_tighten(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        code, out, _ = self.cli("rule", "add", "no-strings", "--json", RULE, "--scope", "project")
        self.assertEqual(code, 0)
        self.assertIn("also a managed rule", out)

    def test_scope_flag_selects_global_and_project(self) -> None:
        self.assertEqual(self.cli("rule", "add", "g", "--json", RULE, "--scope", "global")[0], 0)
        self.assertEqual(self.cli("rule", "add", "p", "--json", RULE, "--scope", "project")[0], 0)
        self.assertEqual(list(self.get(self.gpath)["rules"]), ["g"])
        self.assertEqual(list(self.get(self.ppath)["rules"]), ["p"])
        self.assertFalse(self.mpath.exists())

    def test_default_scope_is_never_managed(self) -> None:
        self.cli("rule", "add", "g", "--json", RULE)
        self.cli("mode", "declare", "m")
        self.cli("preset", "install", "docs-first")
        self.assertFalse(self.mpath.exists())

    def test_agent_still_needs_as_user(self) -> None:
        code, _, err = self.managed("rule", "add", "x", "--json", RULE, agent=True)
        self.assertEqual(code, 3)
        self.assertIn("--as-user", err)
        self.assertFalse(self.mpath.exists())
        self.assertEqual(self.managed("rule", "add", "x", "--json", RULE, "--as-user", agent=True)[0], 0)
        self.assertEqual(self.get(self.mpath)["rules"]["x"]["setBy"]["by"], "agent")

    def test_mode_declare_activate_and_undeclare(self) -> None:
        code, out, _ = self.managed("mode", "declare", "incident", "--description", "firefighting")
        self.assertEqual(code, 0)
        self.assertIn(str(self.mpath), out)
        mode = self.get(self.mpath)["modes"]["incident"]
        self.assertEqual((mode["agentMayEnable"], mode["active"]), (False, False))
        self.assertEqual(self.managed("mode", "on", "incident")[0], 0)
        self.assertTrue(self.get(self.mpath)["modes"]["incident"]["active"])
        self.assertEqual(self.managed("mode", "off", "incident")[0], 0)
        self.assertFalse(self.get(self.mpath)["modes"]["incident"]["active"])
        self.assertEqual(self.managed("mode", "undeclare", "incident")[0], 0)
        self.assertEqual(self.get(self.mpath)["modes"], {})

    def test_mode_on_managed_needs_managed_declaration(self) -> None:
        self.cli("mode", "declare", "incident")
        code, _, err = self.managed("mode", "on", "incident")
        self.assertEqual(code, 2)
        self.assertIn("not declared", err)

    def test_global_can_switch_a_managed_mode_on_but_not_off_the_managed_activation(self) -> None:
        self.managed("mode", "declare", "incident")
        self.assertEqual(self.cli("mode", "on", "incident", "--scope", "global")[0], 0)
        self.assertTrue(self.get(self.gpath)["modes"]["incident"]["active"])
        self.managed("mode", "on", "incident")
        code, out, _ = self.cli("mode", "off", "incident", "--scope", "global")
        self.assertEqual(code, 0)
        self.assertIn("managed scope keeps mode incident on", out)
        self.assertIn("`incident` on (persistent)", self.cli("status")[1])

    def test_project_cannot_switch_a_managed_mode_on(self) -> None:
        self.managed("mode", "declare", "incident")
        code, out, _ = self.cli("mode", "on", "incident", "--scope", "project")
        self.assertEqual(code, 0)
        self.assertIn("a project cannot switch it on", out)
        status = self.cli("status")[1]
        self.assertIn("`incident` off · agent may enable: no · managed+project", status)
        self.assertIn("project state switches on mode 'incident', which the managed file declares (ignored)", status)

    def test_project_can_switch_on_its_own_and_global_modes(self) -> None:
        self.cli("mode", "declare", "mine")
        self.assertEqual(self.cli("mode", "on", "mine", "--scope", "project")[0], 0)
        self.assertIn("`mine` on (persistent) · agent may enable: no · global+project", self.cli("status")[1])

    def test_declare_notes_when_managed_declares_the_mode(self) -> None:
        self.managed("mode", "declare", "incident")
        out = self.cli("mode", "declare", "incident", "--agent-may-enable")[1]
        self.assertIn("also declared in the managed file, which wins", out)
        self.assertNotIn("also declared", self.managed("mode", "declare", "incident")[1])
        self.assertNotIn("also declared", self.cli("mode", "declare", "other")[1])

    def test_session_mode_on_uses_managed_declaration_and_agent_lock(self) -> None:
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
        self.managed("mode", "declare", "incident")
        code, _, _ = self.cli("mode", "on", "incident", "--reason", "x", "--as-user", agent=True)
        self.assertEqual(code, 3)
        self.assertEqual(self.cli("mode", "on", "incident")[0], 0)
        self.assertIn("incident", self.get(self.gpath)["sessions"]["s1"]["modes"])

    def test_preset_install(self) -> None:
        code, out, _ = self.managed("preset", "install", "process-safety")
        self.assertEqual(code, 0)
        self.assertIn(f"installed preset process-safety into {self.mpath}", out)
        self.assertIn("rule no-pkill: added", out)
        self.assertNotIn("always enforced", out.split("no-pkill")[1].split("\n")[0])
        self.assertIn("rule kill-9: added (no modes: always enforced)", out)
        state = self.get(self.mpath)
        self.assertEqual(sorted(state["rules"]), ["kill-9", "no-pkill"])
        self.assertIn("incident", state["modes"])
        self.assertFalse(self.gpath.exists())
        self.assertIn("rule kill-9: unchanged", self.managed("preset", "install", "process-safety")[1])

    def test_status_labels_origins(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        self.managed("rule", "add", "bare", "--json", MANAGED_RULE)
        self.managed("mode", "declare", "reverse-engineering")
        self.cli("rule", "add", "no-strings", "--json", RULE)
        self.cli("rule", "add", "mine", "--json", MANAGED_RULE, "--scope", "project")
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        for expected in ("`no-strings` deny · managed+global · enabled", "`bare      ` deny · managed · always enforced",
                         "`mine      ` deny · project · enabled",
                         "`reverse-engineering` off · agent may enable: no · managed"):
            self.assertIn(expected, out)

    def test_status_reports_unreadable_managed_file(self) -> None:
        self.put(self.mpath, "{nope")
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("unreadable managed state, so its rules are NOT enforced until it is fixed", out)
        self.assertIn("fix or remove the file by hand", out)

    def test_status_reports_invalid_managed_rule(self) -> None:
        self.put(self.mpath, {"rules": {"bad": {"match": {"builtin": "nope"}, "message": "x"}}})
        out = self.cli("status")[1]
        self.assertEqual(out.count("managed rule bad is invalid and ignored"), 1)
        self.assertNotIn("rule bad: ", out)

    def test_status_notes_managed_rules_survive_disabled_hook(self) -> None:
        self.managed("rule", "add", "bare", "--json", MANAGED_RULE)
        self.cli("disable")
        self.assertIn("**Note** the global hook is disabled (disabled by user); managed rules stay enforced",
                      self.cli("status")[1])

    def test_rule_test_shows_origin(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        out = self.cli("rule", "test", "--id", "no-strings", "strings a")[1]
        self.assertIn("### no-strings · deny · retry same-command · managed\n", out)

    def test_corrupt_managed_file_is_not_overwritten_and_says_how_to_fix_it(self) -> None:
        self.put(self.mpath, "{nope")
        code, _, err = self.managed("rule", "add", "x", "--json", RULE)
        self.assertEqual(code, 2)
        self.assertIn("fix or remove the managed file by hand", err)
        self.assertEqual(self.mpath.read_text(), "{nope")

    def test_corrupt_managed_file_does_not_block_other_scopes(self) -> None:
        self.put(self.mpath, "{nope")
        self.assertEqual(self.cli("rule", "add", "x", "--json", RULE)[0], 0)
        self.assertEqual(self.cli("rule", "set", "x", "--json", '{"action": "warn"}')[0], 0)
        self.assertEqual(self.cli("rule", "test", "--id", "x", "strings a")[0], 0)
        self.assertEqual(self.cli("mode", "declare", "m")[0], 0)
        self.assertEqual(self.cli("rule", "rm", "x")[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"], {})

    def test_set_and_rm_work_on_the_users_own_override_of_a_managed_id(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE, "--scope", "project")[0], 0)
        self.assertEqual(self.cli("rule", "set", "no-strings", "--json", '{"message": "mine"}', "--scope", "project")[0], 0)
        self.assertEqual(self.get(self.ppath)["rules"]["no-strings"]["message"], "mine")
        self.assertEqual(self.cli("rule", "rm", "no-strings", "--scope", "project")[0], 0)
        self.assertEqual(self.get(self.ppath)["rules"], {})
        self.assertIn("no-strings", self.get(self.mpath)["rules"])
        self.assertEqual(self.cli("rule", "rm", "no-strings", "--scope", "project")[0], 3)

    def test_global_override_of_a_managed_id_can_be_removed_and_set(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        self.put(self.gpath, {"rules": {"no-strings": {**json.loads(RULE), "action": "warn"}}})
        self.assertEqual(self.cli("rule", "set", "no-strings", "--json", '{"action": "deny"}')[0], 0)
        self.assertEqual(self.cli("rule", "rm", "no-strings")[0], 0)
        self.assertEqual(self.cli("rule", "rm", "no-strings")[0], 3)

    def test_managed_texts_cannot_be_reworded_from_other_scopes(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        self.cli("rule", "add", "no-strings", "--json", RULE.replace("Read", "Ignore"))
        self.assertIn("Read the docs", self.cli("rule", "test", "--id", "no-strings", "strings a")[1])

    def test_status_reports_undeclared_managed_mode(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        out = self.cli("status")[1]
        self.assertIn("managed rule no-strings lists mode 'reverse-engineering', which the managed file does not "
                      "declare", out)

    def test_status_reports_wrong_shape_managed_file(self) -> None:
        self.put(self.mpath, {"rules": [1]})
        self.assertIn("'rules' must be an object", self.cli("status")[1])

    def test_status_reports_unreadable_parent_directory(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        self.put(self.mpath, {})
        self.mpath.parent.chmod(0)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        out = self.cli("status")[1]
        self.assertIn(f"override `{self.mpath}` unreadable", out)
        self.assertIn("unreadable managed state", out)

    def test_status_warns_about_untrusted_managed_location(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("everything is owned by root")
        self.put(self.mpath, {})
        self.mpath.parent.chmod(0o777)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        out = self.cli("status")[1]
        self.assertIn("is not owned by root", out)
        self.assertIn("is writable by group or others", out)

    def test_status_wording_when_global_is_corrupt(self) -> None:
        self.put(self.gpath, "{nope")
        self.assertIn("the hook fails open", self.cli("status")[1])
        self.managed("rule", "add", "bare", "--json", MANAGED_RULE)
        out = self.cli("status")[1]
        self.assertIn("global and project rules are not enforced, managed rules still are", out)
        self.assertNotIn("fails open", out)

    def test_writes_go_to_the_override_when_set_else_the_default(self) -> None:
        self.assertEqual(self.managed("rule", "add", "a", "--json", RULE)[0], 0)
        self.assertTrue(self.mpath.is_file())
        self.assertFalse(self.dpath.exists())
        del os.environ["GUARDRAILS_MANAGED_PATH"]
        self.assertEqual(self.managed("rule", "add", "b", "--json", RULE)[0], 0)
        self.assertEqual(list(self.get(self.dpath)["rules"]), ["b"])
        self.assertEqual(list(self.get(self.mpath)["rules"]), ["a"])

    def test_override_and_default_are_both_effective(self) -> None:
        self.put(self.dpath, {"rules": {"d": json.loads(RULE)}})
        self.managed("rule", "add", "o", "--json", RULE)
        out = self.cli("status")[1]
        self.assertIn("`d` deny · managed", out)
        self.assertIn("`o` deny · managed", out)

    def test_creating_the_managed_directory_ignores_umask(self) -> None:
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        self.assertEqual(self.managed("rule", "add", "x", "--json", RULE)[0], 0)
        self.assertEqual(stat.S_IMODE(self.mpath.parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.mpath.stat().st_mode), 0o644)


class ManagedNotWritable(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")

    def lock_down(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o555)
        self.addCleanup(path.chmod, 0o755)

    def assert_sudo_hint(self, err: str, *argv: str) -> None:
        self.assertIn("error:", err)
        self.assertIn("not writable", err)
        self.assertIn(f"Re-run with sudo: sudo {store.CLI} ", err)
        self.assertIn(shlex.join(argv), err)

    def test_absent_file_in_read_only_directory(self) -> None:
        self.lock_down(self.tmp / "ro")
        os.environ["GUARDRAILS_MANAGED_PATH"] = str(self.tmp / "ro" / "sub" / "guardrails.json")
        argv = ("rule", "add", "x", "--json", RULE, "--scope", "managed")
        code, _, err = self.cli(*argv)
        self.assertEqual(code, 2)
        self.assert_sudo_hint(err, *argv)
        self.assertFalse((self.tmp / "ro" / "sub").exists())

    def test_existing_file_in_read_only_directory_is_untouched(self) -> None:
        self.put(self.mpath, {"rules": {}})
        self.mpath.parent.chmod(0o555)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        for argv in (("rule", "add", "x", "--json", RULE), ("mode", "declare", "m"), ("preset", "install", "docs-first")):
            with self.subTest(argv=argv):
                code, _, err = self.cli(*argv, "--scope", "managed")
                self.assertEqual(code, 2)
                self.assert_sudo_hint(err, *argv, "--scope", "managed")
        self.assertEqual(self.get(self.mpath), {"rules": {}})

    def test_read_only_file(self) -> None:
        self.put(self.mpath, {"rules": {}})
        self.mpath.chmod(0o444)
        self.addCleanup(self.mpath.chmod, 0o644)
        code, _, err = self.cli("mode", "on", "m", "--scope", "managed")
        self.assertEqual(code, 2)
        self.assertIn("sudo", err)

class PathOption(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.fpath = self.tmp / "custom" / "rules.json"

    def with_path(self, *argv: str, agent: bool = False) -> tuple[int, str, str]:
        return self.cli(*argv, "--scope", "managed", "--path", str(self.fpath), agent=agent)

    def test_every_write_verb_targets_the_path_file(self) -> None:
        self.assertEqual(self.with_path("rule", "add", "no-pkill", "--json", MANAGED_RULE)[0], 0)
        self.assertEqual(self.with_path("rule", "set", "no-pkill", "--json", '{"action": "warn"}')[0], 0)
        self.assertEqual(self.with_path("mode", "declare", "incident")[0], 0)
        self.assertEqual(self.with_path("mode", "on", "incident")[0], 0)
        self.assertTrue(self.get(self.fpath)["modes"]["incident"]["active"])
        self.assertEqual(self.with_path("mode", "off", "incident")[0], 0)
        self.assertEqual(self.with_path("mode", "undeclare", "incident")[0], 0)
        self.assertEqual(self.with_path("preset", "install", "process-safety")[0], 0)
        self.assertEqual(sorted(self.get(self.fpath)["rules"]), ["kill-9", "no-pkill"])
        self.assertEqual(self.with_path("rule", "rm", "no-pkill")[0], 0)
        self.assertNotIn("no-pkill", self.get(self.fpath)["rules"])
        self.assertFalse(self.mpath.exists())
        self.assertFalse(self.dpath.exists())
        self.assertFalse(self.gpath.exists())

    def test_path_needs_managed_scope_on_write_verbs(self) -> None:
        p = str(self.fpath)
        for argv in (("rule", "add", "x", "--json", RULE, "--path", p),
                     ("rule", "add", "x", "--json", RULE, "--path", p, "--scope", "global"),
                     ("rule", "add", "x", "--json", RULE, "--path", p, "--scope", "project"),
                     ("rule", "set", "x", "--json", '{"action": "warn"}', "--path", p),
                     ("rule", "rm", "x", "--path", p),
                     ("mode", "declare", "m", "--path", p),
                     ("mode", "undeclare", "m", "--path", p),
                     ("mode", "on", "m", "--scope", "global", "--path", p),
                     ("mode", "off", "m", "--scope", "project", "--path", p),
                     ("preset", "install", "docs-first", "--path", p)):
            with self.subTest(argv=argv):
                code, _, err = self.cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn("--path names a managed-format file and needs --scope managed", err)
        self.assertFalse(self.fpath.exists())
        self.assertFalse(self.gpath.exists())

    def test_note_only_when_the_hook_will_not_read_the_file(self) -> None:
        _, out, _ = self.with_path("rule", "add", "x", "--json", RULE)
        self.assertIn(f"note: the hook enforces {self.fpath} only if GUARDRAILS_MANAGED_PATH points at it", out)
        for target in (self.mpath, self.dpath, self.fpath):
            with self.subTest(target=target):
                os.environ["GUARDRAILS_MANAGED_PATH"] = str(self.fpath if target == self.fpath else self.mpath)
                code, out, _ = self.cli("rule", "add", "y", "--json", RULE, "--scope", "managed", "--path", str(target))
                self.assertEqual(code, 0)
                self.assertNotIn("the hook enforces", out)

    def test_read_verbs_accept_path_without_a_managed_scope(self) -> None:
        target = str(self.tmp / "p.json")
        self.assertEqual(self.cli("status", "--scope", "project", "--path", target)[0], 0)
        self.assertEqual(self.cli("rule", "test", "--json", RULE, "strings x", "--path", target)[0], 0)

    def test_status_loads_the_path_file_as_extra_source(self) -> None:
        self.put(self.fpath, {"rules": {"no-pkill": {"match": {"program": "pkill"}, "message": "no"}}})
        code, out, _ = self.cli("status", "--path", str(self.fpath))
        self.assertEqual(code, 0)
        self.assertIn(f"--path `{self.fpath}` present", out)
        self.assertIn("`no-pkill` deny · managed · always enforced", out)
        self.assertNotIn("no-pkill", self.cli("status")[1])

    def test_rank_below_default_and_tightening_only(self) -> None:
        strict = {"rules": {"r": {"match": {"program": "pkill"}, "message": "default", "action": "deny"}}}
        loose = {"rules": {"r": {"match": {"program": "kill"}, "message": "other", "action": "warn"}}}
        self.put(self.dpath, strict)
        self.put(self.fpath, loose)
        out = self.cli("status", "--path", str(self.fpath))[1]
        self.assertIn("`r` deny · managed · always enforced", out)
        out = self.cli("rule", "test", "--id", "r", "pkill x", "kill 1", "--path", str(self.fpath))[1]
        self.assertEqual(caught(out), {"pkill x": True, "kill 1": False})
        self.assertIn("**Message** default", out)

    def test_path_reports_problems_like_the_override(self) -> None:
        self.put(self.fpath, "{nope")
        out = self.cli("status", "--path", str(self.fpath))[1]
        self.assertIn("unreadable managed state, so its rules are NOT enforced until it is fixed", out)
        self.put(self.fpath, {"rules": {"bad": {"match": {"builtin": "nope"}, "message": "x"}}})
        out = self.cli("status", "--path", str(self.fpath))[1]
        self.assertIn("managed rule bad is invalid and ignored", out)

    def test_path_file_ownership_problems_are_reported(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root owns everything")
        self.put(self.fpath, {"rules": {}})
        out = self.cli("status", "--path", str(self.fpath))[1]
        self.assertIn(f"managed file {self.fpath} is not owned by root", out)

    def test_rule_test_by_id_uses_the_path_file(self) -> None:
        self.put(self.fpath, {"rules": {"only-here": {"match": {"program": "pkill"}, "message": "no"}}})
        self.assertEqual(self.cli("rule", "test", "--id", "only-here", "pkill x")[0], 2)
        code, out, _ = self.cli("rule", "test", "--id", "only-here", "pkill x", "--path", str(self.fpath))
        self.assertEqual(code, 0)
        self.assertEqual(caught(out), {"pkill x": True})

    def test_session_mode_on_reads_modes_from_the_path_file(self) -> None:
        self.put(self.fpath, {"modes": {"incident": {"description": "fire", "agentMayEnable": True}}})
        self.assertEqual(self.cli("mode", "on", "incident", "--session-id", "s1")[0], 2)
        code, _, _ = self.cli("mode", "on", "incident", "--session-id", "s1", "--path", str(self.fpath))
        self.assertEqual(code, 0)

    def test_relative_path_and_home_are_resolved(self) -> None:
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.assertEqual(self.cli("rule", "add", "x", "--json", RULE, "--scope", "managed", "--path", "rel.json")[0], 0)
        self.assertTrue((self.tmp / "rel.json").is_file())

    def test_agent_gating_still_applies(self) -> None:
        code, _, err = self.with_path("rule", "add", "x", "--json", RULE, agent=True)
        self.assertEqual(code, 3)
        self.assertIn("refused:", err)
        self.assertEqual(self.with_path("rule", "add", "x", "--json", RULE, "--as-user", agent=True)[0], 0)


class PathNotWritable(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")

    def test_sudo_hint_keeps_the_path_argument(self) -> None:
        ro = self.tmp / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        self.addCleanup(ro.chmod, 0o755)
        target = str(ro / "sub" / "rules.json")
        argv = ("rule", "add", "x", "--json", RULE, "--scope", "managed", "--path", target)
        code, _, err = self.cli(*argv)
        self.assertEqual(code, 2)
        self.assertIn(f"Re-run with sudo: sudo {store.CLI} ", err)
        self.assertIn(shlex.join(argv), err)
        self.assertFalse((ro / "sub").exists())

    def test_sudo_hint_adds_the_env_override_as_path(self) -> None:
        ro = self.tmp / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        self.addCleanup(ro.chmod, 0o755)
        target = str(ro / "sub" / "rules.json")
        os.environ["GUARDRAILS_MANAGED_PATH"] = target
        code, _, err = self.cli("rule", "rm", "x", "--scope", "managed")
        self.assertEqual(code, 2)
        self.assertIn(f"--scope managed --path {target}", err)


class PathOptionEdges(AstIsolated):
    def test_path_with_spaces_is_quoted_in_the_sudo_hint(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        ro = self.tmp / "ro dir"
        ro.mkdir()
        ro.chmod(0o555)
        self.addCleanup(ro.chmod, 0o755)
        target = str(ro / "x y.json")
        code, _, err = self.cli("rule", "add", "x", "--json", RULE, "--scope", "managed", "--path", target)
        self.assertEqual(code, 2)
        self.assertIn(shlex.quote(target), err)

    def test_path_with_spaces_is_written(self) -> None:
        target = self.tmp / "a b" / "x y.json"
        self.assertEqual(self.cli("rule", "add", "x", "--json", RULE, "--scope", "managed", "--path", str(target))[0], 0)
        self.assertIn("x", self.get(target)["rules"])

    def test_unwritable_existing_file_reports_file_and_sudo_with_path(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        target = self.tmp / "custom" / "ro.json"
        self.put(target, {"rules": {}})
        target.chmod(0o444)
        self.addCleanup(target.chmod, 0o644)
        code, _, err = self.cli("rule", "add", "x", "--json", RULE, "--scope", "managed", "--path", str(target))
        self.assertEqual(code, 2)
        self.assertIn("file is not writable", err)
        self.assertIn(f"--path {target}", err)
        self.assertEqual(self.get(target), {"rules": {}})

    def test_tilde_is_expanded(self) -> None:
        os.environ["HOME"] = str(self.tmp / "home")
        code, _, _ = self.cli("rule", "add", "x", "--json", RULE, "--scope", "managed", "--path", "~/g.json")
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "home" / "g.json").is_file())
        self.assertFalse(Path("~").exists())

    def test_path_equal_to_the_platform_default_is_not_an_extra_source(self) -> None:
        self.put(self.dpath, {"rules": {"r": {"match": {"program": "pkill"}, "message": "m"}}})
        out = self.cli("status", "--path", str(self.dpath))[1]
        self.assertNotIn("--path `", out)
        self.assertEqual(out.splitlines()[0].count(str(self.dpath)), 1)
        self.assertNotIn("the hook enforces", out)

    def test_lower_ranked_file_cannot_switch_on_a_higher_mode(self) -> None:
        self.put(self.dpath, {"rules": {"r": {"match": {"program": "pkill"}, "message": "m", "modes": ["m"]}},
                              "modes": {"m": {}}})
        self.put(self.mpath, {"modes": {"m": {"active": True}}})
        out = self.cli("status")[1]
        self.assertIn("`m` off · agent may enable: no · managed", out)
        self.assertNotIn("suspended", out)
        extra = self.tmp / "extra.json"
        self.put(extra, {"modes": {"m": {"active": True}}})
        os.environ["GUARDRAILS_MANAGED_PATH"] = str(self.tmp / "none.json")
        out = self.cli("status", "--path", str(extra))[1]
        self.assertIn("`m` off · agent may enable: no · managed", out)
        self.assertNotIn("suspended", out)

    def test_hook_keeps_enforcing_when_the_override_declares_active(self) -> None:
        self.put(self.dpath, {"rules": {"r": {"match": {"program": "pkill"}, "message": "no pkill", "modes": ["m"]}},
                              "modes": {"m": {}}})
        self.put(self.mpath, {"modes": {"m": {"active": True}}})
        out = self.hook("pkill x")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class RuleTestNotes(AstIsolated):
    DRAFT = '{"match": {"program": "pkill"}, "message": "m"}'

    def notes(self, *argv: str) -> str:
        code, out, _ = self.cli("rule", "test", *argv, "pkill x")
        self.assertEqual(code, 0)
        return out

    def test_plain_rule_has_no_notes(self) -> None:
        self.assertNotIn("**Note**", self.notes("--json", self.DRAFT))

    def test_disabled_rule(self) -> None:
        out = self.notes("--json", '{"match": {"program": "pkill"}, "message": "m", "enabled": false}')
        self.assertIn("**Note** rule is disabled", out)
        self.assertEqual(caught(out), {"pkill x": True})

    def test_missing_required_binary(self) -> None:
        out = self.notes("--json", '{"match": {"program": "pkill"}, "message": "m", "requires": ["no-such-bin-xyz"]}')
        self.assertIn("none of no-such-bin-xyz is installed here", out)

    def test_modes_inactive_and_active(self) -> None:
        self.cli("rule", "add", "r", "--json", '{"match": {"program": "pkill"}, "message": "m", "modes": ["m"]}')
        self.cli("mode", "declare", "m")
        self.assertIn("suspends this rule while mode m is active (none is now)", self.notes("--id", "r"))
        self.cli("mode", "on", "m", "--session-id", "s1")
        out = self.notes("--id", "r", "--session-id", "s1")
        self.assertIn("mode m is active, so the hook suspends this rule right now", out)
        self.assertEqual(caught(out), {"pkill x": True})

    def test_hook_and_project_rules_disabled(self) -> None:
        self.cli("rule", "add", "g", "--json", self.DRAFT)
        self.cli("rule", "add", "p", "--json", self.DRAFT, "--scope", "project")
        self.cli("disable")
        self.assertIn("the global hook is disabled", self.notes("--id", "g"))
        self.assertIn("the global hook is disabled", self.notes("--id", "p"))
        self.cli("enable")
        self.put(self.ppath, {**self.get(self.ppath), "enabled": False})
        code, _, err = self.cli("rule", "test", "--id", "p", "pkill x")
        self.assertEqual(code, 2)
        self.assertIn("project rules are disabled, so project entries are not loaded", err)
        self.assertNotIn("hook is disabled", self.notes("--id", "g"))

    def test_managed_rule_survives_a_disabled_hook(self) -> None:
        self.cli("rule", "add", "m", "--json", self.DRAFT, "--scope", "managed")
        self.cli("disable")
        self.assertNotIn("hook is disabled", self.notes("--id", "m"))
