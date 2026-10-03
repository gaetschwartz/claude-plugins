from __future__ import annotations  # noqa: I001

import json
import os
import unittest
from typing import Any

from helpers import LIB, AstIsolated

import store

STRINGS: dict[str, Any] = {"match": {"program": "strings"}, "message": "Read the docs.", "retry": "same-command",
                           "modes": ["reverse-engineering"]}
MODES: dict[str, Any] = {"reverse-engineering": {"description": "RE", "agentMayEnable": True}}


def decision(out: dict[str, Any] | None) -> str:
    if out is None:
        return "allow"
    hs = out.get("hookSpecificOutput", {})
    return hs.get("permissionDecision") or ("warn" if "additionalContext" in hs else "notice")


def reason(out: dict[str, Any] | None) -> str:
    assert out is not None
    return out["hookSpecificOutput"]["permissionDecisionReason"]


class Hook(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"no-strings": dict(STRINGS)}, "modes": dict(MODES)})

    def with_session_mode(self, agent_may_enable: bool = True, **mode: Any) -> None:
        state = self.get(self.gpath)
        state["modes"]["reverse-engineering"]["agentMayEnable"] = agent_may_enable
        record = {"by": "agent", "reason": "user: RE libfoo", **mode}
        state["sessions"] = {"s1": {"seenAt": store.now(), "modes": {"reverse-engineering": record}}}
        self.put(self.gpath, state)

    def test_no_rules_is_silent_and_writes_nothing(self) -> None:
        self.gpath.unlink()
        self.assertIsNone(self.hook("strings x"))
        self.assertEqual(list(self.data.iterdir()), [])

    def test_unmatched_command_is_silent(self) -> None:
        self.assertIsNone(self.hook("ls -la"))

    def test_deny_names_rule_retry_and_mode(self) -> None:
        out = self.hook("strings /bin/ls")
        self.assertEqual(decision(out), "deny")
        text = reason(out)
        self.assertIn("[guardrails:no-strings] Read the docs.", text)
        self.assertIn("re-run it unchanged", text)
        self.assertIn("'reverse-engineering'", text)
        self.assertIn("guardrails:mode", text)

    def test_mode_hint_without_agent_permission(self) -> None:
        state = self.get(self.gpath)
        state["modes"]["reverse-engineering"]["agentMayEnable"] = False
        self.put(self.gpath, state)
        text = reason(self.hook("strings /bin/ls"))
        self.assertIn("from their terminal", text)
        self.assertIn("mode on reverse-engineering --session-id s1", text)
        self.assertIn(str(LIB / "guard.py"), text)
        self.assertNotIn("guardrails:mode", text)

    def test_same_command_retry_is_per_command_and_session(self) -> None:
        self.assertEqual(decision(self.hook("strings /bin/ls")), "deny")
        self.assertIsNone(self.hook("strings /bin/ls"))
        self.assertEqual(decision(self.hook("strings /bin/cat")), "deny")
        self.assertEqual(decision(self.hook("strings /bin/ls", session="s2")), "deny")

    def test_retry_none_always_denies(self) -> None:
        state = self.get(self.gpath)
        state["rules"]["no-strings"]["retry"] = "none"
        self.put(self.gpath, state)
        self.assertEqual(decision(self.hook("strings a")), "deny")
        self.assertEqual(decision(self.hook("strings a")), "deny")
        self.assertNotIn("re-run it unchanged", reason(self.hook("strings a")))

    def test_warn_once_per_session_without_permission_decision(self) -> None:
        self.put(self.gpath, {"rules": {"k9": {"match": {"program": "kill", "args": "-9"},
                                               "message": "SIGTERM first", "action": "warn"}}})
        out = self.hook("kill -9 1")
        assert out is not None
        hs = out["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", hs)
        self.assertEqual(hs["hookEventName"], "PreToolUse")
        self.assertIn("[guardrails:k9] SIGTERM first", hs["additionalContext"])
        self.assertIsNone(self.hook("kill -9 2"))
        self.assertEqual(decision(self.hook("kill -9 2", session="s2")), "warn")

    def test_warn_rides_along_with_deny(self) -> None:
        state = self.get(self.gpath)
        state["rules"]["k9"] = {"match": {"program": "kill", "args": "-9"}, "message": "SIGTERM first",
                                "action": "warn"}
        self.put(self.gpath, state)
        text = reason(self.hook("strings a; kill -9 1"))
        self.assertIn("Read the docs.", text)
        self.assertIn("SIGTERM first", text)

    def test_agent_mode_suspends_and_notifies_once(self) -> None:
        self.with_session_mode()
        out = self.hook("strings a")
        assert out is not None
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("rule no-strings suspended by mode reverse-engineering", out["systemMessage"])
        self.assertIn("user: RE libfoo", out["systemMessage"])
        self.assertIsNone(self.hook("strings b"))

    def test_revoking_agent_permission_rearms_rule(self) -> None:
        self.with_session_mode(agent_may_enable=False)
        self.assertEqual(decision(self.hook("strings a")), "deny")

    def test_user_session_mode_is_silent(self) -> None:
        self.with_session_mode(agent_may_enable=False, by="user")
        self.assertIsNone(self.hook("strings a"))

    def test_persistently_active_mode_is_silent(self) -> None:
        state = self.get(self.gpath)
        state["modes"]["reverse-engineering"]["active"] = True
        self.put(self.gpath, state)
        self.assertIsNone(self.hook("strings a"))

    def test_project_rule_applies(self) -> None:
        self.put(self.gpath, {})
        self.put(self.ppath, {"rules": {"nm": {"match": {"program": "nm"}, "message": "no nm"}}})
        self.assertEqual(decision(self.hook("nm libfoo.a")), "deny")

    def test_project_mode_activation(self) -> None:
        self.put(self.ppath, {"modes": {"reverse-engineering": {"active": True}}})
        self.assertIsNone(self.hook("strings a"))

    def test_corrupt_project_state_does_not_disable_global_rules(self) -> None:
        self.put(self.ppath, "garbage")
        self.assertEqual(decision(self.hook("strings a")), "deny")

    def test_global_disabled_is_silent(self) -> None:
        state = self.get(self.gpath)
        state["enabled"] = False
        self.put(self.gpath, state)
        self.assertIsNone(self.hook("strings a"))

    def test_disabled_rule_is_silent(self) -> None:
        state = self.get(self.gpath)
        state["rules"]["no-strings"]["enabled"] = False
        self.put(self.gpath, state)
        self.assertIsNone(self.hook("strings a"))

    def test_unmet_requirement_skips_rule(self) -> None:
        state = self.get(self.gpath)
        state["rules"]["no-strings"]["requires"] = ["definitely-not-installed-xyz"]
        self.put(self.gpath, state)
        self.assertIsNone(self.hook("strings a"))

    def test_message_short_after_first_display_and_dedupe(self) -> None:
        sheet = {"message": "SHEET for {which:zz-none|zz-other}", "messageShort": "terse", "retry": "same-command"}
        self.put(self.gpath, {"rules": {"find-fd": {**sheet, "match": {"program": "find"}},
                                        "grep-rg": {**sheet, "match": {"builtin": "grep-recursive"}}}})
        first = reason(self.hook("find . | xargs grep -r x"))
        self.assertEqual(first.count("SHEET for zz-none"), 1)
        self.assertIn("[guardrails:find-fd, grep-rg]", first)
        second = reason(self.hook("find /tmp"))
        self.assertIn("terse", second)
        self.assertNotIn("SHEET", second)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_readonly_data_dir_still_denies(self) -> None:
        os.chmod(self.data, 0o500)
        self.addCleanup(os.chmod, self.data, 0o700)
        self.assertEqual(decision(self.hook("strings a")), "deny")

    def test_invalid_rule_in_state_is_ignored(self) -> None:
        state = self.get(self.gpath)
        state["rules"]["bad"] = {"match": {"regex": "("}, "message": "x"}
        self.put(self.gpath, state)
        self.assertEqual(decision(self.hook("strings a")), "deny")
        self.assertIsNone(self.hook("echo ("))

    def test_session_state_written_and_config_preserved(self) -> None:
        self.hook("strings a")
        state = self.get(self.gpath)
        self.assertIn("no-strings", state["rules"])
        self.assertEqual(state["modes"], MODES)
        self.assertEqual(len(state["sessions"]["s1"]["acknowledged"]), 1)
        self.assertIn("seenAt", state["sessions"]["s1"])


PKILL: dict[str, Any] = {"match": {"program": "pkill"}, "message": "No pkill.", "action": "deny"}


class ManagedHook(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.mpath, {"rules": {"no-pkill": dict(PKILL)}})

    def hook_stderr(self, command: str, session: str = "s1") -> tuple[dict[str, Any] | None, str]:
        out = self.hook(command, session)
        return out, self.hook_err.getvalue()

    def test_managed_rule_denies_with_origin_label(self) -> None:
        out = self.hook("pkill node")
        self.assertEqual(decision(out), "deny")
        self.assertIn("[guardrails:no-pkill (managed)] No pkill.", reason(out))
        self.assertIsNone(self.hook("echo hi"))

    def test_only_managed_rules_are_labelled(self) -> None:
        self.put(self.gpath, {"rules": {"no-strings": dict(STRINGS)}, "modes": dict(MODES)})
        text = reason(self.hook("strings a; pkill x"))
        self.assertIn("[guardrails:no-strings] Read the docs.", text)
        self.assertIn("[guardrails:no-pkill (managed)] No pkill.", text)

    def test_project_cannot_disable_or_loosen_managed_rule(self) -> None:
        self.put(self.ppath, {"rules": {"no-pkill": {"enabled": False, "action": "warn", "retry": "same-command",
                                                     "modes": ["x"], "match": {"program": "nm"}}}})
        for _ in range(2):
            self.assertEqual(decision(self.hook("pkill node")), "deny")
        self.assertIsNone(self.hook("nm a.out"))

    def test_global_cannot_loosen_managed_rule(self) -> None:
        self.put(self.gpath, {"rules": {"no-pkill": {"enabled": False, "action": "warn"}}})
        self.assertEqual(decision(self.hook("pkill node")), "deny")

    def test_managed_rule_survives_global_kill_switch(self) -> None:
        self.put(self.gpath, {"enabled": False, "rules": {"no-strings": dict(STRINGS)}})
        self.assertEqual(decision(self.hook("pkill node")), "deny")
        self.assertIsNone(self.hook("strings a"))

    def test_disabled_global_without_managed_rules_is_silent(self) -> None:
        self.mpath.unlink()
        self.put(self.gpath, {"enabled": False, "rules": {"no-strings": dict(STRINGS)}})
        self.assertIsNone(self.hook("strings a"))

    def test_project_switched_off_leaves_managed_rule(self) -> None:
        self.put(self.ppath, {"enabled": False})
        self.assertEqual(decision(self.hook("pkill node")), "deny")

    def test_corrupt_global_state_still_enforces_managed_statelessly(self) -> None:
        self.put(self.gpath, "{nope")
        self.assertEqual(decision(self.hook("pkill node")), "deny")
        self.assertEqual(self.gpath.read_text(), "{nope")

    def test_retry_on_managed_rule_is_honoured(self) -> None:
        self.put(self.mpath, {"rules": {"no-pkill": {**PKILL, "retry": "same-command"}}})
        self.assertEqual(decision(self.hook("pkill node")), "deny")
        self.assertIsNone(self.hook("pkill node"))

    def test_modes_suspend_a_managed_rule_only_as_the_managed_file_allows(self) -> None:
        locked = {"rules": {"no-pkill": {**PKILL, "modes": ["incident"]}},
                  "modes": {"incident": {"agentMayEnable": False}}}
        active = {"rules": locked["rules"], "modes": {"incident": {"active": True}}}

        def session(by: str) -> dict[str, Any]:
            return {"sessions": {"s1": {"seenAt": store.now(), "modes": {"incident": {"by": by, "reason": "x"}}}}}

        def mode(**fields: Any) -> dict[str, Any]:
            return {"modes": {"incident": fields}}

        cases = [
            ("a rule without modes ignores every mode", {"rules": {"no-pkill": dict(PKILL)}},
             {**mode(active=True, agentMayEnable=True), **session("user")},
             {"rules": {"no-pkill": {"modes": ["incident"]}}, **mode(active=True)}, "deny"),
            ("a user session mode suspends it", locked, session("user"), {}, "allow"),
            ("an agent cannot use a mode the managed file locks", locked,
             {**mode(agentMayEnable=True), **session("agent")}, {}, "deny"),
            ("a managed active mode cannot be switched off", active, mode(active=False), mode(active=False), "allow"),
            ("a project cannot switch on a managed mode", locked, {}, mode(active=True), "deny"),
            ("a global activation of a managed mode counts", locked, mode(active=True), {}, "allow"),
        ]
        for name, managed, global_state, project, expected in cases:
            with self.subTest(case=name):
                self.put(self.mpath, managed)
                self.put(self.gpath, global_state)
                self.put(self.ppath, project)
                self.assertEqual(decision(self.hook("pkill node")), expected)

    def test_lower_layer_mode_only_suspends_the_lower_layers_own_rule(self) -> None:
        self.put(self.gpath, {"rules": {"mine": {**PKILL, "modes": ["ops"]}},
                              "modes": {"ops": {"active": True}}})
        self.assertEqual(decision(self.hook("pkill node")), "deny")
        text = reason(self.hook("pkill node"))
        self.assertIn("no-pkill (managed)", text)
        self.assertNotIn("mine", text)

    def test_unreadable_managed_file_warns_once_and_keeps_other_layers(self) -> None:
        self.put(self.mpath, "{nope")
        self.put(self.gpath, {"rules": {"no-strings": dict(STRINGS)}, "modes": dict(MODES)})
        out, err = self.hook_stderr("strings a")
        assert out is not None
        self.assertEqual(decision(out), "deny")
        self.assertIn("NOT enforced", out["systemMessage"])
        self.assertIn(str(self.mpath), out["systemMessage"])
        self.assertIn("NOT enforced", err)
        out, _ = self.hook_stderr("strings b")
        assert out is not None
        self.assertNotIn("systemMessage", out)

    def test_unreadable_managed_file_without_other_rules_still_warns(self) -> None:
        self.put(self.mpath, "[]")
        out, _ = self.hook_stderr("ls")
        assert out is not None
        self.assertIn("NOT enforced", out["systemMessage"])
        out, _ = self.hook_stderr("ls")
        self.assertIsNone(out)

    def test_invalid_managed_rule_is_skipped_and_reported_once_per_session(self) -> None:
        self.put(self.mpath, {"rules": {"bad": {"match": {"regex": "("}, "message": "x"},
                                        "no-pkill": dict(PKILL)}})
        out, err = self.hook_stderr("pkill node")
        assert out is not None
        self.assertEqual(decision(out), "deny")
        self.assertIn("managed rule bad is invalid and ignored", out["systemMessage"])
        self.assertIn("managed rule bad is invalid and ignored", err)
        self.assertNotIn("no-pkill", out["systemMessage"])
        out, _ = self.hook_stderr("pkill again")
        assert out is not None
        self.assertNotIn("systemMessage", out)

    def test_wrong_shape_managed_content_is_reported(self) -> None:
        for content, needle in (({"rules": [PKILL]}, "'rules' must be an object"),
                                ({"rules": {"a": "x"}, "modes": "m"}, "'modes' must be an object")):
            with self.subTest(content=content):
                self.put(self.mpath, content)
                out = self.hook("ls", session=needle)
                assert out is not None
                self.assertIn(needle, out["systemMessage"])
                self.assertIsNone(self.hook("ls", session=needle))

    def test_managed_rule_naming_an_undeclared_mode_stays_enforced_and_is_reported(self) -> None:
        self.put(self.mpath, {"rules": {"no-pkill": {**PKILL, "modes": ["ghost"]}}})
        self.put(self.gpath, {"modes": {"ghost": {"active": True}},
                              "sessions": {"s1": {"seenAt": store.now(), "modes": {"ghost": {"by": "user"}}}}})
        self.put(self.ppath, {"modes": {"ghost": {"active": True}}})
        out = self.hook("pkill node")
        assert out is not None
        self.assertEqual(decision(out), "deny")
        self.assertIn("no-pkill lists mode 'ghost'", out["systemMessage"])

    def test_lower_layers_cannot_reword_a_managed_rule(self) -> None:
        self.put(self.gpath, {"rules": {"no-pkill": {"message": "global words"}}})
        self.put(self.ppath, {"rules": {"no-pkill": {"message": "project words", "messageShort": "s"}}})
        text = reason(self.hook("pkill node"))
        self.assertIn("[guardrails:no-pkill (managed)] No pkill.", text)
        self.assertNotIn("words", text)

    def test_unreadable_parent_directory_warns_and_keeps_other_layers(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        self.put(self.gpath, {"rules": {"no-strings": dict(STRINGS)}, "modes": dict(MODES)})
        self.mpath.parent.chmod(0)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        out = self.hook("strings a")
        assert out is not None
        self.assertEqual(decision(out), "deny")
        self.assertIn("NOT enforced", out["systemMessage"])


    def test_invalid_lower_layer_entry_for_managed_id_falls_back(self) -> None:
        self.put(self.ppath, {"rules": {"no-pkill": {"message": ""}}})
        self.assertIn("No pkill.", reason(self.hook("pkill node")))

    def test_no_managed_file_changes_nothing(self) -> None:
        self.mpath.unlink()
        self.assertIsNone(self.hook("pkill node"))


class ManagedSources(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.dpath, {"rules": {"no-pkill": dict(PKILL)}})

    def test_the_platform_default_is_enforced_whatever_the_override_holds(self) -> None:
        loosening = {"rules": {"no-pkill": {**PKILL, "message": "Allowed.", "action": "warn",
                                            "retry": "same-command", "enabled": False}}}
        for name, state in (("absent", None), ("empty", {}), ("loosening", loosening), ("unreadable", "{nope")):
            with self.subTest(override=name):
                if state is not None:
                    self.put(self.mpath, state)
                for _ in range(2):
                    out = self.hook("pkill node", session=name)
                    self.assertEqual(decision(out), "deny")
                    self.assertIn("No pkill.", reason(out))
                self.mpath.unlink(missing_ok=True)
        del os.environ["GUARDRAILS_MANAGED_PATH"]
        self.assertEqual(decision(self.hook("pkill node")), "deny")

    def test_an_unreadable_override_is_reported(self) -> None:
        self.put(self.mpath, "{nope")
        out = self.hook("pkill node")
        assert out is not None
        self.assertIn("NOT enforced", out["systemMessage"])

    def test_override_adds_rules(self) -> None:
        self.put(self.mpath, {"rules": {"no-strings": dict(STRINGS)}})
        self.assertEqual(decision(self.hook("strings a")), "deny")
        self.assertEqual(decision(self.hook("pkill a")), "deny")


class EndToEnd(AstIsolated):
    def test_deny_through_wrapper(self) -> None:
        self.put(self.gpath, {"rules": {"no-strings": dict(STRINGS)}, "modes": dict(MODES)})
        payload = json.dumps({"session_id": "e", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "strings /bin/ls"}})
        proc = self.run_guard(payload)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_corrupt_global_state_fails_open(self) -> None:
        self.put(self.gpath, "{nope")
        payload = json.dumps({"session_id": "e", "tool_name": "Bash", "tool_input": {"command": "strings x"}})
        proc = self.run_guard(payload)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.gpath.read_text(), "{nope")

    def test_odd_payloads_are_ignored(self) -> None:
        self.put(self.gpath, {"rules": {"no-strings": dict(STRINGS)}})
        for odd in ('{"tool_input": "notadict"}', '{"tool_name": "Bash", "tool_input": {"command": ["strings"]}}',
                    '{"tool_name": "Read", "tool_input": {"command": "strings x"}}', "[]", "null", "not json", ""):
            with self.subTest(payload=odd):
                proc = self.run_guard(odd)
                self.assertEqual((proc.returncode, proc.stdout), (0, ""), proc.stderr)


class ManagedEndToEnd(AstIsolated):
    def test_managed_deny_beats_project_disable_through_wrapper(self) -> None:
        self.put(self.mpath, {"rules": {"no-pkill": dict(PKILL)}})
        self.put(self.ppath, {"rules": {"no-pkill": {"enabled": False, "action": "warn", "retry": "same-command"}}})
        payload = json.dumps({"session_id": "e", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "sudo pkill -f node"}})
        for _ in range(2):
            proc = self.run_guard(payload)
            self.assertEqual(proc.returncode, 0)
            out = json.loads(proc.stdout)["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("no-pkill (managed)", out["permissionDecisionReason"])

    def test_unreadable_managed_file_never_crashes_the_wrapper(self) -> None:
        self.put(self.mpath, "{nope")
        payload = json.dumps({"session_id": "e", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        proc = self.run_guard(payload)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("NOT enforced", json.loads(proc.stdout)["systemMessage"])
