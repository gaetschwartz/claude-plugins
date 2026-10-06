from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

import matching
import policy
from ast_grep_py import SgRoot
from helpers import ROOT, AstIsolated

GRAMMAR_BUG = "echo a | sort | cat\nfoo && bar"


def rule_of(match: dict[str, Any]) -> policy.Rule:
    return policy.Rule.from_json({"match": match, "message": "m"})


class NewlineRepair(AstIsolated):
    parses = 0

    def parse(self, text: str, budget: int = 64) -> Any:
        import repair

        self.parses = 0
        return repair.parse(text, repair.Budget(budget), self.reparse)

    def reparse(self, source: str) -> Any:
        self.parses += 1
        return SgRoot(source, "bash").root()

    def last_arguments(self, text: str) -> list[str]:
        import scanner

        parsed = self.parse(text)
        cat = next(node for node in scanner.commands_named(parsed.root, ["cat"]))
        return [node.text() for node in scanner.arguments(cat)]

    def test_the_grammar_bug_swallows_the_next_statement_and_the_repair_ends_the_statement(self) -> None:
        import scanner

        swallowed = next(iter(scanner.commands_named(SgRoot(GRAMMAR_BUG, "bash").root(), ["cat"])))
        self.assertEqual([node.text() for node in scanner.arguments(swallowed)], ["foo"])
        parsed = self.parse(GRAMMAR_BUG)
        self.assertTrue(parsed.repaired)
        self.assertEqual(parsed.text, "echo a | sort | cat;\nfoo && bar")
        self.assertEqual(self.last_arguments(GRAMMAR_BUG), [])

    def test_the_separator_goes_before_a_trailing_comment_and_after_the_last_real_token(self) -> None:
        for text, expected in (("a | b | cat # note\nfoo && bar", "a | b | cat; # note\nfoo && bar"),
                               ("a | b | cat\n  # note\nfoo && bar", "a | b | cat;\n  # note\nfoo && bar"),
                               ("a | b | cat\n\nfoo && bar", "a | b | cat;\n\nfoo && bar")):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).text, expected)

    def test_every_gap_of_a_command_is_repaired_in_one_pass(self) -> None:
        text = "a | b | cat\nc | d | cat\nfoo && bar"
        parsed = self.parse(text)
        self.assertEqual(parsed.text, "a | b | cat;\nc | d | cat;\nfoo && bar")
        self.assertEqual(self.parses, 2)

    def test_columns_are_characters_not_bytes(self) -> None:
        for text, expected in (("echo é | sort | cat # café\nfoo && bar", "echo é | sort | cat; # café\nfoo && bar"),
                               ("echo 日本 | sort | cat\nfoo && bar", "echo 日本 | sort | cat;\nfoo && bar"),
                               ("echo 日本語 é | sort | cat # 日本\nfoo && bar", "echo 日本語 é | sort | cat; # 日本\nfoo && bar")):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).text, expected)

    def test_a_backslash_continuation_is_not_a_gap(self) -> None:
        for text in ("a | b | cat \\\n  --flag", "echo hi \\\n  there && foo", "make \\\n  -j4 | tail \\\n  -5"):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertFalse(parsed.repaired)
                self.assertEqual(parsed.text, text)

    def test_a_heredoc_body_and_a_multi_line_string_are_untouched(self) -> None:
        for text in ("cat <<'E'\na | b | cat\nfoo && bar\nE", "echo 'a | b | cat\nfoo && bar'",
                     "cat <<E > out\nx | y | cat\nz && w\nE\nls"):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertFalse(parsed.repaired)
                self.assertEqual(parsed.text, text)

    def test_a_command_without_a_gap_is_parsed_once_and_returned_as_written(self) -> None:
        for text in ("ls | sort | cat", "echo a\necho b", "a | b | cat;\nfoo && bar", "if x; then\n  y\nfi"):
            with self.subTest(text=text):
                parsed = self.parse(text)
            self.assertEqual(self.parses, 1)
            self.assertIs(parsed.text, text)
            self.assertFalse(parsed.repaired)

    def test_a_repair_that_does_not_add_commands_is_refused(self) -> None:
        text = 'echo "a\nb" | cat'
        with mock.patch("repair.newline_gaps", side_effect=[[(0, 6)], []]):
            parsed = self.parse(text)
        self.assertFalse(parsed.repaired)
        self.assertEqual(parsed.text, text)

    def test_a_repair_that_leaves_a_gap_is_refused(self) -> None:
        with mock.patch("repair.newline_gaps", return_value=[(0, 3)]):
            parsed = self.parse(GRAMMAR_BUG)
        self.assertFalse(parsed.repaired)
        self.assertEqual(parsed.text, GRAMMAR_BUG)

    def test_the_repair_spends_the_parse_budget_and_gives_up_without_it(self) -> None:
        import repair

        budget = repair.Budget(1)
        self.assertTrue(repair.parse(GRAMMAR_BUG, budget, self.reparse).repaired)
        self.assertEqual(budget.left, 0)
        self.assertFalse(repair.parse(GRAMMAR_BUG, budget, self.reparse).repaired)

    def test_rules_see_the_real_last_stage_and_statement(self) -> None:
        swallowed = rule_of({"command": "cat", "args": "foo"})
        self.assertIsNone(matching.evaluate(GRAMMAR_BUG, {"r": swallowed}).kinds["r"])
        self.assertIsNotNone(matching.evaluate("echo a | sort | cat foo", {"r": swallowed}).kinds["r"])
        preset = json.loads((ROOT / "presets" / "shell-hygiene.json").read_text())
        pipe_status = policy.Rule.from_json(preset["rules"]["pipe-status"])
        self.assertIsNone(matching.evaluate("ls | sort | cat\nfoo && echo ok", {"r": pipe_status}).kinds["r"])
        self.assertIsNotNone(matching.evaluate("make | sort | cat\nmake | tail && echo ok", {"r": pipe_status}).kinds["r"])

    def test_the_matched_statement_is_the_one_in_the_repaired_text(self) -> None:
        rule = rule_of({"command": "pkill"})
        replay = matching.matched_statement("a | b | cat\nfoo && pkill x", rule)
        assert replay.matched is not None
        self.assertEqual(replay.matched.text, "foo && pkill x")

    def test_rule_ast_shows_the_repaired_unit(self) -> None:
        code, out, _ = self.cli("rule", "ast", GRAMMAR_BUG)
        self.assertEqual(code, 0)
        self.assertIn("units: 1 (1 as written, 0 from shell strings, 1 repaired)", out)
        self.assertIn("tree: command, repaired, source: echo a | sort | cat;", out)
        clean = self.cli("rule", "ast", "echo a | sort | cat")[1]
        self.assertIn("units: 1 (1 as written, 0 from shell strings)", clean)

    def test_a_shell_string_with_the_bug_is_repaired_as_its_own_unit(self) -> None:
        code, out, _ = self.cli("rule", "ast", "bash -c 'a | b | cat\nfoo && bar'")
        self.assertEqual(code, 0)
        self.assertIn("units: 2 (1 as written, 1 from shell strings, 1 repaired)", out)
        self.assertIn("tree: shell string, repaired", out)


if __name__ == "__main__":
    unittest.main()
