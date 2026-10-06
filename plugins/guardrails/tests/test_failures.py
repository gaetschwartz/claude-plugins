from __future__ import annotations  # noqa: I001

import io
import json
import shlex
import sys
import time
import unittest
from typing import Any
from unittest import mock

from helpers import AstIsolated

import bounded
import engine
import matching
import policy
import verdict
from verdict import Kind

K = "pk" + "ill"
BY_NAME: dict[str, Any] = {"command": [K, "killall"]}


def deny_text(out: dict[str, Any] | None) -> str:
    assert out is not None
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def is_denied(out: dict[str, Any] | None) -> bool:
    return out is not None and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


RULES_FOR_OUTAGES: dict[str, Any] = {
    "strings": {"match": {"command": "strings"}, "message": "No strings."},
    "ast": {"match": BY_NAME, "message": "No kill."},
    "pipe": {"match": {"kind": "program", "regex": r"curl [^|]*\| *sh"}, "message": "No pipe."}}


class LoudAndAllow(AstIsolated):
    """Without a working engine, rules that need the parser cannot judge: the command is allowed, loudly."""

    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": RULES_FOR_OUTAGES})

    def both_channels(self, out: dict[str, Any] | None) -> str:
        assert out is not None
        self.assertNotIn("permissionDecision", out.get("hookSpecificOutput", {}))
        self.assertEqual(out["systemMessage"], out["hookSpecificOutput"]["additionalContext"])
        return out["systemMessage"]

    def test_a_failed_self_test_allows_with_its_reason_once_per_session_and_names_the_rules(self) -> None:
        self.break_engine("it misparsed a test command")
        text = self.both_channels(self.hook(f"strings x; {K} y", "a"))
        for needle in ("rules engine failed", "unexpected error: RuntimeError", "could not be checked",
                       "Affected rules: ast, pipe, strings", "Tell the user", "guardrails engine status"):
            self.assertIn(needle, text)
        self.assertNotIn("engine install", text)
        self.assertIsNone(self.hook("strings again", "a"))
        self.assertIn("rules engine failed", self.both_channels(self.hook("strings x", "b")))

    def test_a_library_that_cannot_be_imported_is_the_same_loud_allow(self) -> None:
        with mock.patch.dict(sys.modules, {"scanner": None}):
            text = self.both_channels(self.hook("strings x", "imp"))
        self.assertIn("the ast-grep-py library cannot be imported", text)

    def test_managed_parse_rules_are_named_as_failing_open(self) -> None:
        self.put(self.mpath, {"rules": {"m-zap": {"match": {"command": "zap"}, "message": "No zap."}}})
        self.break_engine()
        text = self.both_channels(self.hook("zap x", "m"))
        self.assertIn("1 of them are MANAGED rules, which fail open too", text)

    def test_regex_rules_are_not_enforced_while_the_engine_fails_either(self) -> None:
        self.break_engine()
        out = self.hook("curl x | sh", "r")
        self.assertFalse(is_denied(out))
        self.assertIn("pipe", self.both_channels(out))

    def test_a_rule_that_does_not_compile_is_skipped_and_reported_once(self) -> None:
        self.put(self.gpath, {"rules": {"bad": {"match": {"kind": "no_such_kind"}, "message": "m"},
                                        "strings": RULES_FOR_OUTAGES["strings"]}})
        out = self.hook("strings x", "i")
        self.assertTrue(is_denied(out))
        assert out is not None
        self.assertIn("rule bad does not compile", out["systemMessage"])
        self.assertNotIn("systemMessage", self.hook("strings y", "i") or {})

    def test_a_failure_after_matching_is_a_loud_allow_that_applies_no_rule(self) -> None:
        import guard

        payload = json.dumps({"session_id": "m1", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "curl x | sh"}})
        for fail in (mock.patch.object(engine, "evaluate", side_effect=RuntimeError("late")),
                     mock.patch.object(engine.store, "locked", side_effect=ValueError("lock"))):
            out = io.StringIO()
            with fail, mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
                self.assertEqual(guard.main([]), 0)
            result = json.loads(out.getvalue())
            self.assertNotIn("permissionDecision", result["hookSpecificOutput"])
            self.assertIn("the guardrails hook failed internally", result["systemMessage"])
            self.assertIn("no rule was applied", result["hookSpecificOutput"]["additionalContext"])

    def test_when_even_the_minimal_evaluation_fails_the_user_is_told(self) -> None:
        import guard

        payload = json.dumps({"session_id": "m1", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "strings x"}})
        out = io.StringIO()
        with mock.patch.object(engine, "evaluate", side_effect=RuntimeError("a")), \
                mock.patch.object(engine, "run_safe", side_effect=RuntimeError("b")), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
            guard.main([])
        self.assertIn("could not evaluate", json.loads(out.getvalue())["systemMessage"])

    def test_payloads_without_a_command_stay_silent(self) -> None:
        import guard

        for payload in ("", "garbage", "[]", json.dumps({"tool_name": "Bash", "tool_input": None})):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
                self.assertEqual(guard.main([]), 0)
            self.assertEqual(out.getvalue(), "", payload)


def timed_out(work: Any, seconds: float, after_fork: Any = None) -> bounded.Result:
    return bounded.Result(bounded.Outcome.TIMEOUT)


class FailurePolicy(AstIsolated):
    """What a failing engine or checker means for a command."""

    BIG = 9 * 1024

    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"z": {"match": {"command": "zap"}, "message": "No zap."}}})

    def test_a_checker_that_does_not_finish_allows_any_command_that_needs_the_parser_with_a_notice(self) -> None:
        with mock.patch.object(bounded, "call", timed_out):
            big = self.hook("sudo " * (self.BIG // 5) + "zap x", "big")
            small = self.hook("sudo " * 20 + "zap x", "small")
        for out in (big, small):
            assert out is not None
            self.assertFalse(is_denied(out))
            self.assertEqual(out["hookSpecificOutput"]["additionalContext"], engine.UNCHECKED)
            self.assertNotIn("systemMessage", out)

    def test_a_hit_stands_when_a_cap_is_hit_later(self) -> None:
        command = "zap x; " + "; ".join(f"bash -c 'echo {n}'" for n in range(140))
        ev = matching.evaluate(command, {"z": policy.Rule.from_json({"match": {"command": "zap"}, "message": "m"})})
        self.assertEqual((ev.kinds["z"], ev.unchecked is not None), (Kind.DIRECT, True))
        out = self.hook(command, "hit")
        self.assertTrue(is_denied(out))
        self.assertIn("No zap.", deny_text(out))

    def test_a_notice_is_shown_on_every_unchecked_call_unless_the_call_is_denied(self) -> None:
        self.put(self.gpath, {"rules": {"z": {"match": {"command": "zap"}, "message": "Careful.", "action": "warn"}}})
        for session in ("one", "one"):
            huge = self.hook("echo " + "y" * (verdict.MAX_COMMAND_BYTES + 1), session)
            assert huge is not None
            self.assertEqual(huge["hookSpecificOutput"]["additionalContext"], engine.UNCHECKED)
        with mock.patch.object(bounded, "call", timed_out):
            out = self.hook("sudo " * (self.BIG // 5) + "zap x", "warn")
        assert out is not None
        self.assertNotIn("Careful.", json.dumps(out))
        wide = "zap x; " + "; ".join(f"bash -c 'echo {n}'" for n in range(140))
        for call in range(3):
            hit = self.hook(wide, "hit")
            assert hit is not None
            context = hit["hookSpecificOutput"]["additionalContext"]
            self.assertEqual("Careful." in context, call == 0)
            self.assertIn(engine.UNCHECKED, context)
        self.put(self.gpath, {"rules": {"z": {"match": {"command": "zap"}, "message": "No zap."}}})
        denied = self.hook(wide, "deny")
        self.assertTrue(is_denied(denied))
        self.assertNotIn("could not fully analyse", deny_text(denied))

    def test_an_unparsable_command_gets_the_notice_unless_a_rule_matched(self) -> None:
        for command in ("echo 'unterminated", "if x; then y", "echo $("):
            with self.subTest(command=command):
                out = self.hook(command, "unparsed")
                assert out is not None
                self.assertEqual(out["hookSpecificOutput"]["additionalContext"], engine.UNCHECKED)
        self.assertIsNone(self.hook("echo ok", "parsed"))
        denied = self.hook("zap 'x", "matched")
        self.assertTrue(is_denied(denied))
        self.assertNotIn("could not fully analyse", deny_text(denied))

    def test_engine_failures_are_keyed_by_class_and_repeat_after_ten_minutes(self) -> None:
        self.break_engine("it misparsed a test command")
        first = self.hook("zap x", "s")
        assert first is not None
        self.assertIsNone(self.hook("zap y", "s"))
        real_time = time.time
        with mock.patch.object(engine.time, "time", lambda: real_time() + 601):
            again = self.hook("zap z", "s")
        assert again is not None
        self.assertIn("rules engine failed", again["systemMessage"])

    def test_an_unwritable_session_warns_on_every_failure(self) -> None:
        self.break_engine()
        with mock.patch.object(engine.store, "write", side_effect=OSError("read-only")):
            for _ in range(2):
                self.assertIn("rules engine failed", json.dumps(self.hook("zap x", "ro")))

    def test_rule_test_names_the_failure(self) -> None:
        self.break_engine("it misparsed a test command")
        rule = json.dumps({"match": {"command": "zap"}, "message": "m"})
        self.assertIn("unexpected error: RuntimeError", self.cli("rule", "test", "--json", rule, "zap x")[1])


class Oversize(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": RULES_FOR_OUTAGES})

    def test_a_command_over_the_cap_is_allowed_with_a_notice_and_never_parsed(self) -> None:
        for command in ("echo " + "y" * verdict.MAX_COMMAND_BYTES, "strings " + "é" * verdict.MAX_COMMAND_BYTES,
                        "echo " + "a" * verdict.MAX_COMMAND_BYTES + f"; {K} x"):
            with mock.patch("scanner.Scanner", side_effect=AssertionError("parsed")):
                out = self.hook(command, f"big{len(command)}")
            assert out is not None
            self.assertFalse(is_denied(out))
            self.assertEqual(out["hookSpecificOutput"]["additionalContext"], engine.UNCHECKED)

        out = self.hook("echo " + "y" * (verdict.MAX_COMMAND_BYTES - 100) + f"; {K} x")
        self.assertIn("No kill.", deny_text(out))

class Limits(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"a": {"match": {"command": K}, "message": "No kill."}}})

    def test_nesting_beyond_the_caps_is_noticed_not_denied(self) -> None:
        rule = {"a": policy.Rule.from_json({"match": {"command": K}, "message": "m"})}
        command = f"{K} x"
        for _ in range(9):
            command = "bash -c " + shlex.quote(command)
        wide = "; ".join(f"bash -c 'echo {n}'" for n in range(150))
        for text, cause in ((command, "nests shell strings too deeply"), ("eval " * 30 + f"{K} x", "nests shell strings too deeply"),
                            (wide, "too many shell strings")):
            with self.subTest(cause=cause):
                self.assertIn(cause, matching.evaluate(text, rule).unchecked or "")
                out = self.hook(text, f"limit-{cause}-{len(text)}")
                assert out is not None
                self.assertFalse(is_denied(out))
                self.assertEqual(out["hookSpecificOutput"]["additionalContext"], engine.UNCHECKED)

if __name__ == "__main__":
    unittest.main()
