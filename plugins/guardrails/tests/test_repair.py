from __future__ import annotations  # noqa: I001

import json
import shlex
import unittest
from typing import Any
from unittest import mock

from ast_grep_py import SgRoot
from helpers import ROOT, AstIsolated

import matching
import policy

GRAMMAR_BUG = "echo a | sort | cat\nfoo && bar"


def rule_of(match: dict[str, Any]) -> policy.Rule:
    return policy.Rule.from_json({"match": match, "message": "m"})


class RepairCase(AstIsolated):
    parses = 0

    def parse(self, text: str, budget: int = 64) -> Any:
        import repair

        self.parses = 0
        return repair.parse(text, repair.Budget(budget), self.reparse)

    def reparse(self, source: str) -> Any:
        self.parses += 1
        return SgRoot(source, "bash").root()


class NewlineRepair(RepairCase):
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


class HeredocRepair(RepairCase):
    def names(self, root: Any) -> list[str]:
        return sorted(node.field("name").text() for node in root.find_all(kind="command") if node.field("name"))

    def errors(self, root: Any) -> int:
        return len(root.find_all(kind="ERROR"))

    def test_a_redirect_after_the_marker_moves_before_the_heredoc_operator(self) -> None:
        text = "python3 - <<'E' 2>&1 | tail -3\nprint(1)\nE"
        parsed = self.parse(text)
        self.assertTrue(parsed.repaired)
        self.assertEqual(parsed.text, "python3 - 2>&1 <<'E' | tail -3\nprint(1)\nE")
        self.assertEqual(self.names(parsed.root), ["python3", "tail"])
        self.assertEqual(self.errors(parsed.root), 0)
        self.assertEqual(self.parses, 2)

    def test_an_operator_after_the_marker_moves_the_rest_of_the_line_after_the_heredoc(self) -> None:
        for text, expected in (("cat <<'E' ; echo hi\nbody\nE", "cat <<'E'\nbody\nE\necho hi"),
                               ("echo a <<'E' ;\nx\nE\nls", "echo a <<'E'\nx\nE\nls"),
                               ("cat <<'E' > f ; ls\nx\nE", "cat > f <<'E'\nx\nE\nls")):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertTrue(parsed.repaired)
                self.assertEqual(parsed.text, expected)
                self.assertEqual(self.errors(parsed.root), 0)

    def test_both_quirks_in_one_command_are_repaired_together(self) -> None:
        text = "a | b | cat\npython3 - <<'E' 2>&1 | tail -3\nprint(1)\nE\nfoo && bar"
        parsed = self.parse(text)
        self.assertEqual(parsed.text, "a | b | cat;\npython3 - 2>&1 <<'E' | tail -3\nprint(1)\nE\nfoo && bar")
        self.assertEqual(self.names(parsed.root), ["a", "b", "bar", "cat", "foo", "python3", "tail"])

    def test_a_heredoc_without_a_swallowed_redirect_is_untouched_and_parsed_once(self) -> None:
        for text in ("cat <<'E'\nbody\nE\nls", "cat <<'E' | tail -3\nbody\nE", "cat <<'E' > out && echo done\nbody\nE",
                     "cat <<'EOF' | sh\npkill x\nEOF", "cat > q.sql <<'EOF'\nSELECT 1\nEOF\npodman exec -i db psql -f - < q.sql | gzip > q.gz",
                     "cat <<'E'\npython3 - <<'X' 2>&1 | tail\nE", "bash <<'E'\npkill x\nE"):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertFalse(parsed.repaired)
                self.assertIs(parsed.text, text)
                self.assertEqual(self.parses, 1)

    def test_a_heredoc_started_in_the_background_keeps_its_commands(self) -> None:
        text = "cd d\npython3 - <<'PY' &\nimport time\nPY\nsleep 0.7\ngo test | grep x | head -3\nwait"
        before = self.names(SgRoot(text, "bash").root())
        parsed = self.parse(text)
        self.assertEqual(self.names(parsed.root), before)
        self.assertEqual(self.errors(parsed.root), 0)
        self.assertEqual(parsed.text, text.replace(" &\n", "\n", 1))

    def test_a_clean_command_is_byte_identical(self) -> None:
        for text in ("python3 - 2>&1 <<'E' | tail -3\nprint(1)\nE", "echo a\necho b", "ls | sort"):
            with self.subTest(text=text):
                self.assertIs(self.parse(text).text, text)
                self.assertEqual(self.parses, 1)

    def test_a_symptom_across_lines_is_left_alone(self) -> None:
        import repair

        lines = ["cat <<'E' \\", "  2>&1 | tail", "x", "E"]
        text = "\n".join(lines)
        root = SgRoot(text, "bash").root()
        for symptom in repair.heredoc_symptoms(root):
            self.assertIsNone(repair.move_heredoc_tail(list(lines), symptom))

    def test_rules_see_the_commands_the_parser_hid(self) -> None:
        tail = rule_of({"command": "tail"})
        self.assertIsNotNone(matching.evaluate("python3 - <<'E' 2>&1 | tail -3\nprint(1)\nE", {"r": tail}).kinds["r"])
        later = rule_of({"command": "pkill"})
        self.assertIsNotNone(matching.evaluate("cat <<'E' ; pkill x\nbody\nE", {"r": later}).kinds["r"])
        self.assertIsNone(matching.evaluate("cat <<'E'\npkill x\nE", {"r": later}).kinds["r"])

    def test_a_repaired_swallowed_newline_or_heredoc_inside_a_joined_runner_string_is_scanned(self) -> None:
        pkill = rule_of({"command": "pkill"})
        newline = "echo a | sort | cat\nfoo && pkill x"
        heredoc = "python3 - <<'E' 2>&1 | tail -3\nprint(1)\nE\npkill x"
        for script in (newline, heredoc):
            for runner in ("ssh host {}", "watch {}", "eval {}", "bash -c {}"):
                command = runner.format(shlex.quote(script))
                with self.subTest(command=command):
                    self.assertIsNotNone(matching.evaluate(command, {"r": pkill}).kinds["r"])
        out = self.cli("rule", "ast", "ssh host " + shlex.quote(newline))[1]
        self.assertIn("2 from shell strings, 2 repaired)", out)

    def test_rule_ast_shows_the_repaired_heredoc(self) -> None:
        code, out, _ = self.cli("rule", "ast", "python3 - <<'E' 2>&1 | tail -3\nprint(1)\nE")
        self.assertEqual(code, 0)
        self.assertIn("units: 1 (1 as written, 0 from shell strings, 1 repaired)", out)
        self.assertIn("python3 - 2>&1 <<'E' | tail -3", out)


if __name__ == "__main__":
    unittest.main()
