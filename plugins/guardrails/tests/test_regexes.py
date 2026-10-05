from __future__ import annotations  # noqa: I001

from typing import Any

from helpers import AstIsolated

import json

import policy

BAD = "("
LOOK_BEHIND = "(?<=a)b"


def load(match: dict[str, Any], **extra: Any) -> policy.Rule:
    return policy.Rule.from_json({"match": match, "message": "m", **extra})


class InvalidRegexes(AstIsolated):
    def refused(self, match: dict[str, Any], path: str, **extra: Any) -> None:
        with self.assertRaises(policy.Invalid) as raised:
            load(match, **extra)
        self.assertIn(f"'{path}' is not a valid regex", str(raised.exception))

    def test_every_position_a_regex_can_be_written_at_is_checked(self) -> None:
        for match, path in (
            ({"kind": "program", "regex": BAD}, "match.regex"),
            ({"kind": "command", "has": {"regex": BAD}}, "match.has.regex"),
            ({"command": "x", "regex": BAD}, "match.regex"),
            ({"command": "x", "args": BAD}, "match.args"),
            ({"any": [{"command": "x"}, {"kind": "word", "regex": BAD}]}, "match.any[1].regex"),
            ({"command": "x", "not": {"regex": BAD}}, "match.not.regex"),
            ({"command": "x", "inside": {"kind": "list", "stopBy": {"regex": BAD}}}, "match.inside.stopBy.regex"),
            ({"assignment": {"name": {"regex": BAD}}}, "match.assignment.name.regex"),
            ({"assignment": {"value": {"regex": BAD}}}, "match.assignment.value.regex"),
            ({"statement": {"command": "x"}, "has": {"redirect": {"to": {"regex": BAD}}}},
             "match.has.redirect.to.regex"),
            ({"command": "x", "follows": {"command": "y", "args": BAD}}, "match.follows.args"),
            ({"capture": {"kind": "word", "regex": BAD}, "name": "X"}, "match.capture.regex"),
        ):
            with self.subTest(match=match):
                self.refused(match, path)

    def test_a_regex_in_a_message_case_matches_is_checked(self) -> None:
        raw = {"match": {"command": "x"}, "message": "m",
               "messages": [{"when": {"matches": {"has": {"regex": BAD}}}, "text": "t"}]}
        with self.assertRaises(policy.Invalid) as raised:
            policy.Rule.from_json(raw)
        self.assertIn("messages[0].when.matches", str(raised.exception))
        self.assertIn("match.has.regex", str(raised.exception))

    def test_the_rust_dialect_is_the_judge_not_python(self) -> None:
        for text in (LOOK_BEHIND, "a(?=b)", "(a)\\1", "["):
            with self.subTest(regex=text):
                self.refused({"kind": "word", "regex": text}, "match.regex")
        for text in ("^a+$", "(?i)x", "a{2,3}", "\\bkill\\b", "^(\\S*/)?(echo|ls)$"):
            with self.subTest(regex=text):
                load({"kind": "word", "regex": text})

    def test_rule_add_and_rule_test_refuse_it_and_nothing_is_stored(self) -> None:
        bad = json.dumps({"match": {"command": "x", "args": BAD}, "message": "m"})
        for argv in (("rule", "add", "r", "--json", bad), ("rule", "test", "--json", bad, "x")):
            with self.subTest(argv=argv[:2]):
                code, out, err = self.cli(*argv)
                self.assertEqual(code, 2, out + err)
                self.assertIn("'match.args' is not a valid regex", out + err)
        self.assertFalse(self.gpath.exists())

    def test_the_error_says_why_and_names_the_dialect(self) -> None:
        with self.assertRaises(policy.Invalid) as raised:
            load({"kind": "word", "regex": BAD})
        self.assertIn("unclosed group", str(raised.exception))
        self.assertIn("Rust regex", str(raised.exception))
