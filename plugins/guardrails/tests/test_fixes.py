from __future__ import annotations  # noqa: I001

import io
import json
import shlex
import sys
import time
import unittest
from typing import Any
from unittest import mock

from helpers import GREP_RECURSIVE, HOOKS, AstIsolated

import bounded
import engine
import matching
import policy
import verdict
import wrappers
from test_corpus import ALLOW, DENY
from verdict import Kind, Refusal

K = "pk" + "ill"
BY_NAME: dict[str, Any] = {"any": [{"pattern": f"{K} $$$"}, {"pattern": "killall $$$"}]}
PROGRAM_RULE = policy.Rule.from_json({"match": {"program": [K, "killall"]}, "message": "m"})


def deny_text(out: dict[str, Any] | None) -> str:
    assert out is not None
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def is_denied(out: dict[str, Any] | None) -> bool:
    return out is not None and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


FORMS = [
    "xargs -I{} KILL {}", "xargs -I{} sh -c 'KILL {}'", "env - KILL x", 'env "A=1 B" KILL x', "bash -c -- 'KILL x'",
    "sudo -nu bob KILL x", "sudo -Eu bob KILL x", "sudo -iu bob KILL x", "xargs -i KILL {}", "cat <<EOF\n$(KILL x)\nEOF",
    "echo $(cat <<EOF\n$(KILL x)\nEOF\n)", 'echo "$(nm $(KILL z))"', "sudo -u bob -- KILL x",
]


class Forms(AstIsolated):
    def test_program_sees_every_form_and_quoted_heredocs_stay_data(self) -> None:
        for command in FORMS:
            with self.subTest(command=command):
                self.assertIsNotNone(matching.evaluate(command.replace("KILL", K), {"r": PROGRAM_RULE}).kinds["r"])
        for command in (f"cat <<'EOF'\n$({K} x)\nEOF", f"cat <<\"EOF\"\n`{K} x`\nEOF"):
            self.assertIsNone(matching.evaluate(command, {"r": PROGRAM_RULE}).kinds["r"], command)


class Layering(AstIsolated):
    def managed_rules(self) -> None:
        self.put(self.mpath, {"rules": {"no-kill": {"match": {"program": K}, "message": "No kill by name."},
                                        "no-strings": {"match": {"program": "strings", "args": "^strings -n"},
                                                       "message": "No strings."}}})

    def test_lower_layers_only_add_wrapper_names(self) -> None:
        self.managed_rules()
        self.put(self.ppath, {"wrappers": {"mywrap": {}, "sudo": {}, K: {}}})
        for n, command in enumerate([f"sudo -E {K} x", f"nohup {K} x", f"env -i {K} x", f"mywrap -x {K} x", f"{K} x",
                                     "strings -n 4 /bin/ls"]):
            self.assertTrue(is_denied(self.hook(command, f"b{n}")), command)

    def test_a_global_layer_is_held_to_the_same_rule(self) -> None:
        self.managed_rules()
        self.put(self.gpath, {"wrappers": {"sudo": {}, K: {}}})
        self.assertTrue(is_denied(self.hook(f"sudo -E {K} x", "g1")))
        self.assertTrue(is_denied(self.hook(f"{K} x", "g2")))

    def test_the_cli_takes_names_and_refuses_builtins(self) -> None:
        code, _, err = self.cli("wrapper", "add", "sudo")
        self.assertEqual(code, 2)
        self.assertIn("already built in", err)
        self.assertFalse(self.gpath.exists())

    def test_adding_names_never_reduces_detection(self) -> None:
        rules = {"p": PROGRAM_RULE, "s": policy.Rule.from_json({"match": {"program": "strings"}, "message": "m"}),
                 "g": policy.Rule.from_json({"match": {"ast": GREP_RECURSIVE}, "message": "m"})}
        corpus = [*DENY, *ALLOW[:20], f"sudo -E {K} x", f"nohup {K} x", f"timeout 5 {K} a", f"bash -c '{K} x'",
                  "grep -r foo .", "sudo grep -rn foo .", "strings -n 4 /bin/ls"]
        base = {c: matching.evaluate(c, rules).kinds for c in corpus}
        names = wrappers.resolve([("g", {"wrappers": {K: {}, "strings": {}, "grep": {}, "mywrap": {}}})])[0]
        for command in corpus:
            after = matching.evaluate(command, rules, names).kinds
            for rid, before in base[command].items():
                if before is not None:
                    self.assertIsNotNone(after[rid], f"{rid} stopped matching {command!r}")


RULES_FOR_OUTAGES: dict[str, Any] = {
    "strings": {"match": {"program": "strings"}, "message": "No strings."},
    "ast": {"match": {"ast": BY_NAME}, "message": "No kill."},
    "pipe": {"match": {"regex": r"curl [^|]*\| *sh"}, "message": "No pipe."}}


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
        self.put(self.mpath, {"rules": {"m-zap": {"match": {"program": "zap"}, "message": "No zap."}}})
        self.break_engine()
        text = self.both_channels(self.hook("zap x", "m"))
        self.assertIn("1 of them are MANAGED rules, which fail open too", text)

    def test_regex_rules_are_not_enforced_while_the_engine_fails_either(self) -> None:
        self.break_engine()
        out = self.hook("curl x | sh", "r")
        self.assertFalse(is_denied(out))
        self.assertIn("rules engine failed", self.both_channels(out))
        self.assertIn("pipe", out["systemMessage"] if out else "")

    def test_a_rule_that_does_not_compile_is_skipped_and_reported_once(self) -> None:
        self.put(self.gpath, {"rules": {"bad": {"match": {"ast": {"kind": "no_such_kind"}}, "message": "m"},
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


def timed_out(work: Any, seconds: float) -> bounded.Result:
    return bounded.Result(bounded.Outcome.TIMEOUT)


class FailurePolicy(AstIsolated):
    """What a failing engine or checker means for a command."""

    BIG = 9 * 1024

    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"z": {"match": {"program": "zap"}, "message": "No zap."}}})

    def test_a_checker_that_does_not_finish_denies_any_command_that_needs_the_parser(self) -> None:
        with mock.patch.object(bounded, "call", timed_out):
            big = self.hook("sudo " * (self.BIG // 5) + "zap x", "big")
            small = self.hook("sudo " * 20 + "zap x", "small")
        for out in (big, small):
            self.assertTrue(is_denied(out))
            self.assertIn("command too complex to check (it did not finish within", deny_text(out))
            self.assertIn("script file", deny_text(out))

    def test_regex_rules_wait_for_the_bounded_checker_like_every_other_rule(self) -> None:
        self.put(self.gpath, {"rules": {"pipe": RULES_FOR_OUTAGES["pipe"]}})
        with mock.patch.object(bounded, "call", timed_out):
            out = self.hook("curl x | sh")
        self.assertTrue(is_denied(out))
        self.assertIn("did not finish within", deny_text(out))

    def test_a_hit_stands_when_a_cap_is_hit_later(self) -> None:
        command = "zap x; " + "; ".join(f"bash -c 'echo {n}'" for n in range(80))
        ev = matching.evaluate(command, {"z": policy.Rule.from_json({"match": {"program": "zap"}, "message": "m"})})
        self.assertEqual((ev.kinds["z"], ev.refusal_kind), (Kind.DIRECT, Refusal.COMPLEX))
        out = self.hook(command, "hit")
        self.assertTrue(is_denied(out))
        self.assertIn("No zap.", deny_text(out))

    def test_warn_only_rules_never_turn_a_refusal_into_a_denial(self) -> None:
        self.put(self.gpath, {"rules": {"z": {"match": {"program": "zap"}, "message": "Careful.", "action": "warn"}}})
        with mock.patch.object(bounded, "call", timed_out):
            out = self.hook("sudo " * (self.BIG // 5) + "zap x", "warn")
        assert out is not None
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("allowed because only warn rules", out["systemMessage"])
        huge = self.hook("echo " + "y" * (verdict.MAX_COMMAND_BYTES + 1), "huge")
        assert huge is not None
        self.assertNotIn("permissionDecision", huge["hookSpecificOutput"])

    def test_engine_failures_are_keyed_by_class_and_repeat_after_ten_minutes(self) -> None:
        self.break_engine("it misparsed a test command")
        first = self.hook("zap x", "s")
        assert first is not None
        self.assertEqual(self.get(self.gpath)["sessions"]["s"]["engineFailure"]["kind"], "engine")
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

    def test_status_and_rule_test_name_the_last_failure(self) -> None:
        self.break_engine("it misparsed a test command")
        self.hook("zap x", "s")
        out = self.cli("status", "--session-id", "s")[1]
        self.assertIn("last engine failure", out)
        self.assertIn("engine", out)
        rule = json.dumps({"match": {"program": "zap"}, "message": "m"})
        self.assertIn("unexpected error: RuntimeError", self.cli("rule", "test", "--json", rule, "zap x")[1])


class Oversize(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": RULES_FOR_OUTAGES})

    def test_a_command_over_the_cap_is_denied_and_never_parsed(self) -> None:
        for command in ("echo " + "y" * verdict.MAX_COMMAND_BYTES, "strings " + "é" * verdict.MAX_COMMAND_BYTES,
                        "echo " + "a" * verdict.MAX_COMMAND_BYTES + f"; {K} x"):
            with mock.patch("scanner.Scanner", side_effect=AssertionError("parsed")):
                out = self.hook(command, f"big{len(command)}")
            self.assertTrue(is_denied(out))
            self.assertIn("command too large to check", deny_text(out))

    def test_regex_rules_are_judged_by_the_same_checker_so_size_denies_them_too(self) -> None:
        self.put(self.gpath, {"rules": {"pipe": RULES_FOR_OUTAGES["pipe"]}})
        out = self.hook("echo " + "y" * (verdict.MAX_COMMAND_BYTES + 10))
        self.assertIn("command too large to check", deny_text(out))

    def test_just_under_the_cap_is_analysed(self) -> None:
        out = self.hook("echo " + "y" * (verdict.MAX_COMMAND_BYTES - 100) + f"; {K} x")
        self.assertTrue(is_denied(out))
        self.assertIn("No kill.", deny_text(out))

    def test_the_cap_counts_bytes(self) -> None:
        out = self.hook("echo " + "日" * (verdict.MAX_COMMAND_BYTES // 3 + 10))
        self.assertTrue(is_denied(out))


class Limits(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"a": {"match": {"program": K}, "message": "No kill."}}})

    def test_seven_nested_shell_strings_are_analysed(self) -> None:
        command = f"{K} x"
        for _ in range(7):
            command = "bash -c " + shlex.quote(command)
        self.assertTrue(is_denied(self.hook(command)))

    def test_nesting_beyond_the_caps_is_refused_not_passed(self) -> None:
        command = f"{K} x"
        for _ in range(9):
            command = "bash -c " + shlex.quote(command)
        out = self.hook(command, "deep")
        self.assertTrue(is_denied(out))
        self.assertIn("command too complex to check", deny_text(out))
        self.assertIn("nests shell strings too deeply", deny_text(out))
        out = self.hook("eval " * 30 + f"{K} x", "evals")
        self.assertIn("nests shell strings too deeply", deny_text(out))
        out = self.hook("; ".join(f"bash -c 'echo {n}'" for n in range(100)), "wide")
        self.assertIn("too many shell strings", deny_text(out))

    def test_a_clean_command_with_many_shell_strings_is_not_limited(self) -> None:
        self.assertIsNone(self.hook("bash -c 'true'; " * 200 + "ls"))
        self.assertIsNone(self.hook("sudo true; " * 20 + "ls"))

    def test_too_many_wrapper_variants_are_refused_at_any_size(self) -> None:
        out = self.hook("sudo " * 2100 + "ls", "variants")
        self.assertTrue(is_denied(out))
        self.assertIn("unwraps into too many command variants", deny_text(out))


class MonitorCoverage(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.mpath, {"rules": {"no-strings": {"match": {"program": "strings"}, "message": "No strings."},
                                        "soft": {"match": {"program": "nm"}, "message": "Prefer otool.",
                                                 "action": "warn"}}})

    def test_a_managed_rule_denies_a_monitor_command(self) -> None:
        out = self.hook("strings /bin/ls", tool="Monitor")
        self.assertTrue(is_denied(out))
        self.assertIn("[guardrails:no-strings (managed)]", deny_text(out))

    def test_a_monitor_call_with_only_ws_is_ignored(self) -> None:
        for tool_input in ({"ws": "ws://localhost:1/x"}, {}, None, [], {"command": ""}, {"command": 3}):
            self.assertIsNone(self.hook("x", tool="Monitor", tool_input=tool_input) if tool_input is not None
                              else self.hook("x", tool="Monitor", tool_input=[]))

    def test_other_tools_are_ignored(self) -> None:
        self.assertIsNone(self.hook("strings x", tool="Read"))
        self.assertIsNone(self.hook("strings x", tool="Write"))

    def test_warn_once_and_retry_behave_the_same(self) -> None:
        first = self.hook("nm a.out", tool="Monitor")
        assert first is not None
        self.assertIn("Prefer otool.", first["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook("nm b.out", tool="Monitor"))
        self.assertIsNotNone(self.hook("nm b.out", session="other", tool="Monitor"))
        self.put(self.gpath, {"rules": {"r": {"match": {"program": "sed"}, "message": "No sed.",
                                               "retry": "same-command"}}})
        self.assertTrue(is_denied(self.hook("sed -i x", "rr", tool="Monitor")))
        self.assertIsNone(self.hook("sed -i x", "rr", tool="Monitor"))
        self.assertTrue(is_denied(self.hook("sed -i y", "rr", tool="Monitor")))

    def test_a_monitor_acknowledgement_is_shared_with_bash(self) -> None:
        self.put(self.gpath, {"rules": {"r": {"match": {"program": "sed"}, "message": "No sed.",
                                               "retry": "same-command"}}})
        self.assertTrue(is_denied(self.hook("sed -i x", "sh", tool="Bash")))
        self.assertIsNone(self.hook("sed -i x", "sh", tool="Monitor"))

    def test_hooks_json_matches_bash_and_monitor_and_the_deadlines_fit_its_timeout(self) -> None:
        hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PreToolUse"][0]
        self.assertEqual(hooks["matcher"], "Bash|Monitor")
        self.assertEqual(hooks["hooks"][0]["timeout"], matching.HOOK_SECONDS)
        self.assertLessEqual(matching.DEADLINE_SECONDS + matching.PROBE_SECONDS + matching.HEADROOM_SECONDS,
                             matching.HOOK_SECONDS)


if __name__ == "__main__":
    unittest.main()
