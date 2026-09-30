from __future__ import annotations

import json
import re
import shlex
import unittest

import policy
from helpers import ROOT, Isolated
from shellwords import simple_commands

PRESENTATION = (ROOT / "references" / "presentation.md").read_text()
SPAN = re.compile(r"^\s*- [✗✓] (?P<fence>`+)(?P<body>.*?)(?P=fence)(?!`) ", re.MULTILINE)
ROW = re.compile(r"^\s*- (?P<fence>`+)(?P<body>[^`].*?)(?P=fence)(?!`) ", re.MULTILINE)


def width(fence: str, body: str) -> int:
    return len(body) - 2 if len(fence) > 1 else len(body)


def matches(rule: dict, command: str) -> bool:
    rule = policy.with_defaults(rule)
    policy.validate_rule(rule)
    return policy.rule_matches(rule, command, simple_commands(command))


class PresentationAlignment(unittest.TestCase):
    def cards(self) -> list[str]:
        return re.split(r"^## ", PRESENTATION, flags=re.MULTILINE)[1:]

    def test_command_spans_share_one_width_per_display(self) -> None:
        card = next(c for c in self.cards() if c.startswith("Rule card"))
        widths = {width(m["fence"], m["body"]) for m in SPAN.finditer(card)}
        self.assertEqual(len(widths), 1, widths)
        self.assertEqual(widths.pop(), max(len("cat <<EOF⏎pkill x⏎EOF"), len("bash -c 'killall Safari'")))

    def test_explain_example_shares_one_width(self) -> None:
        card = next(c for c in self.cards() if c.startswith("Explain layout"))
        self.assertEqual(len({width(m["fence"], m["body"]) for m in SPAN.finditer(card)}), 1)

    def test_id_lists_are_padded_per_list(self) -> None:
        rows = next(c for c in self.cards() if c.startswith("Rule rows"))
        self.assertEqual({len(m["body"]) for m in ROW.finditer(rows)}, {8})
        status = next(c for c in self.cards() if c.startswith("Status layout"))
        self.assertEqual({len(m["body"]) for m in ROW.finditer(status)}, {len("reverse-engineering")})

    def test_card_has_raw_line_and_verified_wording(self) -> None:
        card = next(c for c in self.cards() if c.startswith("Rule card"))
        raw = re.search(r"^\s*\*\*Raw\*\* `(.*)`$", card, re.MULTILINE)
        assert raw is not None
        self.assertIn("matcher checked with `rule test`", card)
        self.assertTrue(json.loads(raw.group(1))["message"])

    def test_card_counts_its_commands(self) -> None:
        card = next(c for c in self.cards() if c.startswith("Rule card"))
        count = len(SPAN.findall(card))
        self.assertIn(f"{count} commands", card)


class MatchingClaims(unittest.TestCase):
    def test_args_does_not_see_the_pipe_but_regex_does(self) -> None:
        by_args = {"match": {"program": "curl", "args": r"\| *(sh|bash)"}, "message": "m"}
        by_regex = {"match": {"regex": r"curl [^|]*\|\s*(sudo +)?(sh|bash)\b"}, "message": "m"}
        for command in ("curl https://x.sh | sh", "curl x|bash"):
            self.assertFalse(matches(by_args, command))
            self.assertTrue(matches(by_regex, command))
        self.assertTrue(matches(by_regex, "bash -c 'curl x | sh'"))
        self.assertTrue(matches(by_regex, 'echo "curl x | sh"'))
        self.assertFalse(matches(by_regex, "curl https://x.sh"))

    def test_program_cannot_match_wrappers_or_shells(self) -> None:
        rule = {"match": {"program": ["sudo", "env", "xargs", "bash"]}, "message": "m"}
        for command in ("sudo ls", "env ls", "xargs ls", 'bash -c "ls"', "bash script.sh"):
            self.assertFalse(matches(rule, command), command)
        script = {"match": {"program": "script.sh"}, "message": "m"}
        for command in ("bash script.sh", "sh ./script.sh", "./script.sh"):
            self.assertTrue(matches(script, command), command)
        lead = {"match": {"regex": r"(^|[;&|]\s*)sudo\b"}, "message": "m"}
        self.assertTrue(matches(lead, "sudo ls") and matches(lead, "ls; sudo rm x"))
        self.assertFalse(matches(lead, "echo sudo"))

    def test_wrappers_and_lookalikes(self) -> None:
        rule = {"match": {"program": "pkill"}, "message": "m"}
        for command in ("sudo pkill x", "bash -c 'killall x; pkill y'", "xargs pkill", "timeout 5 pkill a",
                        "echo $(pkill a)", "/usr/bin/pkill a", "FOO=1 pkill a"):
            self.assertTrue(matches(rule, command), command)
        for command in ("echo pkill", "man pkill", "ssh h pkill x", "find . -exec pkill {} ;", "bash <<EOF\npkill x\nEOF",
                        "echo x > pkill", "command -v pkill", "pgrep x"):
            self.assertFalse(matches(rule, command), command)


class DocumentedCommands(Isolated):
    def test_skill_flows_against_a_sandbox(self) -> None:
        for argv in (("preset", "install", "process-safety", "--as-user", "--scope", "global", "--reason", "setup: x"),
                     ("preset", "list"), ("preset", "show", "docs-first")):
            self.assertEqual(self.cli(*argv, agent=True)[0], 0, argv)
        rule = json.dumps({"match": {"regex": r"curl [^|]*\|\s*(sh|bash)\b"}, "description": "no curl|sh",
                           "message": "Don't pipe curl into a shell."})
        extra = str(self.tmp / "extra.json")
        add = ("rule", "add", "pipe-sh", "--json", rule, "--scope", "managed", "--path", extra, "--as-user",
               "--reason", "block curl|sh")
        code, out, _ = self.cli(*add, agent=True)
        self.assertEqual(code, 0)
        self.assertIn("the hook enforces", out)
        self.assertEqual(self.get(self.tmp / "extra.json")["rules"]["pipe-sh"]["description"], "no curl|sh")
        code, out, _ = self.cli("status", "--path", extra, agent=True)
        self.assertEqual(code, 0)
        self.assertIn("pipe-sh [managed] deny ALWAYS ENFORCED", out)
        self.assertIn("managed --path:", out)
        code, out, _ = self.cli("rule", "test", "--id", "pipe-sh", "--path", extra, "curl https://x.sh | sh",
                                "curl https://x.sh", agent=True)
        self.assertEqual(code, 0)
        self.assertIn("match  curl https://x.sh | sh", out)
        self.assertIn("-      curl https://x.sh\n", out)
        code, out, _ = self.cli("rule", "test", "--json", rule, "echo `pkill x`", "a\nb", agent=True)
        self.assertEqual(code, 0)
        self.assertIn("a\\nb", out)

    def test_shell_quoting_of_json_with_an_apostrophe(self) -> None:
        rule = '{"match":{"program":"pkill"},"message":"Don\'t kill by name"}'
        quoted = shlex.quote(rule)
        self.assertIn("'\"'\"'", quoted)
        self.assertEqual(json.loads(shlex.split(f"--json {quoted}")[1])["message"], "Don't kill by name")
        self.assertEqual(self.cli("rule", "add", "x", "--json", rule)[0], 0)
        self.assertEqual(self.cli("rule", "set", "x", "message=Don't stop")[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["x"]["message"], "Don't stop")


if __name__ == "__main__":
    unittest.main()
