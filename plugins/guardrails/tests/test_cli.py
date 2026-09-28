from __future__ import annotations

import json
import os

from helpers import Isolated

RULE = ('{"match": {"program": "strings"}, "message": "Read the docs.", "retry": "same-command", '
        '"modes": ["reverse-engineering"]}')


class RuleCommands(Isolated):
    def test_add_set_rm_global(self) -> None:
        code, out, _ = self.cli("rule", "add", "no-strings", "--json", RULE, "--reason", "user asked")
        self.assertEqual(code, 0)
        self.assertIn("added rule no-strings", out)
        rule = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual((rule["setBy"]["by"], rule["setBy"]["reason"]), ("user", "user asked"))
        self.assertEqual(self.cli("rule", "set", "no-strings", "action=warn", "program=strings,otool")[0], 0)
        rule = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual((rule["action"], rule["match"]["program"]), ("warn", ["strings", "otool"]))
        self.assertEqual(self.cli("rule", "set", "no-strings", "messageShort=short", "requires=fd,fdfind")[0], 0)
        rule = self.get(self.gpath)["rules"]["no-strings"]
        self.assertEqual((rule["messageShort"], rule["requires"]), ("short", ["fd", "fdfind"]))
        self.assertEqual(self.cli("rule", "set", "no-strings", "messageShort=")[0], 0)
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
        self.assertEqual(self.cli("rule", "set", "no-strings", "colour=red")[0], 2)
        self.assertEqual(self.cli("rule", "set", "no-strings", "regex=(")[0], 2)
        self.assertEqual(self.cli("rule", "set", "no-strings", "enabled=maybe")[0], 2)
        self.assertNotIn("regex", self.get(self.gpath)["rules"]["no-strings"]["match"])

    def test_unknown_rule(self) -> None:
        self.assertEqual(self.cli("rule", "set", "ghost", "action=deny")[0], 2)
        self.assertEqual(self.cli("rule", "rm", "ghost")[0], 2)

    def test_project_add_goes_to_project_file(self) -> None:
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE, "--project")[0], 0)
        self.assertIn("no-strings", self.get(self.ppath)["rules"])
        self.assertFalse(self.gpath.exists())

    def test_project_override_of_global_rule(self) -> None:
        self.cli("rule", "add", "no-strings", "--json", RULE)
        code, out, _ = self.cli("rule", "set", "no-strings", "action=deny", "enabled=false", "args=-a", "--project")
        self.assertEqual(code, 0)
        entry = self.get(self.ppath)["rules"]["no-strings"]
        self.assertEqual(entry["action"], "deny")
        self.assertEqual(entry["match"], {"args": "-a"})
        self.assertNotIn("message", entry)
        self.assertIn("enabled=false", out)
        self.assertIn("args=-a", out)

    def test_project_flag_outside_project(self) -> None:
        del os.environ["CLAUDE_PROJECT_DIR"]
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE, "--project")[0], 2)

    def test_corrupt_state_is_not_overwritten(self) -> None:
        self.put(self.gpath, "{nope")
        self.assertEqual(self.cli("rule", "add", "no-strings", "--json", RULE)[0], 2)
        self.assertEqual(self.gpath.read_text(), "{nope")


class Gate(Isolated):
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
        self.assertEqual(self.cli("disable", "--project")[0], 0)
        self.assertFalse(self.get(self.ppath)["enabled"])
        self.assertFalse(self.gpath.exists())

    def test_read_only_verbs_allowed_for_agents(self) -> None:
        self.assertEqual(self.cli("status", agent=True)[0], 0)
        self.assertEqual(self.cli("preset", "list", agent=True)[0], 0)


class ModeCommands(Isolated):
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
        self.assertEqual(self.cli("mode", "declare", "reverse-engineering", "--project")[0], 0)
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
        self.assertEqual(self.cli("mode", "declare", "reverse-engineering", "--project")[0], 0)
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


class Status(Isolated):
    def test_lists_rules_modes_and_problems(self) -> None:
        self.put(self.gpath, {"rules": {"no-strings": json.loads(RULE),
                                        "bad": {"match": {"regex": "("}, "message": "x"}},
                              "modes": {"reverse-engineering": {"description": "RE", "agentMayEnable": True}}})
        self.put(self.ppath, {"rules": {"nm": {"match": {"program": "nm"}, "message": "m", "modes": ["ghost"]}}})
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        for expected in ("no-strings [global] deny retry: program=strings", "nm [project] deny: program=nm",
                         "reverse-engineering: inactive; agent may enable: yes; RE", "rule bad:",
                         "mode 'ghost' is not declared"):
            self.assertIn(expected, out)

    def test_shows_session_activation(self) -> None:
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
        self.put(self.gpath, {"rules": {"no-strings": json.loads(RULE)},
                              "modes": {"reverse-engineering": {"description": "RE", "agentMayEnable": True}},
                              "sessions": {"s1": {"modes": {"reverse-engineering": {"by": "agent", "reason": "why"}}}}})
        out = self.cli("status")[1]
        self.assertIn("ACTIVE (by agent: why)", out)
        self.assertIn("SUSPENDED by reverse-engineering", out)

    def test_reports_corrupt_file(self) -> None:
        self.put(self.gpath, "{nope")
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("unreadable", out)

    def test_empty(self) -> None:
        out = self.cli("status")[1]
        self.assertIn("(none; the guardrails:setup skill installs recommended presets)", out)
        self.assertIn("(none declared)", out)


def line(marker: str, cmd: str) -> str:
    return f"  {marker:<7}{cmd}"


class RuleTest(Isolated):
    DRAFT = '{"match": {"program": "strings"}, "message": "docs"}'

    def test_draft_matches_and_misses(self) -> None:
        heredoc = "cat <<'EOF' > n.md\nstrings\nEOF"
        code, out, _ = self.cli(
            "rule", "test", "--json", self.DRAFT,
            "sudo strings x", "bash -c 'strings a'", "man strings", "echo strings", heredoc,
        )
        self.assertEqual(code, 0)
        self.assertIn("rule (draft): deny", out)
        self.assertIn(line("match", "sudo strings x"), out)
        self.assertIn(line("match", "bash -c 'strings a'"), out)
        self.assertIn(line("-", "man strings"), out)
        self.assertIn(line("-", "echo strings"), out)
        self.assertIn(line("-", "cat <<'EOF' > n.md\\nstrings\\nEOF"), out)
        self.assertIn("message: docs", out)

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
        self.cli("rule", "set", "no-strings", "action=deny", "--project")
        code, out, _ = self.cli("rule", "test", "--id", "no-strings", "strings a")
        self.assertEqual(code, 0)
        self.assertIn("rule no-strings: deny retry modes=reverse-engineering", out)
        self.assertIn(line("match", "strings a"), out)

    def test_unknown_id(self) -> None:
        self.assertEqual(self.cli("rule", "test", "--id", "ghost", "echo hi")[0], 2)

    def test_agent_allowed_without_as_user(self) -> None:
        self.assertEqual(self.cli("rule", "test", "--json", self.DRAFT, "echo hi", agent=True)[0], 0)

    def test_is_read_only(self) -> None:
        self.cli("rule", "test", "--json", self.DRAFT, "echo hi")
        self.assertFalse(self.gpath.exists())

    def test_requires_unmet_notes(self) -> None:
        draft = ('{"match": {"program": "strings"}, "message": "docs", '
                 '"requires": ["definitely-not-installed-xyz"]}')
        out = self.cli("rule", "test", "--json", draft, "strings a")[1]
        self.assertIn("note: none of definitely-not-installed-xyz is installed here, so the hook skips this rule",
                      out)

    def test_disabled_rule_notes(self) -> None:
        draft = '{"match": {"program": "strings"}, "message": "docs", "enabled": false}'
        out = self.cli("rule", "test", "--json", draft, "strings a")[1]
        self.assertIn("note: rule is disabled", out)
