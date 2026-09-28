from __future__ import annotations  # noqa: I001

import json
from typing import Any

from helpers import Isolated

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


class Hook(Isolated):
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


class EndToEnd(Isolated):
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
