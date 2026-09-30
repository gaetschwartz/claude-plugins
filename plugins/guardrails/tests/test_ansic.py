from __future__ import annotations  # noqa: I001

import unittest
from typing import Any

from helpers import AstIsolated, Isolated

import ansic
import matching
import policy
from shellwords import simple_commands

K = "pk" + "ill"

DECODE = [
    (r"p\x6bill", K), (r"\160\153ill", K), (r"pkill", K), (r"\U00000070kill", K), (r"p\x6b\x69ll", K),
    (r"a\nb", "a\nb"), (r"\t\e\a\b\f\r\v", "\t\x1b\x07\x08\x0c\r\x0b"), (r"\\ \' \" \?", "\\ ' \" ?"),
    (r"\cA\cz\c?", "\x01\x1a\x7f"), (r"\x41\x4", "A\x04"), (r"\101\18", "A\x018"), (r"\q\y", r"\q\y"),
    (r"\x", r"\x"), (r"\u", r"\u"), (r"tail\\", "tail\\"), (r"\xc3\xa9", "é"), (r"é", "é"),
    (r"ab\0cd", "ab"), (r"ab\x00cd", "ab"), (r"\U0001F600", "\U0001F600"), (r"\U00110000x", "x"),
]
NAMES = [
    (r"$'p\x6bill' x", K, ["x"]), (r"$'\160kill' -9 x", K, ["-9", "x"]), (r"$'p'ki$'ll' x", K, ["x"]),
    (r"sudo $'p\x6bill' x", K, ["x"]), (r"env A=1 nice -n 5 $'pkil\x6c' x", K, ["x"]),
    (r"bash -c $'p\x6bill x'", K, ["x"]), (r"xargs $'pkill'", K, []), (r"echo $(  $'p\x6bill' x)", K, ["x"]),
]
ARGS = [(r"rm $'-r\x66' $'/tmp/x'", "rm", ["-rf", "/tmp/x"]), (r"echo $'a b' c", "echo", ["a b", "c"]),
        (r"echo $'it\'s'", "echo", ["it's"]), (r"echo $'$(pkill x)'", "echo", ["$(pkill x)"]),
        (r"echo $'line\nbreak'", "echo", ["line\nbreak"])]


class Decode(unittest.TestCase):
    def test_table(self) -> None:
        for body, want in DECODE:
            with self.subTest(body=body):
                self.assertEqual(ansic.decode(body), want)

    def test_quoted_is_one_safe_shell_word(self) -> None:
        import shlex

        for body in (r"it\'s", r"a\nb", r"p\x6bill x", "plain", ""):
            self.assertEqual(shlex.split(ansic.quoted(body)), [ansic.decode(body)])


class Lexer(unittest.TestCase):
    def test_names_and_arguments_are_decoded_even_inside_wrappers(self) -> None:
        for command, name, args in NAMES + [(c, n, a) for c, n, a in ARGS]:
            with self.subTest(command=command):
                found = [(c.name, c.args) for c in simple_commands(command)]
                self.assertIn((name, args), found)

    def test_decoded_text_is_data_not_code(self) -> None:
        self.assertEqual([c.name for c in simple_commands(r"echo $'$(pkill x)'")], ["echo"])


RULES: dict[str, Any] = {
    "prog": policy.with_defaults({"match": {"program": K}, "message": "m"}),
    "ast": policy.with_defaults({"match": {"ast": {"pattern": f"{K} $$$"}}, "message": "m"}),
    "args": policy.with_defaults({"match": {"program": "rm", "args": "-rf"}, "message": "m"}),
}


class PlainMatching(Isolated):
    def test_program_and_args_rules_see_the_decoded_words(self) -> None:
        for command, rule in ((r"$'p\x6bill' x", "prog"), (r"sudo $'\160kill' x", "prog"),
                              (r"bash -c $'p\x6bill x'", "prog"), (r"rm $'-r\x66' x", "args")):
            with self.subTest(command=command):
                self.assertIsNotNone(matching.evaluate(command, {rule: RULES[rule]}).kinds[rule])
        self.assertIsNone(matching.evaluate(r"echo $'p\x6bill'", {"prog": RULES["prog"]}).kinds["prog"])


class AstMatching(AstIsolated):
    def test_the_tree_engine_normalises_ansi_c_names(self) -> None:
        for command in (c for c, _, _ in NAMES):
            with self.subTest(command=command):
                self.assertIsNotNone(matching.evaluate(command, {"ast": RULES["ast"]}).kinds["ast"])
        self.assertIsNone(matching.evaluate(r"echo $'p\x6bill' x", {"ast": RULES["ast"]}).kinds["ast"])

    def test_the_hook_denies_a_hidden_name(self) -> None:
        self.put(self.gpath, {"rules": {"ast": {"match": {"ast": {"pattern": f"{K} $$$"}}, "message": "No kill."}}})
        for n, command in enumerate((r"$'p\x6bill' x", r"sudo $'\160kill' x", r"echo ok; bash -c $'p\x6bill x'")):
            out = self.hook(command, session=f"h{n}")
            assert out is not None
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", command)
        self.assertIsNone(self.hook(r"echo $'p\x6bill'", session="h9"))

    def test_the_rule_ast_tree_shows_the_normalised_unit(self) -> None:
        out = self.cli("rule", "ast", r"$'p\x6bill' x")[1]
        self.assertIn("units: 2", out)
        self.assertIn("ansi_c_string", out)
        self.assertIn("word «pkill»", out)


class Degraded(Isolated):
    def test_the_name_scan_decodes_too(self) -> None:
        self.put(self.gpath, {"rules": {"ast": {"match": {"ast": {"pattern": f"{K} $$$"}}, "message": "No kill."}}})
        for n, command in enumerate((r"$'p\x6bill' x", r"sudo $'\160kill' x", r"$'p\u006bill' x", r"$'p\U0000006bill' x")):
            out = self.hook(command, session=f"d{n}")
            assert out is not None
            self.assertEqual(out["hookSpecificOutput"].get("permissionDecision"), "deny", command)


if __name__ == "__main__":
    unittest.main()
