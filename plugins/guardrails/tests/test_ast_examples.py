from __future__ import annotations  # noqa: I001

import json
import re
import unittest
from pathlib import Path
from typing import Any

from helpers import ROOT, AstIsolated

import engine
import matching
import policy

BLOCK = re.compile(r"^```rule-example\n(.*?)\n```$", re.MULTILINE | re.DOTALL)
REQUIRED = {"id", "title", "rule", "action", "catch", "pass"}
OPTIONAL = {"tree", "wrappers", "when", "message", "messages", "cases", "says", "matchers"}
NEW_SKILL = ROOT / "skills" / "new" / "SKILL.md"


def sources() -> list[Path]:
    return [*sorted((ROOT / "references" / "ast").glob("*.md")), ROOT / "references" / "writing-rules.md", NEW_SKILL]


def examples() -> list[tuple[Path, dict[str, Any]]]:
    return [(path, json.loads(text)) for path in sources() for text in BLOCK.findall(path.read_text())]


def rule_of(example: dict[str, Any]) -> policy.Rule:
    extra = {key: example[key] for key in ("when", "messages") if key in example}
    return policy.Rule.from_json({"match": example["rule"], "message": example.get("message", "m"),
                                  "action": example["action"], "wrappers": example.get("wrappers", True), **extra},
                                 example.get("matchers"))


class ExampleShape(unittest.TestCase):
    def test_blocks_are_well_formed_and_ids_unique(self) -> None:
        found = examples()
        self.assertGreaterEqual(len(found), 15)
        ids = [e["id"] for _, e in found]
        self.assertEqual(len(ids), len(set(ids)), "example ids must be unique")
        for path, e in found:
            with self.subTest(example=e.get("id"), file=path.name):
                self.assertEqual(REQUIRED - set(e), set())
                self.assertEqual(set(e) - REQUIRED - OPTIONAL, set())
                self.assertIn(e["action"], ("deny", "warn"))
                self.assertGreaterEqual(len(e["catch"]), 3)
                self.assertGreaterEqual(len(e["pass"]), 3)

    def test_new_embeds_exactly_three(self) -> None:
        self.assertEqual(len(BLOCK.findall(NEW_SKILL.read_text())), 3)

    def test_every_cookbook_file_has_examples(self) -> None:
        for path in sorted((ROOT / "references" / "ast").glob("*.md")):
            if path.name != "index.md":
                with self.subTest(file=path.name):
                    self.assertTrue(BLOCK.search(path.read_text()))


class ExamplesRun(AstIsolated):
    def kinds(self, rule: policy.Rule, commands: list[str]) -> dict[str, str | None]:
        return {c: matching.evaluate(c, {"r": rule}).kinds["r"] for c in commands}

    def test_catch_matches_and_pass_does_not(self) -> None:
        for path, e in examples():
            with self.subTest(example=e["id"], file=path.name):
                rule = rule_of(e)
                caught, passed = self.kinds(rule, e["catch"]), self.kinds(rule, e["pass"])
                self.assertEqual([c for c, k in caught.items() if not k], [], "must match")
                self.assertEqual([c for c, k in passed.items() if k], [], "must not match")
                self.assertIn("wrapped", caught.values(), "needs a wrapped catch command")

    def test_message_cases_and_rendered_texts_are_what_the_example_says(self) -> None:
        checked = 0
        for path, e in examples():
            rule = rule_of(e)
            for command, case in e.get("cases", {}).items():
                with self.subTest(example=e["id"], file=path.name, command=command):
                    detail = matching.evaluate(command, {"r": rule}).details.get("r")
                    self.assertEqual(detail.case + 1 if detail and detail.case is not None else None, case)
                    checked += 1
            for command, text in e.get("says", {}).items():
                with self.subTest(example=e["id"], file=path.name, command=command):
                    ev = matching.evaluate(command, {"r": rule})
                    self.assertIsNotNone(ev.kinds["r"])
                    self.assertEqual(engine.texts_of(rule, ev.details.get("r")).full, text)
                    checked += 1
        self.assertGreaterEqual(checked, 6)
        self.assertTrue(any("when" in e for _, e in examples()))

    def test_tree_names_kinds_the_engine_prints(self) -> None:
        for path, e in examples():
            if "tree" not in e:
                continue
            with self.subTest(example=e["id"], file=path.name):
                shown = {kind for unit in matching.tree(e["catch"][0])[0] for _, kind, _ in unit.rows}
                self.assertEqual(set(re.findall(r"[a-z_]+", e["tree"])) - shown, set())

    def test_negated_relations_are_documented_as_working(self) -> None:
        text = "\n".join(path.read_text() for path in sources())
        for needle in ('"not": {"inside"', '"not": {\n      "follows"'):
            self.assertIn(needle, text)
        self.assertNotIn("Negated context does not work", text)


if __name__ == "__main__":
    unittest.main()
