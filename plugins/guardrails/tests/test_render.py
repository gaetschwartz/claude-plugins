from __future__ import annotations

import io
import json
import os
import re
import unittest
from unittest import mock

from helpers import GREP_RECURSIVE, ROOT, AstIsolated, render

PRESENTATION = (ROOT / "references" / "presentation.md").read_text()
SPAN = re.compile(r"^- [✗✓] (?:⚠ )?(?P<fence>`+)(?P<body>.*?)(?P=fence)(?!`) ", re.MULTILINE)

PKILL = {"match": {"program": ["pkill", "killall"]}, "retry": "same-command",
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


def doc_block(first_line: str) -> str:
    lines = PRESENTATION.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("    " + first_line))
    out = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        out.append(line[4:].rstrip())
    return "\n".join(out).rstrip()


def command_of(row: str) -> str:
    match = SPAN.match(row)
    assert match is not None
    body = match["body"]
    return (body[1:-1] if len(match["fence"]) > 1 else body).rstrip()


def span_width(match: re.Match[str]) -> int:
    body = match["body"]
    if len(match["fence"]) > 1 or body.startswith(" "):
        body = body[1:-1]
    return render.display_width(body)


class Spans(unittest.TestCase):
    def test_plain_padding(self) -> None:
        self.assertEqual(render.span("ab", 5), "`ab   `")
        self.assertEqual(render.span("abcdef", 3), "`abcdef`")

    def test_backtick_uses_longer_fence_and_counts_width(self) -> None:
        self.assertEqual(render.span("a`b", 6), "`` a`b    ``")
        self.assertEqual(render.span("a``b", 4), "``` a``b ```")

    def test_leading_space_is_protected(self) -> None:
        self.assertEqual(render.span(" x", 4), "`  x   `")

    def test_display_width(self) -> None:
        self.assertEqual(render.display_width("abc"), 3)
        self.assertEqual(render.display_width("日本"), 4)
        self.assertEqual(render.display_width("😀"), 2)
        self.assertEqual(render.display_width("é"), 1)

    def test_newlines(self) -> None:
        self.assertEqual(render.clean("a\nb\r\nc"), "a⏎b⏎c")

    def test_common_width_is_capped(self) -> None:
        self.assertEqual(render.common_width(["a" * 10, "b"]), 10)
        self.assertEqual(render.common_width(["a" * 90, "b"]), 40)
        self.assertEqual(render.common_width([]), 0)


class Card(AstIsolated):
    def card(self, rule: dict, examples: list, *extra: str) -> str:
        code, out, err = self.cli("rule", "test", "--json", json.dumps(rule), "--examples",
                                  json.dumps(examples), *extra)
        self.assertEqual((code, err), (0, ""))
        return out

    def rows(self, out: str) -> list[str]:
        return [line for line in out.splitlines() if re.match(r"- [✗✓]", line)]

    def test_golden_matches_the_documented_example(self) -> None:
        out = self.card(PKILL, GOLDEN_EXAMPLES, "--intent", "stop killing processes by name, suggest kill by PID",
                        "--id-name", "no-pkill")
        self.assertEqual(out.rstrip("\n"), doc_block("### no-pkill"))

    def test_status_example_matches_the_renderer(self) -> None:
        rules = [render.RuleRow("kill-9", "warn", ["managed"], "always enforced"),
                 render.RuleRow("no-pkill", "deny", ["global", "project"], "enabled"),
                 render.RuleRow("no-strings", "deny", ["global"], "suspended by reverse-engineering"),
                 render.RuleRow("old-rule", "deny", ["global"], "disabled")]
        modes = [render.ModeRow("incident", "off", False, ["global"]),
                 render.ModeRow("reverse-engineering", "on (by agent: user said RE work)", True, ["global"])]
        managed = ("**Managed** platform file `/Library/Application Support/ClaudeCode/guardrails.json` absent · "
                   "override `/tmp/g.json` present · managed rules come only from `/tmp/g.json`")
        text = render.status_listing(render.Status(managed, True, False, rules, modes, ["text as reported"]))
        self.assertEqual(text, doc_block("**Managed** platform file"))

    def test_spans_share_one_width_and_tags_line_up(self) -> None:
        out = self.card(PKILL, [{"cmd": "pkill a"}, {"cmd": "sudo pkill -f vite"}, {"cmd": "ls"}, {"cmd": "man pkill"}])
        widths = {span_width(m) for m in SPAN.finditer(out)}
        self.assertEqual(widths, {len("sudo pkill -f vite")})
        self.assertIn("- ✗ `pkill a           ` inferred\n", out)
        self.assertIn("- ✓ `ls                ` inferred\n", out)

    def test_width_is_capped_and_longer_commands_go_last_unpadded(self) -> None:
        long = "pkill " + "x" * 50
        out = self.card(PKILL, [{"cmd": long}, {"cmd": "pkill a"}, {"cmd": "pkill " + "y" * 45}, {"cmd": "pkill b"}])
        block = self.rows(out)
        self.assertEqual([r.split("`")[1].rstrip() for r in block],
                         ["pkill a", "pkill b", long, "pkill " + "y" * 45])
        self.assertEqual(block[0], "- ✗ `pkill a" + " " * 33 + "` inferred")
        self.assertEqual(block[2], f"- ✗ `{long}` inferred")

    def test_exactly_forty_is_padded_not_moved(self) -> None:
        exact = "pkill " + "x" * 34
        out = self.card(PKILL, [{"cmd": exact}, {"cmd": "pkill a"}])
        self.assertEqual([r.split("`")[1].rstrip() for r in self.rows(out)], [exact, "pkill a"])

    def test_backtick_commands_count_the_extra_width(self) -> None:
        out = self.card(PKILL, [{"cmd": "echo `pkill x`"}, {"cmd": "pkill aaaaaaaaaaaaaaaaaaaa"}])
        self.assertEqual({span_width(m) for m in SPAN.finditer(out)}, {len("pkill aaaaaaaaaaaaaaaaaaaa")})
        self.assertIn("- ✗ `` echo `pkill x`" + " " * 12 + " `` inferred", out)

    def test_unicode_width(self) -> None:
        out = self.card(PKILL, [{"cmd": "pkill 日本語"}, {"cmd": "pkill abcdefgh"}, {"cmd": "pkill 😀"}])
        self.assertEqual({span_width(m) for m in SPAN.finditer(out)}, {len("pkill abcdefgh")})
        self.assertIn("`pkill 日本語" + " " * 2 + "`", out)
        self.assertIn("`pkill 😀" + " " * 6 + "`", out)

    def test_newline_is_shown_as_a_glyph(self) -> None:
        out = self.card(PKILL, [{"cmd": "a\npkill x"}])
        self.assertIn("`a⏎pkill x`", out)

    def test_wrapped_is_computed_by_the_engine(self) -> None:
        wrapped = ["sudo pkill x", "bash -c 'pkill x'", "xargs pkill", "timeout 5 pkill x", "echo $(pkill x)",
                   "echo `pkill x`", "ps | pkill x", "pkill x | cat", "env A=1 pkill x", "sh -c 'a; pkill x'"]
        direct = ["pkill x", "/usr/bin/pkill x", "FOO=1 pkill x", "a; pkill x", "a && pkill x"]
        out = self.card(PKILL, [{"cmd": c} for c in wrapped + direct])
        for row in self.rows(out):
            self.assertEqual(row.endswith("· wrapped"), command_of(row) in wrapped, row)

    def test_regex_matches_are_never_wrapped_and_beat_a_wrapped_program_match(self) -> None:
        rule = {"match": {"regex": r"curl [^|]*\| *sh"}, "message": "m"}
        out = self.card(rule, [{"cmd": "sudo curl x | sh"}, {"cmd": "echo hi"}])
        self.assertNotIn("wrapped", out)
        both = {"match": {"program": "pkill", "regex": "pkill"}, "message": "m"}
        self.assertNotIn("wrapped", self.card(both, [{"cmd": "sudo pkill x"}]))

    def test_unbalanced_quotes_fall_back_to_direct(self) -> None:
        self.assertNotIn("wrapped", self.card(PKILL, [{"cmd": "pkill 'x"}]))

    def test_block_versus_warn_heading(self) -> None:
        deny = self.card(PKILL, [{"cmd": "pkill a"}, {"cmd": "ls"}])
        self.assertIn("**Block**\n- ✗", deny)
        self.assertNotIn("**Warn**", deny)
        warn = self.card({**PKILL, "action": "warn"}, [{"cmd": "pkill a"}, {"cmd": "ls"}])
        self.assertIn("### new-rule · warn · retry same-command · global", warn)
        self.assertIn("**Warn**\n- ✗", warn)
        self.assertNotIn("**Block**", warn)
        self.assertIn("**Allow**\n- ✓", warn)

    def test_empty_groups_are_omitted(self) -> None:
        only_hits = self.card(PKILL, [{"cmd": "pkill a"}])
        self.assertNotIn("**Allow**", only_hits)
        only_passes = self.card(PKILL, [{"cmd": "ls"}])
        self.assertNotIn("**Block**", only_passes)
        self.assertIn("**Allow**", only_passes)

    def test_title_omits_retry_when_none(self) -> None:
        out = self.card({"match": {"program": "x"}, "message": "m"}, [{"cmd": "x"}])
        self.assertTrue(out.startswith("### new-rule · deny · global\n"))

    def test_draft_ids(self) -> None:
        rule = {**PKILL, "id": "from-rule"}
        self.assertTrue(self.card(rule, [{"cmd": "ls"}]).startswith("### from-rule ·"))
        self.assertTrue(self.card(rule, [{"cmd": "ls"}], "--id-name", "named").startswith("### named ·"))

    def test_draft_scope_label_and_managed_file(self) -> None:
        out = self.card(PKILL, [{"cmd": "ls"}], "--scope", "project")
        self.assertTrue(out.startswith("### new-rule · deny · retry same-command · project\n"))
        out = self.card(PKILL, [{"cmd": "ls"}], "--scope", "managed")
        self.assertIn(f"\n**File** `{self.mpath}`\n", out)
        os.environ.pop("GUARDRAILS_MANAGED_PATH")
        self.assertNotIn("**File**", self.card(PKILL, [{"cmd": "ls"}], "--scope", "managed"))

    def test_mismatches_are_flagged_and_counted(self) -> None:
        out = self.card(PKILL, [{"cmd": "pkill a", "expect": "pass"}, {"cmd": "ls", "expect": "match"},
                                {"cmd": "pkill b", "expect": "match"}, {"cmd": "ls", "expect": "pass"},
                                {"cmd": "pkill c"}, {"cmd": "ls"}])
        flagged = [r for r in self.rows(out) if "⚠" in r]
        self.assertEqual(len(flagged), 2)
        self.assertTrue(flagged[0].startswith("- ✗ ⚠ `pkill a"))
        self.assertTrue(flagged[1].startswith("- ✓ ⚠ `ls"))
        self.assertIn("**Verified** matcher checked with `rule test`, 6 commands, 2 mismatches\n", out)

    def test_no_expectation_is_no_mismatch(self) -> None:
        out = self.card(PKILL, [{"cmd": "pkill a"}, {"cmd": "ls"}])
        self.assertNotIn("⚠", out)
        self.assertIn("2 commands, 0 mismatches", out)

    def test_singular_counts(self) -> None:
        out = self.card(PKILL, [{"cmd": "pkill a", "expect": "pass"}])
        self.assertIn("1 command, 1 mismatch\n", out)

    def test_raw_shows_only_the_regex_else_args(self) -> None:
        regex = r"curl [^|]*\|\s*(sh|bash)`"
        out = self.card({"match": {"regex": regex}, "message": "m"}, [{"cmd": "ls"}])
        self.assertIn(f"\n**Raw** `` {regex} ``\n" if regex.endswith("`") else f"\n**Raw** `{regex}`", out + "\n")
        self.assertTrue(out.rstrip("\n").endswith("``"))
        args = self.card({"match": {"program": "x", "args": "-f"}, "message": "m"}, [{"cmd": "x -f"}])
        self.assertTrue(args.rstrip("\n").endswith("**Raw** `-f`"))
        both = self.card({"match": {"program": "x", "args": "-f", "regex": "zz"}, "message": "m"}, [{"cmd": "x"}])
        self.assertTrue(both.rstrip("\n").endswith("**Raw** `zz`"))
        self.assertNotIn("**Raw**", self.card(PKILL, [{"cmd": "ls"}]))

    def test_match_line_shows_every_field(self) -> None:
        rule = {"match": {"program": "grep", "args": "-r", "regex": "zz"}, "message": "m"}
        self.assertIn("**Match** program = `grep`; args = `-r`; regex = `zz`\n",
                      self.card(rule, [{"cmd": "ls"}]))

    def test_intent_only_when_given(self) -> None:
        self.assertNotIn("**Intent**", self.card(PKILL, [{"cmd": "ls"}]))
        self.assertIn("\n**Intent** stop it\n", self.card(PKILL, [{"cmd": "ls"}], "--intent", "stop it"))

    def test_notes_are_one_line(self) -> None:
        rule = {**PKILL, "enabled": False, "requires": ["no-such-bin-xyz"]}
        out = self.card(rule, [{"cmd": "pkill a"}])
        notes = [line for line in out.splitlines() if line.startswith("**Note**")]
        self.assertEqual(len(notes), 1)
        self.assertIn("rule is disabled; none of no-such-bin-xyz is installed here", notes[0])
        self.assertNotIn("**Note**", self.card(PKILL, [{"cmd": "pkill a"}]))

    def test_message_placeholder_is_resolved(self) -> None:
        rule = {"match": {"program": "x"}, "message": "use {which:definitely-missing-a|definitely-missing-b}"}
        self.assertIn("**Message** use definitely-missing-a\n", self.card(rule, [{"cmd": "x"}]))

    def test_installed_rule_scope_is_its_layers(self) -> None:
        self.cli("rule", "add", "no-pkill", "--json", json.dumps(PKILL))
        self.cli("rule", "set", "no-pkill", "--json", '{"retry": "none"}', "--scope", "project")
        code, out, _ = self.cli("rule", "test", "--id", "no-pkill", "pkill a", "ls")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### no-pkill · deny · global+project\n"))
        self.assertIn("inferred", out)

    def test_positional_commands_and_source_flag(self) -> None:
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(PKILL), "pkill a", "ls",
                                "--source", "yours")
        self.assertEqual(code, 0)
        self.assertEqual(out.count(" yours\n"), 2)

    def test_positional_then_examples(self) -> None:
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(PKILL), "pkill a",
                                "--examples", json.dumps([{"cmd": "pkill b", "source": "yours"}, "pkill c"]))
        self.assertEqual(code, 0)
        self.assertEqual([r.split("`")[1].rstrip() for r in self.rows(out)], ["pkill a", "pkill b", "pkill c"])
        self.assertIn(" yours\n", out)

    def test_examples_from_file_and_stdin(self) -> None:
        path = self.tmp / "ex file.json"
        path.write_text(json.dumps([{"cmd": "pkill a", "source": "yours"}]))
        code, from_file, _ = self.cli("rule", "test", "--json", json.dumps(PKILL), "--examples",
                                      f"@{path}")
        self.assertEqual(code, 0)
        with mock.patch("sys.stdin", io.StringIO(path.read_text())):
            code, from_stdin, _ = self.cli("rule", "test", "--json", json.dumps(PKILL), "--examples", "-")
        self.assertEqual(code, 0)
        self.assertEqual(from_file, from_stdin)
        self.assertIn(" yours\n", from_file)

    def test_examples_errors(self) -> None:
        for bad, fragment in (("{", "not valid JSON"), ('{"cmd": "x"}', "JSON list"), ('[{"source": "yours"}]', "'cmd'"),
                              ('[{"cmd": "x", "source": "me"}]', "source must be"),
                              ('[{"cmd": "x", "expect": "block"}]', "expect must be"),
                              ('[{"cmd": "x", "expects": "match"}]', "unknown keys"), ("[]", "at least one"),
                              ("@/no/such/file.json", "cannot read")):
            with self.subTest(bad=bad):
                code, out, err = self.cli("rule", "test", "--json", json.dumps(PKILL), "--examples", bad)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(fragment, err)

    def test_commands_are_required_without_examples(self) -> None:
        code, _, err = self.cli("rule", "test", "--json", json.dumps(PKILL))
        self.assertEqual(code, 2)
        self.assertIn("at least one command", err)

    def test_draft_labels_are_rejected_for_an_installed_rule(self) -> None:
        self.cli("rule", "add", "r", "--json", json.dumps(PKILL))
        for label in ("--scope", "--id-name"):
            self.assertEqual(self.cli("rule", "test", "--id", "r", label, "project", "ls")[0], 2)

    def test_render_output_is_unfenced(self) -> None:
        out = self.card(PKILL, GOLDEN_EXAMPLES)
        self.assertNotIn("```\n", out)
        self.assertNotIn("|", out.replace("pgrep -fl node | xargs", ""))


class StatusRender(AstIsolated):
    def status(self, *extra: str) -> str:
        code, out, err = self.cli("status", *extra)
        self.assertEqual((code, err), (0, ""))
        return out

    def rule(self, **fields: object) -> str:
        return json.dumps({"match": {"program": "x"}, "message": "m", **fields})

    def test_first_line_default_absent_no_override(self) -> None:
        os.environ.pop("GUARDRAILS_MANAGED_PATH")
        first = self.status().splitlines()[0]
        self.assertEqual(first, f"**Managed** platform file `{self.dpath}` absent · no managed file is present, "
                                "so there are no managed rules")

    def test_first_line_default_present(self) -> None:
        self.put(self.dpath, {})
        os.environ.pop("GUARDRAILS_MANAGED_PATH")
        self.assertEqual(self.status().splitlines()[0], f"**Managed** platform file `{self.dpath}` present")

    def test_first_line_override_and_path(self) -> None:
        self.put(self.mpath, {})
        extra = self.tmp / "extra.json"
        first = self.status("--path", str(extra)).splitlines()[0]
        self.assertIn(f"platform file `{self.dpath}` absent", first)
        self.assertIn(f"override `{self.mpath}` present", first)
        self.assertIn(f"--path `{extra}` absent", first)
        self.assertIn(f"managed rules come only from `{self.mpath}`", first)
        self.assertIn("the --path file is read for this status only", first)

    def test_managed_line_cases(self) -> None:
        cases = {
            "override only": ([self.mpath], (), [f"managed rules come only from `{self.mpath}`"], []),
            "default and override": ([self.dpath, self.mpath], (), [], ["come only from"]),
            "--path equal to the env override": ([self.mpath], ("--path", str(self.mpath)), [], ["--path `"]),
            "--path equal to the platform default": ([self.dpath], ("--path", str(self.dpath)), [],
                                                     ["--path `", "the hook enforces", "come only from"]),
            "no managed file": ([], (), [], ["come only from"]),
        }
        for name, (files, argv, present, absent) in cases.items():
            with self.subTest(case=name):
                for path in files:
                    self.put(path, {})
                first = self.status(*argv).splitlines()[0]
                for text in present:
                    self.assertIn(text, first)
                for text in absent:
                    self.assertNotIn(text, first)
                for path in files:
                    path.unlink()

    def test_rule_rows_and_states(self) -> None:
        self.put(self.mpath, {"rules": {"kill-9": json.loads(self.rule(action="warn"))}})
        self.cli("rule", "add", "no-strings", "--json", self.rule(modes=["re"]))
        self.cli("rule", "add", "old-rule", "--json", self.rule(enabled=False))
        self.cli("rule", "add", "plain", "--json", self.rule())
        self.cli("mode", "declare", "re", "--agent-may-enable", "--description", "RE work")
        self.cli("mode", "declare", "incident")
        self.cli("mode", "on", "re", "--session-id", "s1", "--reason", "RE")
        out = self.status("--session-id", "s1")
        self.assertIn("### Guardrails · 4 rules · hook on\n", out)
        for row in ("- `kill-9    ` warn · managed · always enforced\n",
                    "- `no-strings` deny · global · suspended by re\n",
                    "- `old-rule  ` deny · global · disabled\n",
                    "- `plain     ` deny · global · enabled\n"):
            self.assertIn(row, out)
        self.assertIn("**Modes**\n- `incident` off · agent may enable: no · global\n"
                      "- `re      ` on (by user: RE) · agent may enable: yes · global\n".replace("`incident`",
                                                                                               "`incident`"), out)

    def test_persistent_mode_reads_on_persistent(self) -> None:
        self.cli("mode", "declare", "m")
        self.cli("mode", "on", "m", "--scope", "global")
        self.assertIn("- `m` on (persistent) · agent may enable: no · global", self.status())

    def test_hook_off_and_project_off_are_in_the_header(self) -> None:
        self.cli("rule", "add", "g", "--json", self.rule())
        self.cli("disable", "--reason", "testing")
        self.put(self.ppath, {"enabled": False})
        out = self.status()
        self.assertIn("### Guardrails · 1 rule · hook off · project rules off\n", out)
        self.assertIn("**Note** the global hook is disabled (testing)\n", out)

    def test_no_rules_message_and_no_modes_group(self) -> None:
        out = self.status()
        self.assertIn("**Rules**\nNo rules installed. guardrails:setup installs presets.", out)
        self.assertNotIn("**Modes**", out)

    def test_problems_group_and_problems_flag(self) -> None:
        self.put(self.gpath, {"rules": {"bad": {"match": {"builtin": "nope"}, "message": "x"}}})
        out = self.status()
        self.assertIn("**Problems**\n- ", out)
        self.assertIn("rule bad:", out)
        only = self.status("--problems")
        self.assertTrue(only.startswith("**Problems**\n"))
        self.assertNotIn("### Guardrails", only)
        self.put(self.gpath, {})
        os.chmod(self.tmp, 0o755)
        self.put(self.mpath, {})
        os.environ.pop("GUARDRAILS_MANAGED_PATH")
        self.assertEqual(self.status("--problems"), "No problems.\n")

    def test_scope_filter(self) -> None:
        self.put(self.mpath, {"rules": {"m-rule": json.loads(self.rule())}})
        self.cli("rule", "add", "g-rule", "--json", self.rule())
        self.cli("rule", "add", "p-rule", "--json", self.rule(), "--scope", "project")
        self.cli("mode", "declare", "gm")
        self.cli("mode", "declare", "pm", "--scope", "project")
        for scope, rule, mode in (("global", "g-rule", "gm"), ("project", "p-rule", "pm"), ("managed", "m-rule", None)):
            with self.subTest(scope=scope):
                out = self.status("--scope", scope)
                self.assertIn("1 rule ·", out)
                self.assertIn(f"`{rule}`", out)
                self.assertEqual(f"`{mode}`" in out, mode is not None)
        empty = self.status("--scope", "project", "--path", str(self.tmp / "none.json"))
        self.assertIn("p-rule", empty)
        self.cli("rule", "rm", "p-rule", "--scope", "project")
        self.assertIn("No rules with a project entry.", self.status("--scope", "project"))

    def test_rule_flag_prints_one_row(self) -> None:
        self.cli("rule", "add", "a-rule", "--json", self.rule())
        self.cli("rule", "add", "b-rule", "--json", self.rule(action="warn"))
        self.assertEqual(self.status("--rule", "b-rule"), "- `b-rule` warn · global · enabled\n")
        code, _, err = self.cli("status", "--rule", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("no rule 'ghost'", err)

    def test_long_ids_are_capped(self) -> None:
        long = "r" * 50
        self.cli("rule", "add", long, "--json", self.rule())
        self.cli("rule", "add", "short", "--json", self.rule())
        out = self.status()
        self.assertIn(f"- `{long}` deny", out)
        self.assertIn("- `short" + " " * 35 + "` deny", out)

    def test_agent_can_read_status(self) -> None:
        code, _, _ = self.cli("status", agent=True)
        self.assertEqual(code, 0)


class InputSources(AstIsolated):
    RULE = json.dumps({"match": {"program": "strings"}, "message": "Don't use strings."})

    def test_add_from_file_stdin_and_home(self) -> None:
        spaced = self.tmp / "my rules" / "rule.json"
        self.put(spaced, self.RULE)
        self.assertEqual(self.cli("rule", "add", "a", "--json", f"@{spaced}")[0], 0)
        with mock.patch("sys.stdin", io.StringIO(self.RULE)):
            self.assertEqual(self.cli("rule", "add", "b", "--json", "-")[0], 0)
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "my rules")}):
            self.assertEqual(self.cli("rule", "add", "c", "--json", "@~/rule.json")[0], 0)
        rules = self.get(self.gpath)["rules"]
        for rid in "abc":
            self.assertEqual(rules[rid]["message"], "Don't use strings.")

    def test_test_from_file_and_stdin(self) -> None:
        path = self.tmp / "r.json"
        self.put(path, self.RULE)
        code, out, _ = self.cli("rule", "test", "--json", f"@{path}", "strings x", "ls")
        self.assertEqual(code, 0)
        self.assertIn("- ✗ `strings x` inferred", out)
        with mock.patch("sys.stdin", io.StringIO(self.RULE)):
            code, out, _ = self.cli("rule", "test", "--json", "-", "strings x")
        self.assertEqual(code, 0)
        self.assertIn("- ✗ `strings x` inferred", out)

    def test_hard_quoting_survives_a_file(self) -> None:
        hard = {"match": {"regex": "echo `pkill $(x)` \"y\" 'z'"}, "message": "it's \"hard\""}
        path = self.tmp / "hard.json"
        self.put(path, json.dumps(hard))
        self.assertEqual(self.cli("rule", "add", "hard", "--json", f"@{path}")[0], 0)
        stored = self.get(self.gpath)["rules"]["hard"]
        self.assertEqual((stored["match"], stored["message"]), (hard["match"], hard["message"]))

    def test_missing_file_and_invalid_json_exit_2(self) -> None:
        bad = self.tmp / "bad.json"
        self.put(bad, "{nope")
        for verb in (("rule", "add", "x"), ("rule", "test", "ls")):
            with self.subTest(verb=verb):
                code, _, err = self.cli(*verb, "--json", f"@{self.tmp / 'missing.json'}")
                self.assertEqual(code, 2)
                self.assertIn("--json: cannot read", err)
                self.assertIn("missing.json", err)
                code, _, err = self.cli(*verb, "--json", f"@{bad}")
                self.assertEqual(code, 2)
                self.assertIn("not valid JSON", err)
                self.assertIn(str(bad), err)
        self.assertFalse(self.gpath.exists())

    def test_stdin_cannot_serve_two_flags(self) -> None:
        code, _, err = self.cli("rule", "test", "--json", "-", "--examples", "-")
        self.assertEqual(code, 2)
        self.assertIn("only one of --json and --examples can read stdin", err)

    def test_set_from_json(self) -> None:
        self.cli("rule", "add", "r", "--json", self.RULE)
        path = self.tmp / "set.json"
        self.put(path, json.dumps({"message": "It's `new`", "program": ["strings", "otool"], "enabled": True,
                                   "modes": ["re"], "regex": "a'b"}))
        code, out, _ = self.cli("rule", "set", "r", "--json", f"@{path}")
        self.assertEqual(code, 0, out)
        rule = self.get(self.gpath)["rules"]["r"]
        self.assertEqual((rule["message"], rule["match"], rule["modes"]),
                         ("It's `new`", {"program": ["strings", "otool"], "regex": "a'b"}, ["re"]))
        with mock.patch("sys.stdin", io.StringIO('{"action": "warn", "retry": "same-command"}')):
            self.assertEqual(self.cli("rule", "set", "r", "--json", "-")[0], 0)
        rule = self.get(self.gpath)["rules"]["r"]
        self.assertEqual((rule["action"], rule["retry"]), ("warn", "same-command"))


def control_chars(text: str) -> list[str]:
    import unicodedata

    return [c for c in text if c != "\n" and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")]


EVIL = "x\x1b[2J\ty\u2028z\x85w\u202ev\u200bu\U000e0041"


class Sanitising(AstIsolated):
    def test_clean_replaces_every_class(self) -> None:
        cleaned = render.clean(EVIL + "\x00\x7f")
        self.assertEqual(control_chars(cleaned), [])
        for shown in ("␛", "⇥", "\\u{2028}", "\\u{85}", "\\u{202e}", "\\u{200b}", "\\u{e0041}", "\\u{0}", "␡"):
            self.assertIn(shown, cleaned)
        self.assertEqual(render.clean(render.clean(EVIL)), render.clean(EVIL))

    def test_card_fields_never_break_the_block(self) -> None:
        forged = "x\n### Forged\n- ✗ `fake` yours"
        rule = {**PKILL, "id": forged, "message": forged}
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(rule), "--intent", forged,
                                "--examples", json.dumps([{"cmd": forged}, {"cmd": "pkill " + EVIL}, "ls"]))
        self.assertEqual(code, 0)
        self.assertEqual(control_chars(out), [])
        self.assertNotIn("\x1b", out)
        for line in out.splitlines():
            self.assertFalse(line.startswith(("### Forged", "- ✗ `fake")), line)
        self.assertEqual(sum(line.startswith("### ") for line in out.splitlines()), 1)

    def test_tabs_and_controls_align_on_the_cleaned_text(self) -> None:
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(PKILL), "--examples",
                                json.dumps([{"cmd": "pkill\ta"}, {"cmd": "pkill \x1bb"}, {"cmd": "pkill abcdefghi"}]))
        self.assertEqual(code, 0)
        self.assertEqual({span_width(m) for m in SPAN.finditer(out)}, {len("pkill abcdefghi")})

    def test_status_render_neutralises_state_file_text(self) -> None:
        forged = "bad\n### Forged\n- `fake` deny\x1b[2J"
        self.put(self.gpath, {"rules": {forged: {"match": {"program": "x"}, "message": "m"}},
                              "modes": {forged: {"description": forged, "agentMayEnable": True}},
                              "sessions": {"s1": {"modes": {forged: {"by": "agent", "reason": forged}}}}})
        for argv in (("status", "--session-id", "s1"), ("status", "--rule", forged)):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual((code, control_chars(out), "\x1b" in out), (0, [], False), err)
                for line in out.splitlines():
                    self.assertFalse(line.startswith(("### Forged", "- `fake")), line)

    def test_problems_are_neutralised(self) -> None:
        self.put(self.gpath, {"rules": {"bad": {"match": {"builtin": "nope"}, "message": "x\x1b"}}})
        self.put(self.ppath, {"rules": {"r\x1b": {"match": {"program": "x"}, "message": "m", "modes": ["g\nh"]}}})
        out = self.cli("status")[1]
        self.assertEqual((control_chars(out), "\x1b" in out), ([], False))
        self.assertIn("mode 'g h'", out)


class WrappedForms(AstIsolated):
    def kinds(self, commands: list[str]) -> dict[str, str | None]:
        import matching
        import policy

        rule = policy.Rule.from_json({"match": {"program": "pkill"}, "message": "m"})
        return {c: matching.evaluate(c, {"r": rule}).kinds["r"] for c in commands}

    def test_glued_substitutions_and_process_substitution(self) -> None:
        commands = ["echo foo$(pkill x)", "x=$(pkill y)", "echo --a=$(pkill x)", "cat <(pkill x)", "echo $(pkill x)",
                    "echo $( pkill x)", "echo `pkill x`", "echo \"$(pkill x)\"", "echo a$(b $(pkill x))"]
        for command, kind in self.kinds(commands).items():
            self.assertEqual(kind, "wrapped", command)

    def test_pipelines_including_newline_continuation(self) -> None:
        for command, kind in self.kinds(["a | pkill x", "a |\n pkill x", "a | \n pkill x", "pkill x | b",
                                         "a |& pkill x"]).items():
            self.assertEqual(kind, "wrapped", command)

    def test_lists_and_subshell_groups_are_not_wrappers(self) -> None:
        for command, kind in self.kinds(["a; pkill x", "a && pkill x", "a || pkill x", "a\npkill x", "(pkill x)",
                                         "{ pkill x; }", "pkill x &"]).items():
            self.assertEqual(kind, "direct", command)

    def test_verdicts_agree_with_the_hook(self) -> None:
        import engine
        import policy

        rules = [{"match": {"program": "pkill"}}, {"match": {"regex": r"curl [^|]*\| *sh"}},
                 {"match": {"program": "grep", "args": "-r"}}, {"match": {"ast": GREP_RECURSIVE}},
                 {"match": {"program": "pkill", "regex": "zz"}}]
        commands = ["pkill x", "sudo pkill x", "echo foo$(pkill x)", "cat <(pkill x)", "a | pkill x", "curl x | sh",
                    "bash -c 'curl y | sh'", "grep -r x", "grep x", "grep -R y .", "echo pkill", "man pkill",
                    "pkill 'x", "cat <<EOF\npkill x\nEOF", "xargs pkill", "zz", "ls"]
        for raw in rules:
            rule = policy.Rule.from_json({**raw, "message": "m"})
            out = self.cli("rule", "test", "--json", json.dumps({**raw, "message": "m"}), *commands)[1]
            caught = {command_of(row): row.startswith("- ✗") for row in out.splitlines() if row.startswith(("- ✗", "- ✓"))}
            for command in commands:
                with self.subTest(rule=raw, command=command):
                    output, _ = engine.evaluate(command, {"r": rule}, {}, policy.Session(), "s")
                    self.assertEqual(caught[render.clean(command)], output is not None)


class NotEvaluated(AstIsolated):
    def test_a_rule_the_engine_cannot_judge_is_never_shown_as_allowed(self) -> None:
        self.break_engine()
        examples = json.dumps([{"cmd": "pkill a", "expect": "match"}, {"cmd": "ls", "expect": "pass"}])
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(PKILL), "--examples", examples)
        self.assertEqual(code, 0)
        self.assertIn("**Not evaluated**", out)
        self.assertIn("- ? ⚠ `pkill a", out)
        self.assertNotIn("**Allow**", out)
        self.assertIn("**Verified** NOT verified: 2 of 2 commands could not be evaluated", out)
        self.assertNotIn("matcher checked", out)
        self.assertIn("cannot evaluate the parsing part of this rule", out)

    def test_a_regex_rule_is_judged_by_the_engine_like_the_rest(self) -> None:
        rule = json.dumps({"match": {"regex": "^pkill"}, "message": "m"})
        code, out, _ = self.cli("rule", "test", "--json", rule, "pkill a", "ls")
        self.assertEqual(code, 0)
        self.assertIn("**Block**", out)
        self.break_engine()
        out = self.cli("rule", "test", "--json", rule, "pkill a", "ls")[1]
        self.assertIn("Not evaluated", out)

    def test_an_oversize_command_is_not_judged_either(self) -> None:
        import matching

        out = self.cli("rule", "test", "--json", json.dumps(PKILL), "x" * (matching.MAX_COMMAND + 1))[1]
        self.assertIn("cannot evaluate the parsing part of this rule: command too large to check", out)


class Isolation(AstIsolated):
    def test_render_stdout_is_only_the_block(self) -> None:
        extra = self.tmp / "custom.json"
        code, out, err = self.cli("rule", "test", "--json", json.dumps(PKILL), "--scope", "managed",
                                  "--path", str(extra), "pkill a")
        self.assertEqual(code, 0)
        self.assertNotIn("\nnote:", "\n" + out)
        self.assertIn(f"**File** `{extra}`", out)
        self.assertIn(f"**Note** the hook enforces {extra} only if GUARDRAILS_MANAGED_PATH points at it", out)
        self.assertIn("the hook enforces", err)
        code, out, err = self.cli("status", "--scope", "managed", "--path", str(extra))
        self.assertEqual(code, 0)
        self.assertNotIn("\nnote:", "\n" + out)
        self.assertTrue(out.startswith("**Managed**"))
        code, out, _ = self.cli("rule", "add", "x", "--json", json.dumps(PKILL), "--scope", "managed", "--path",
                                str(extra))
        self.assertIn("note: the hook enforces", out)

    def test_status_scope_filters_problems(self) -> None:
        self.put(self.gpath, {"rules": {"g-bad": {"match": {"program": "x"}, "message": "m", "modes": ["a"]}}})
        self.put(self.ppath, {"rules": {"p-bad": {"match": {"program": "x"}, "message": "m", "modes": ["b"]}}})
        allp = self.cli("status", "--problems")[1]
        self.assertIn("g-bad", allp)
        self.assertIn("p-bad", allp)
        for scope, present, absent in (("global", "g-bad", "p-bad"), ("project", "p-bad", "g-bad")):
            for extra in ((), ("--problems",)):
                out = self.cli("status", "--scope", scope, *extra)[1]
                self.assertIn(present, out)
                self.assertNotIn(absent, out)
            self.assertNotIn(absent, self.cli("status", "--scope", scope)[1])
        self.assertEqual(self.cli("status", "--scope", "managed", "--problems")[1], "No problems.\n")

    def test_unreadable_layer_problems_follow_their_scope(self) -> None:
        self.put(self.gpath, "{nope")
        self.assertIn("unreadable global", self.cli("status", "--scope", "global")[1])
        self.assertNotIn("unreadable global", self.cli("status", "--scope", "project")[1])


class Inputs(AstIsolated):
    def test_id_name_is_validated(self) -> None:
        code, _, err = self.cli("rule", "test", "--json", json.dumps(PKILL), "--id-name",
                                "bad id ### x", "ls")
        self.assertEqual(code, 2)
        self.assertIn("must match", err)

    def test_blank_commands_are_rejected(self) -> None:
        for argv in (("--json", json.dumps(PKILL), "  "), ("--json", json.dumps(PKILL), "--examples", '[{"cmd": " "}]'),
                     ("--json", json.dumps(PKILL), "--examples", '[" "]')):
            with self.subTest(argv=argv):
                code, out, _ = self.cli("rule", "test", *argv)
                self.assertEqual((code, out), (2, ""))

    def test_envelope_carries_rule_and_examples_through_one_stdin(self) -> None:
        document = json.dumps({"rule": PKILL, "examples": [{"cmd": "pkill a", "source": "yours", "expect": "match"},
                                                          {"cmd": "ls", "expect": "pass"}]})
        with mock.patch("sys.stdin", io.StringIO(document)):
            code, out, _ = self.cli("rule", "test", "--json", "-", "--id-name", "no-pkill")
        self.assertEqual(code, 0)
        self.assertIn("2 commands, 0 mismatches", out)
        self.assertIn(" yours\n", out)
        with mock.patch("sys.stdin", io.StringIO(document)):
            self.assertEqual(self.cli("rule", "add", "no-pkill", "--json", "-")[0], 0)
        stored = self.get(self.gpath)["rules"]["no-pkill"]
        self.assertEqual(stored["match"], PKILL["match"])
        self.assertNotIn("examples", stored)
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"rule": PKILL, "examples": "x"}))):
            self.assertEqual(self.cli("rule", "test", "--json", "-")[0], 2)


if __name__ == "__main__":
    unittest.main()
