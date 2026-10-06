from __future__ import annotations

import json
import re
import unicodedata
import unittest

from helpers import GREP_RECURSIVE, ROOT, AstIsolated, render

PRESENTATION = (ROOT / "references" / "presentation.md").read_text()
SPAN = re.compile(r"^- [✗✓] (?:⚠ )?(?P<fence>`+)(?P<body>.*?)(?P=fence)(?!`) ", re.MULTILINE)

PKILL = {"match": {"command": ["pkill", "killall"]}, "retry": "same-command",
         "message": "Killing by name can hit the wrong process. Find the PID with pgrep -fl, then kill it by PID."}
GOLDEN_EXAMPLES = [
    {"cmd": "pkill node", "source": "yours", "expect": "match"},
    {"cmd": "sudo pkill -f vite", "expect": "match"},
    {"cmd": "bash -c 'killall Safari'", "expect": "match"},
    {"cmd": "killall Safari", "expect": "match"},
    {"cmd": "pkill -0 node", "source": "you chose", "expect": "match"},
    {"cmd": "echo `pkill x`", "expect": "match"},
    {"cmd": "kill 4242", "source": "yours", "expect": "pass"},
    {"cmd": "pgrep -fl node", "expect": "pass"},
    {"cmd": "man pkill", "expect": "pass"},
    {"cmd": 'echo "pkill node"', "expect": "pass"},
    {"cmd": "cat <<EOF\npkill x\nEOF", "expect": "pass"},
    {"cmd": "pgrep -fl node | xargs -n 1 echo running:", "expect": "pass"},
]
EVIL = "x\x1b[2J\ty\u2028z\x85w\u202ev\u200bu\U000e0041"


def doc_block(first_line: str) -> str:
    lines = PRESENTATION.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("    " + first_line))
    out = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        out.append(line[4:].rstrip())
    return "\n".join(out).rstrip()


def span_width(match: re.Match[str]) -> int:
    body = match["body"]
    if len(match["fence"]) > 1 or body.startswith(" "):
        body = body[1:-1]
    return render.display_width(body)


def control_chars(text: str) -> list[str]:
    return [c for c in text if c != "\n" and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")]


class Spans(unittest.TestCase):
    def test_padding_fences_widths_and_neutralised_text(self) -> None:
        for args, expected in ((("ab", 5), "`ab   `"), (("abcdef", 3), "`abcdef`"), (("a`b", 6), "`` a`b    ``"),
                               (("a``b", 4), "``` a``b ```"), ((" x", 4), "`  x   `")):
            self.assertEqual(render.span(*args), expected)
        for text, width in (("abc", 3), ("日本", 4), ("😀", 2), ("é", 1)):
            self.assertEqual(render.display_width(text), width)
        self.assertEqual(render.clean("a\nb\r\nc"), "a⏎b⏎c")
        self.assertEqual([render.common_width(["a" * 10, "b"]), render.common_width(["a" * 90, "b"]), render.common_width([])],
                         [10, 40, 0])
        cleaned = render.clean(EVIL + "\x00\x7f")
        self.assertEqual(control_chars(cleaned), [])
        for shown in ("␛", "⇥", "\\u{2028}", "\\u{85}", "\\u{202e}", "\\u{200b}", "\\u{e0041}", "\\u{0}", "␡"):
            self.assertIn(shown, cleaned)
        self.assertEqual(render.clean(render.clean(EVIL)), render.clean(EVIL))


class Card(AstIsolated):
    def card(self, rule: dict, examples: list, *extra: str) -> str:
        code, out, err = self.cli("rule", "test", "--json", json.dumps(rule), "--examples", json.dumps(examples), *extra)
        self.assertEqual((code, err), (0, ""))
        return out

    def rows(self, out: str) -> list[str]:
        return [line for line in out.splitlines() if re.match(r"- [✗✓]", line)]

    def test_the_card_and_the_status_listing_match_the_documented_examples(self) -> None:
        out = self.card(PKILL, GOLDEN_EXAMPLES, "--intent", "stop killing processes by name, suggest kill by PID",
                        "--id-name", "no-pkill")
        self.assertEqual(out.rstrip("\n"), doc_block("### no-pkill"))
        rules = [render.RuleRow("find-fd", "warn", ["global"], "enabled", 'when `{"bin":["fd","fdfind"]}`'),
                 render.RuleRow("grep-rg", "warn", ["global"], "inactive here: its when does not hold",
                                'when `{"bin":"rg"}`'),
                 render.RuleRow("kill-9", "warn", ["managed"], "always enforced"),
                 render.RuleRow("no-pkill", "deny", ["global", "project"], "enabled"),
                 render.RuleRow("no-strings", "deny", ["global"], "suspended by reverse-engineering"),
                 render.RuleRow("old-rule", "deny", ["global"], "disabled")]
        modes = [render.ModeRow("incident", "off", False, ["global"]),
                 render.ModeRow("reverse-engineering", "on (by agent: user said RE work)", True, ["global"])]
        files = ["**Managed** platform file `/Library/Application Support/ClaudeCode/guardrails.json` present",
                 "**Global** config `/Users/me/.config/dev.gaetans.guardrails/claude-plugin/config.json` present",
                 "**Project** config `/Users/me/src/app/.claude/guardrails.json` absent"]
        text = render.status_listing(render.Status(files, True, False, rules, modes, ["text as reported"]))
        self.assertEqual(text, doc_block("**Managed** platform file"))
        self.assertNotIn("```\n", out)

    def test_spans_share_one_width_capped_at_forty_and_longer_commands_go_last_unpadded(self) -> None:
        out = self.card(PKILL, [{"cmd": "pkill a"}, {"cmd": "sudo pkill -f vite"}, {"cmd": "ls"}, {"cmd": "man pkill"}])
        self.assertEqual({span_width(m) for m in SPAN.finditer(out)}, {len("sudo pkill -f vite")})
        self.assertIn("- ✗ `pkill a           ` inferred\n", out)
        self.assertIn("- ✓ `ls                ` inferred\n", out)
        long, exact = "pkill " + "x" * 50, "pkill " + "x" * 34
        block = self.rows(self.card(PKILL, [{"cmd": long}, {"cmd": "pkill a"}, {"cmd": "pkill " + "y" * 45}, {"cmd": "pkill b"}]))
        self.assertEqual([r.split("`")[1].rstrip() for r in block], ["pkill a", "pkill b", long, "pkill " + "y" * 45])
        self.assertEqual((block[0], block[2]), ("- ✗ `pkill a" + " " * 33 + "` inferred", f"- ✗ `{long}` inferred"))
        self.assertEqual([r.split("`")[1].rstrip() for r in self.rows(self.card(PKILL, [{"cmd": exact}, {"cmd": "pkill a"}]))],
                         [exact, "pkill a"])

    def test_controls_in_commands_neither_break_the_block_nor_the_alignment(self) -> None:
        forged = "x\n### Forged\n- ✗ `fake` yours"
        rule = {**PKILL, "id": forged, "message": forged}
        out = self.card(rule, [{"cmd": forged}, {"cmd": "pkill " + EVIL}, "ls"], "--intent", forged)
        self.assertEqual((control_chars(out), "\x1b" in out), ([], False))
        for line in out.splitlines():
            self.assertFalse(line.startswith(("### Forged", "- ✗ `fake")), line)
        self.assertEqual(sum(line.startswith("### ") for line in out.splitlines()), 1)
        out = self.card(PKILL, [{"cmd": "pkill\ta"}, {"cmd": "pkill \x1bb"}, {"cmd": "pkill abcdefghi"}])
        self.assertEqual({span_width(m) for m in SPAN.finditer(out)}, {len("pkill abcdefghi")})

    def test_headings_titles_groups_and_counts(self) -> None:
        deny = self.card(PKILL, [{"cmd": "pkill a"}, {"cmd": "ls"}])
        self.assertIn("**Block**\n- ✗", deny)
        self.assertNotIn("**Warn**", deny)
        warn = self.card({**PKILL, "action": "warn"}, [{"cmd": "pkill a"}, {"cmd": "ls"}])
        self.assertIn("### new-rule · warn · retry same-command · global", warn)
        self.assertIn("**Warn**\n- ✗", warn)
        self.assertIn("**Allow**\n- ✓", warn)
        self.assertNotIn("**Block**", warn)
        self.assertNotIn("**Allow**", self.card(PKILL, [{"cmd": "pkill a"}]))
        self.assertNotIn("**Block**", self.card(PKILL, [{"cmd": "ls"}]))
        self.assertTrue(self.card({"match": {"command": "x"}, "message": "m"}, [{"cmd": "x"}]).startswith(
            "### new-rule · deny · global\n"))
        rule = {**PKILL, "id": "from-rule"}
        self.assertTrue(self.card(rule, [{"cmd": "ls"}]).startswith("### from-rule ·"))
        self.assertTrue(self.card(rule, [{"cmd": "ls"}], "--id-name", "named").startswith("### named ·"))
        self.assertTrue(self.card(PKILL, [{"cmd": "ls"}], "--scope", "project").startswith(
            "### new-rule · deny · retry same-command · project\n"))

    def test_mismatches_are_flagged_and_counted_and_no_expectation_is_no_mismatch(self) -> None:
        out = self.card(PKILL, [{"cmd": "pkill a", "expect": "pass"}, {"cmd": "ls", "expect": "match"},
                                {"cmd": "pkill b", "expect": "match"}, {"cmd": "ls", "expect": "pass"},
                                {"cmd": "pkill c"}, {"cmd": "ls"}])
        flagged = [r for r in self.rows(out) if "⚠" in r]
        self.assertEqual(len(flagged), 2)
        self.assertTrue(flagged[0].startswith("- ✗ ⚠ `pkill a") and flagged[1].startswith("- ✓ ⚠ `ls"))
        self.assertIn("**Verified** matcher checked with `rule test`, 6 commands, 2 mismatches\n", out)
        out = self.card(PKILL, [{"cmd": "pkill a"}, {"cmd": "ls"}])
        self.assertNotIn("⚠", out)
        self.assertIn("2 commands, 0 mismatches", out)
        self.assertIn("1 command, 1 mismatch\n", self.card(PKILL, [{"cmd": "pkill a", "expect": "pass"}]))

    def test_match_raw_intent_message_and_note_lines(self) -> None:
        regex = r"curl [^|]*\|\s*(sh|bash)`"
        out = self.card({"match": {"kind": "program", "regex": regex}, "message": "m"}, [{"cmd": "ls"}])
        self.assertIn(f"\n**Raw** `` {regex} ``", out + "\n")
        for match, raw in (({"command": "x", "args": "-f"}, "-f"), ({"command": "x", "args": "-f", "regex": "zz"}, "-f | zz"),
                           ({"command": "x", "has": {"pattern": "y $$$"}, "regex": "zz"}, "y $$$")):
            self.assertTrue(self.card({"match": match, "message": "m"}, [{"cmd": "x"}]).rstrip("\n").endswith(f"**Raw** `{raw}`"))
        self.assertNotIn("**Raw**", self.card(PKILL, [{"cmd": "ls"}]))
        rule = {"match": {"command": "grep", "args": "-r", "regex": "zz"}, "message": "m"}
        self.assertIn('**Match** `{"command":"grep","args":"-r","regex":"zz"}`\n', self.card(rule, [{"cmd": "ls"}]))
        self.assertIn('**Match** `{"command":"grep"}` · not through wrappers\n',
                      self.card({"match": {"command": "grep"}, "wrappers": False, "message": "m"}, [{"cmd": "ls"}]))
        self.assertNotIn("**Intent**", self.card(PKILL, [{"cmd": "ls"}]))
        self.assertIn("\n**Intent** stop it\n", self.card(PKILL, [{"cmd": "ls"}], "--intent", "stop it"))
        placeholder = {"match": {"pattern": "x $ARG"}, "message": "use {ARG} not {{x}}"}
        self.assertIn("**Message** use {ARG} not {x}\n", self.card(placeholder, [{"cmd": "x"}]))
        out = self.card({**PKILL, "enabled": False, "when": {"bin": "no-such-bin-xyz"}}, [{"cmd": "pkill a"}])
        notes = [line for line in out.splitlines() if line.startswith("**Note**")]
        self.assertEqual(len(notes), 1)
        self.assertIn("rule is disabled; its when does not hold here (for a Bash call), so the hook skips this rule",
                      notes[0])
        self.assertIn('\n**When** `{"bin":"no-such-bin-xyz"}` · does not hold here\n', out)
        self.assertNotIn("**Note**", self.card(PKILL, [{"cmd": "pkill a"}]))

    def test_whole_text_regex_matches_are_direct_and_unbalanced_quotes_fall_back_to_direct(self) -> None:
        rule = {"match": {"kind": "program", "regex": r"curl [^|]*\| *sh"}, "message": "m"}
        self.assertNotIn("wrapped", self.card(rule, [{"cmd": "sudo curl x | sh"}, {"cmd": "echo hi"}]))
        both = {"match": {"any": [{"command": "pkill"}, {"kind": "program", "regex": "pkill"}]}, "message": "m"}
        self.assertNotIn("wrapped", self.card(both, [{"cmd": "sudo pkill x"}]))
        self.assertNotIn("wrapped", self.card(PKILL, [{"cmd": "pkill 'x"}]))


class WrappedForms(AstIsolated):
    def test_card_verdicts_agree_with_the_hook(self) -> None:
        import engine
        import policy

        rules = [{"match": {"command": "pkill"}}, {"match": {"kind": "program", "regex": r"curl [^|]*\| *sh"}},
                 {"match": {"command": "grep", "args": "-r"}}, {"match": GREP_RECURSIVE},
                 {"match": {"any": [{"command": "pkill"}, {"kind": "program", "regex": "zz"}]}},
                 {"match": {"command": "pkill"}, "wrappers": False}]
        commands = ["pkill x", "sudo pkill x", "echo foo$(pkill x)", "cat <(pkill x)", "a | pkill x", "curl x | sh",
                    "bash -c 'curl y | sh'", "grep -r x", "grep x", "grep -R y .", "echo pkill", "man pkill",
                    "pkill 'x", "cat <<EOF\npkill x\nEOF", "xargs pkill", "zz", "ls"]
        for raw in rules:
            rule = policy.Rule.from_json({**raw, "message": "m"})
            out = self.cli("rule", "test", "--json", json.dumps({**raw, "message": "m"}), *commands)[1]
            card = {}
            for row in out.splitlines():
                if matched := SPAN.match(row):
                    body = matched["body"]
                    card[(body[1:-1] if len(matched["fence"]) > 1 else body).rstrip()] = row.startswith("- ✗")
            for command in commands:
                with self.subTest(rule=raw, command=command):
                    output, _ = engine.evaluate(command, {"r": rule}, {}, policy.Session(), "s")
                    self.assertEqual(card[render.clean(command)], output is not None and "permissionDecision" in output["hookSpecificOutput"])


class NotEvaluated(AstIsolated):
    def test_a_rule_the_engine_cannot_judge_is_never_shown_as_allowed(self) -> None:
        import verdict

        examples = json.dumps([{"cmd": "pkill a", "expect": "match"}, {"cmd": "ls", "expect": "pass"}])
        regex = json.dumps({"match": {"kind": "program", "regex": "^pkill"}, "message": "m"})
        self.assertIn("**Block**", self.cli("rule", "test", "--json", regex, "pkill a", "ls")[1])
        self.break_engine()
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(PKILL), "--examples", examples)
        self.assertEqual(code, 0)
        self.assertIn("**Not evaluated**", out)
        self.assertIn("- ? ⚠ `pkill a", out)
        self.assertNotIn("**Allow**", out)
        self.assertIn("**Verified** NOT verified: 2 of 2 commands could not be evaluated", out)
        self.assertNotIn("matcher checked", out)
        self.assertIn("cannot evaluate this rule's match", out)
        self.assertIn("Not evaluated", self.cli("rule", "test", "--json", regex, "pkill a", "ls")[1])
        out = self.cli("rule", "test", "--json", json.dumps(PKILL), "x" * (verdict.MAX_COMMAND_BYTES + 1))[1]
        self.assertIn("cannot evaluate this rule's match: command too large to check", out)


if __name__ == "__main__":
    unittest.main()
