from __future__ import annotations  # noqa: I001

import json
import os
import sys
import unittest
from typing import Any
from unittest import mock

from helpers import HOOKS, AstIsolated

import bounded
import engine
import matching

K = "pk" + "ill"
DONE, CRASHED, TIMEOUT = bounded.Outcome.DONE, bounded.Outcome.CRASHED, bounded.Outcome.TIMEOUT


def adversarial(n: int) -> dict[str, str]:
    return {
        "open-subst": "$(" * (n // 2) + f" {K} x", "open-subst-words": "$(a " * (n // 4) + K, "ticks": "`" * n,
        "tick-subst": "`$(" * (n // 3), "heredocs": "<<A\n" * (n // 4), "heredoc-open": "cat <<EOF\n" + "a\n" * (n // 2),
        "squote": "'" * n, "dquote": '"' * n, "list": "a;" * (n // 2) + K, "pipes": "a|" * (n // 2) + K,
        "braces": "{ " * (n // 2), "parens": "(" * n, "close": ")" * n, "word": "x" * n, "words": "a " * (n // 2),
        "redir": "<" * n, "amp": "&" * n, "ansi": "$'" * (n // 2), "backslash": "\\" * n,
        "assign-subst": "a=$(" * (n // 4), "balanced": "$(" * (n // 4) + ")" * (n // 4),
        "wrappers": "sudo " * (n // 5) + K, "dollar": "$" * n, "dash-heredoc": "<<-A\n\t" * (n // 6),
    }


RUNNABLE = {"ticks", "heredocs", "heredoc-open", "squote", "dquote", "list", "pipes", "word", "words", "ansi",
            "backslash", "balanced", "wrappers", "dollar", "dash-heredoc"}


def spin() -> str:
    while True:
        pass


def crash() -> str:
    os._exit(3)


def boom() -> str:
    raise ValueError("x")


class Call(unittest.TestCase):
    def test_the_answer_comes_back_and_every_failure_is_classified(self) -> None:
        self.assertEqual(bounded.call(lambda: "x" * 300_000, 10), bounded.Result(DONE, "x" * 300_000))
        for work, outcome in ((spin, TIMEOUT), (crash, CRASHED), (boom, CRASHED), (object, CRASHED)):
            with self.subTest(work=work.__name__):
                self.assertEqual(bounded.call(work, 0.5).outcome, outcome)
        with mock.patch.object(json, "loads", side_effect=ValueError):
            self.assertEqual(bounded.call(lambda: 1, 5).outcome, bounded.Outcome.GARBLED)
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)

    def test_the_parent_keeps_no_state_from_the_child(self) -> None:
        box: list[int] = []
        bounded.call(lambda: box.append(1), 5)
        self.assertEqual(box, [])


class Pathological(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {
            "no-kill": {"match": {"program": K}, "message": "No kill."},
            "rx": {"match": {"regex": r"never-present-\d+"}, "message": "No rx."},
            "nested": {"match": {"ast": {"pattern": f"{K} $$$", "inside": {"kind": "command_substitution",
                                                                           "stopBy": "end"}}}, "message": "No nest."}}})

    def test_a_command_the_parser_cannot_finish_is_denied_at_the_deadline(self) -> None:
        with mock.patch.object(matching, "DEADLINE_SECONDS", 0.5):
            out = self.hook("$(" * 70_000 + ")" * 70_000, "dense")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("command too complex to check (it did not finish within 0.5 seconds)",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_adversarial_shapes_never_slip_through_and_the_largest_are_never_silent(self) -> None:
        for size in (2_000, 10_000):
            for name, text in adversarial(size).items():
                out = self.hook(text + f"; {K} x", f"c{size}{name}")
                if name in RUNNABLE:
                    self.assertTrue(out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny",
                                    f"{name} at {size}")
        with mock.patch.object(matching, "DEADLINE_SECONDS", 2.0):
            for name in ("open-subst", "balanced", "word", "list", "heredoc-open"):
                self.assertIsNotNone(self.hook(adversarial(100_000)[name] + f"; {K} x", f"big{name}"), name)


class Crashes(AstIsolated):
    """A native crash of the library: deny the command that causes it, fail open when the library itself is broken."""

    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"no-kill": {"match": {"program": K}, "message": "No kill."}}})
        broken = mock.patch.object(engine, "report_broken", mock.Mock())
        self.reported = broken.start()
        self.addCleanup(broken.stop)

    def run_hook(self, *results: bounded.Result, session: str = "s1") -> dict[str, Any]:
        with mock.patch.object(bounded, "call", side_effect=list(results)):
            out = self.hook("ls", session=session)
        assert out is not None
        return out

    def test_the_probe_decides_between_one_bad_command_and_a_broken_library(self) -> None:
        crash, slow, ok = bounded.Result(CRASHED), bounded.Result(TIMEOUT), bounded.Result(DONE, True)
        out = self.run_hook(crash, ok)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("this command crashes the parser", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.run_hook(crash, slow, ok, session="s2")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.reported.assert_not_called()
        for results in ((crash, bounded.Result(DONE, False)), (crash, slow, bounded.Result(DONE, False))):
            self.reported.reset_mock()
            out = self.run_hook(*results, session=f"b{len(results)}")
            self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
            text = out["systemMessage"]
            self.assertIn("crashes even on a trivial command", text)
            self.assertIn("being rebuilt", text)
            self.assertEqual(out["hookSpecificOutput"]["additionalContext"], text)
            self.reported.assert_called_once()
            for banned in ("claude plugin disable", "guardrails disable"):
                self.assertNotIn(banned, text)

    def test_a_library_that_never_answers_the_probe_is_unverified_not_broken(self) -> None:
        crash, slow = bounded.Result(CRASHED), bounded.Result(TIMEOUT)
        with mock.patch.object(bounded, "call", side_effect=[crash, slow, slow]) as call:
            out = self.hook("ls")
        assert out is not None
        self.assertEqual(call.call_count, 3)
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("could not verify the matcher (timed out)", out["systemMessage"])
        self.assertNotIn("rebuilt", out["systemMessage"])
        self.reported.assert_not_called()

    def test_the_probes_never_run_past_the_hooks_own_time_budget(self) -> None:
        with mock.patch.object(bounded, "call", side_effect=[bounded.Result(CRASHED)]) as call, \
                mock.patch.object(matching, "HOOK_SECONDS", matching.HEADROOM_SECONDS + 0.2):
            out = self.hook("ls")
        assert out is not None
        self.assertEqual(call.call_count, 1)
        self.assertIn("could not verify the matcher", out["systemMessage"])
        timeout = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
        self.assertEqual(timeout, 10)
        self.assertLessEqual(matching.DEADLINE_SECONDS + matching.PROBE_SECONDS + matching.HEADROOM_SECONDS, timeout)

    def test_a_real_abort_in_the_library_is_classified_end_to_end(self) -> None:
        stub = self.tmp / "stub"
        stub.mkdir()
        (stub / "ast_grep_py.py").write_text("import os\nos.abort()\n")
        with mock.patch.dict(sys.modules), mock.patch.object(sys, "path", [str(stub), *sys.path]):
            for name in ("ast_grep_py", "scanner", "rulebuilder"):
                sys.modules.pop(name, None)
            out = self.hook("ls")
        assert out is not None
        self.assertIn("crashes even on a trivial command", out["systemMessage"])
        self.reported.assert_called_once()

    def test_a_checker_that_cannot_be_started_is_not_a_broken_runtime(self) -> None:
        with mock.patch.object(bounded, "call", side_effect=OSError("fork failed")):
            out = self.hook("ls")
        assert out is not None
        self.assertIn("the checker could not be started", out["systemMessage"])
        self.reported.assert_not_called()


if __name__ == "__main__":
    unittest.main()
