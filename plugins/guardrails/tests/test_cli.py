from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path

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
                         "reverse-engineering [global]: inactive; agent may enable: yes; RE", "rule bad:",
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
        self.assertIn("rule no-strings [global+project]: deny retry modes=reverse-engineering", out)
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


MANAGED_RULE = '{"match": {"program": "pkill"}, "message": "No pkill."}'


class ManagedScope(Isolated):
    def managed(self, *argv: str, agent: bool = False) -> tuple[int, str, str]:
        return self.cli(*argv, "--scope", "managed", agent=agent)

    def test_add_set_rm_round_trip(self) -> None:
        code, out, _ = self.managed("rule", "add", "no-pkill", "--json", RULE, "--reason", "policy")
        self.assertEqual(code, 0)
        self.assertIn(f"added rule no-pkill in {self.mpath}", out)
        self.assertFalse(self.gpath.exists())
        self.assertEqual(stat.S_IMODE(self.mpath.stat().st_mode), 0o644)
        self.assertEqual(self.get(self.mpath)["rules"]["no-pkill"]["setBy"]["reason"], "policy")
        self.assertEqual(self.managed("rule", "set", "no-pkill", "action=warn", "modes=")[0], 0)
        rule = self.get(self.mpath)["rules"]["no-pkill"]
        self.assertEqual((rule["action"], rule["modes"]), ("warn", []))
        self.assertEqual(self.managed("rule", "rm", "no-pkill")[0], 0)
        self.assertEqual(self.get(self.mpath)["rules"], {})

    def test_creates_missing_directory(self) -> None:
        self.assertFalse(self.mpath.parent.exists())
        self.assertEqual(self.managed("rule", "add", "x", "--json", RULE)[0], 0)
        self.assertTrue(self.mpath.is_file())

    def test_set_and_rm_need_an_existing_managed_rule(self) -> None:
        self.assertEqual(self.managed("rule", "set", "ghost", "action=deny")[0], 2)
        self.assertEqual(self.managed("rule", "rm", "ghost")[0], 2)

    def test_always_enforced_note_only_without_modes(self) -> None:
        _, out, _ = self.managed("rule", "add", "bare", "--json", MANAGED_RULE)
        self.assertIn("note: managed rule bare lists no modes, so it is always enforced", out)
        _, out, _ = self.managed("rule", "add", "modal", "--json", RULE)
        self.assertNotIn("always enforced", out)
        _, out, _ = self.managed("rule", "set", "modal", "modes=")
        self.assertIn("always enforced", out)
        _, out, _ = self.cli("rule", "add", "local", "--json", MANAGED_RULE)
        self.assertNotIn("always enforced", out)

    def test_set_and_rm_refused_from_other_scopes(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        before = self.mpath.read_text()
        for scope in ("global", "project"):
            for argv in (("rule", "set", "no-strings", "action=warn"), ("rule", "rm", "no-strings")):
                with self.subTest(scope=scope, verb=argv[1]):
                    code, _, err = self.cli(*argv, "--scope", scope)
                    self.assertEqual(code, 3)
                    self.assertIn("--scope managed", err)
        self.assertEqual(self.cli("rule", "set", "no-strings", "action=warn", "--project")[0], 3)
        self.assertEqual(self.mpath.read_text(), before)
        self.assertFalse(self.gpath.exists())
        self.assertFalse(self.ppath.exists())

    def test_add_from_other_scope_notes_it_can_only_tighten(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        code, out, _ = self.cli("rule", "add", "no-strings", "--json", RULE, "--scope", "project")
        self.assertEqual(code, 0)
        self.assertIn("also a managed rule", out)

    def test_project_flag_conflicts_with_managed_scope(self) -> None:
        code, _, err = self.cli("rule", "add", "x", "--json", RULE, "--project", "--scope", "managed")
        self.assertEqual(code, 2)
        self.assertIn("conflicts", err)
        self.assertEqual(self.cli("rule", "add", "x", "--json", RULE, "--project", "--scope", "project")[0], 0)

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

    def test_lower_scope_can_switch_a_managed_mode_on_but_not_off_the_managed_activation(self) -> None:
        self.managed("mode", "declare", "incident")
        self.assertEqual(self.cli("mode", "on", "incident", "--scope", "global")[0], 0)
        self.assertTrue(self.get(self.gpath)["modes"]["incident"]["active"])
        self.managed("mode", "on", "incident")
        code, out, _ = self.cli("mode", "off", "incident", "--scope", "project")
        self.assertEqual(code, 0)
        self.assertIn("managed scope keeps mode incident on", out)
        self.assertIn("ACTIVE", self.cli("status")[1])

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
        self.cli("rule", "add", "mine", "--json", MANAGED_RULE, "--project")
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        for expected in (f"managed state: {self.mpath}", "no-strings [managed+global] deny retry",
                         "bare [managed] deny ALWAYS ENFORCED", "mine [project] deny",
                         "reverse-engineering [managed]: inactive"):
            self.assertIn(expected, out)

    def test_status_without_managed_file(self) -> None:
        self.assertIn(f"managed state: {self.mpath} (absent)", self.cli("status")[1])

    def test_status_reports_unreadable_managed_file(self) -> None:
        self.put(self.mpath, "{nope")
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("unreadable managed state file (managed rules are not enforced until it is fixed)", out)

    def test_status_reports_invalid_managed_rule(self) -> None:
        self.put(self.mpath, {"rules": {"bad": {"match": {"regex": "("}, "message": "x"}}})
        self.assertIn("rule bad:", self.cli("status")[1])

    def test_status_notes_managed_rules_survive_disabled_hook(self) -> None:
        self.managed("rule", "add", "bare", "--json", MANAGED_RULE)
        self.cli("disable")
        self.assertIn("hook enabled:  no (disabled by user); managed rules stay enforced", self.cli("status")[1])

    def test_rule_test_shows_origin(self) -> None:
        self.managed("rule", "add", "no-strings", "--json", RULE)
        out = self.cli("rule", "test", "--id", "no-strings", "strings a")[1]
        self.assertIn("rule no-strings [managed]:", out)

    def test_corrupt_managed_file_is_not_overwritten(self) -> None:
        self.put(self.mpath, "{nope")
        self.assertEqual(self.managed("rule", "add", "x", "--json", RULE)[0], 2)
        self.assertEqual(self.mpath.read_text(), "{nope")


class ManagedNotWritable(Isolated):
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
        self.assertIn("Re-run with sudo: sudo python3", err)
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

    def test_other_scopes_are_unaffected(self) -> None:
        self.lock_down(self.tmp / "ro")
        os.environ["GUARDRAILS_MANAGED_PATH"] = str(self.tmp / "ro" / "guardrails.json")
        self.assertEqual(self.cli("rule", "add", "x", "--json", RULE)[0], 0)
