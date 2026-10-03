from __future__ import annotations  # noqa: I001

import os
import time
import unittest
from unittest import mock

from helpers import AstIsolated, Isolated

import bounded
import matching

K = "pk" + "ill"


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


class Call(unittest.TestCase):
    def test_the_answer_comes_back(self) -> None:
        self.assertEqual(bounded.call(lambda: "x" * 300_000, 10), bounded.Result(bounded.Outcome.DONE, "x" * 300_000))

    def test_a_child_that_never_finishes_is_killed_and_reaped_at_the_deadline(self) -> None:
        started = time.monotonic()
        self.assertEqual(bounded.call(spin, 0.3).outcome, bounded.Outcome.TIMEOUT)
        self.assertLess(time.monotonic() - started, 5.0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)

    def test_a_child_that_dies_or_raises_is_reported(self) -> None:
        def boom() -> str:
            raise ValueError("x")

        for work in (crash, boom):
            with self.subTest(work=work.__name__):
                self.assertEqual(bounded.call(work, 5).outcome, bounded.Outcome.CRASHED)

    def test_the_parent_keeps_no_state_from_the_child(self) -> None:
        box: list[int] = []

        def work() -> str:
            box.append(1)
            return "ok"

        bounded.call(work, 5)
        self.assertEqual(box, [])


class Pathological(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {
            "no-kill": {"match": {"program": K}, "message": "No kill."},
            "rx": {"match": {"regex": r"never-present-\d+"}, "message": "No rx."},
            "nested": {"match": {"ast": {"pattern": f"{K} $$$", "inside": {"kind": "command_substitution",
                                                                           "stopBy": "end"}}}, "message": "No nest."}}})

    def test_a_command_the_parser_cannot_finish_is_denied_and_the_hook_answers_at_the_deadline(self) -> None:
        with mock.patch.object(matching, "DEADLINE_SECONDS", 0.5):
            started = time.monotonic()
            out = self.hook("$(" * 70_000 + ")" * 70_000, "dense")
            elapsed = time.monotonic() - started
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("command too complex to check (it did not finish within 0.5 seconds)",
                      out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertLess(elapsed, 8.0)

    def test_adversarial_shapes_never_slip_through_and_the_deny_rule_still_fires(self) -> None:
        for size in (2_000, 10_000):
            for name, text in adversarial(size).items():
                out = self.hook(text + f"; {K} x", f"c{size}{name}")
                if name in RUNNABLE:
                    self.assertTrue(out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny",
                                    f"{name} at {size}")

    def test_the_largest_shapes_are_never_silent(self) -> None:
        with mock.patch.object(matching, "DEADLINE_SECONDS", 2.0):
            for name in ("open-subst", "balanced", "word", "list", "heredoc-open"):
                out = self.hook(adversarial(100_000)[name] + f"; {K} x", f"big{name}")
                self.assertIsNotNone(out, name)


class Budget(Isolated):
    def test_the_checker_deadline_is_below_the_hook_timeout(self) -> None:
        import json

        from helpers import HOOKS

        timeout = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
        self.assertLess(matching.DEADLINE_SECONDS * 1.5, timeout)


if __name__ == "__main__":
    unittest.main()
