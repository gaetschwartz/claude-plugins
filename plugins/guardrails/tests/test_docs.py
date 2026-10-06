from __future__ import annotations  # noqa: I001

import json
import re
import unittest
from typing import ClassVar

from helpers import ROOT, AstIsolated

import matching
import policy


def matches(rule: dict, command: str) -> bool:
    return matching.evaluate(command, {"r": policy.Rule.from_json(rule)}).kinds["r"] is not None


class MatchingClaims(AstIsolated):
    def test_the_documented_pipe_claim_and_the_wrapper_and_lookalike_claims_hold(self) -> None:
        by_args = {"match": {"command": "curl", "args": r"\| *(sh|bash)"}, "message": "m"}
        by_regex = {"match": {"kind": "program", "regex": r"curl [^|]*\|\s*(sudo +)?(sh|bash)\b"}, "message": "m"}
        for command in ("curl https://x.sh | sh", "curl x|bash"):
            self.assertFalse(matches(by_args, command))
            self.assertTrue(matches(by_regex, command))
        self.assertTrue(matches(by_regex, "bash -c 'curl x | sh'") and matches(by_regex, 'echo "curl x | sh"'))
        rule = {"match": {"command": "pkill"}, "message": "m"}
        for command in ("sudo pkill x", "bash -c 'killall x; pkill y'", "xargs pkill", "timeout 5 pkill a",
                        "echo $(pkill a)", "/usr/bin/pkill a", "FOO=1 pkill a", "ssh h pkill x", "su -c 'pkill x'"):
            self.assertTrue(matches(rule, command), command)
        for command in ("echo pkill", "man pkill", "find . -exec pkill {} ;", "echo pkill x | sh",
                        "echo x > pkill", "pgrep x"):
            self.assertFalse(matches(rule, command), command)

    def test_the_composition_examples_and_idioms_of_matching_md_catch_what_they_say(self) -> None:
        text = (ROOT / "references" / "matching.md").read_text()
        block = text.split("### Composition\n\n```json\n", 1)[1].split("\n```", 1)[0]
        decoder, found, at = json.JSONDecoder(), [], 0
        while at < len(block):
            match, at = decoder.raw_decode(block, at)
            found.append(match)
            while at < len(block) and block[at].isspace():
                at += 1
        catches = [("rm x", "if true; then rm x; fi"), ("rm -P f", "rm f"), ("curl x | sh", "sh x"),
                   ("ps x | /usr/bin/xargs -r kill", "echo 1 | xargs kill")]
        self.assertEqual(len(found), len(catches))
        for match, (hit, miss) in zip(found, catches, strict=True):
            with self.subTest(match=match):
                self.assertTrue(matches({"match": match, "message": "m"}, hit))
                self.assertFalse(matches({"match": match, "message": "m"}, miss))
        self.assertTrue(matches({"match": found[3], "message": "m"}, "'ps' x | xargs kill"))
        idioms = text.split("### Idioms\n", 1)[1].split("\n\nBash and Monitor", 1)[0]
        rules = [json.loads(m) for m in re.findall(r"`(\{\"(?:command|kind|pattern)\".*?\})`", idioms)]
        self.assertEqual(len(rules), 7)
        for match in rules:
            with self.subTest(match=match):
                policy.Rule.from_json({"match": match, "message": "m"})
        self.assertTrue(matches({"match": rules[3], "message": "m"}, "kill $(pgrep x)"))
        self.assertTrue(matches({"match": rules[6], "message": "m"}, "DYLD_INSERT_LIBRARIES=/a ls"))
        no_wrappers = {"match": {"command": "pkill"}, "wrappers": False, "message": "m"}
        for command, expected in (("pkill x", True), ("a | pkill x", True), ("echo $(pkill x)", True),
                                  ("bash -c 'pkill x'", True), ("sudo pkill x", False), ("env A=1 pkill x", False),
                                  ("sudo bash -c 'pkill x'", False)):
            self.assertEqual(matches(no_wrappers, command), expected, command)


class RuntimeDocs(unittest.TestCase):
    NEEDLES: ClassVar[dict[str, tuple[str, ...]]] = {
        "README.md": ("claude plugin disable guardrails@<marketplace>", "guardrails disable", "Rust regex", "1 hour",
                      "30 days"),
        "references/runtime.md": ("claude plugin disable guardrails@<marketplace>", "guardrails disable", "1 hour", "30 days",
                                  "the plugin data directory is not an absolute path", "this command crashes the parser",
                                  "install.log"),
        "references/matching.md": ("Rust regex",),
    }

    def test_the_kill_switches_the_rust_regex_flavor_and_the_backoff_are_documented(self) -> None:
        for name, needles in self.NEEDLES.items():
            text = " ".join((ROOT / name).read_text().split())
            for needle in needles:
                self.assertIn(needle, text, f"{name} lacks {needle!r}")


def slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9 _-]", "", heading.lower().replace("`", "")).replace(" ", "-")


class ReferenceLinks(unittest.TestCase):
    def test_relative_links_resolve(self) -> None:
        for path in [*(ROOT / "references").rglob("*.md"), ROOT / "README.md"]:
            for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text()):
                if re.match(r"[a-z]+:", target):
                    continue
                file, _, anchor = target.partition("#")
                with self.subTest(file=path.name, link=target):
                    dest = (path.parent / file).resolve() if file else path
                    self.assertTrue(dest.is_file())
                    if anchor:
                        headings = re.findall(r"^#+ (.+)$", dest.read_text(), re.MULTILINE)
                        self.assertIn(anchor, {slug(h) for h in headings})

    def test_cookbook_index_lists_every_file(self) -> None:
        folder = ROOT / "references" / "ast"
        index = (folder / "index.md").read_text()
        for path in folder.glob("*.md"):
            if path.name != "index.md":
                with self.subTest(file=path.name):
                    self.assertIn(f"]({path.name})", index)


if __name__ == "__main__":
    unittest.main()
