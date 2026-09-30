from __future__ import annotations

import io
import json
import os
import re
import unittest
from unittest import mock

import render
from helpers import ROOT, Isolated

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
        self.assertEqual(render.one_line("a\nb\r\nc"), "a⏎b⏎c")

    def test_common_width_is_capped(self) -> None:
        self.assertEqual(render.common_width(["a" * 10, "b"]), 10)
        self.assertEqual(render.common_width(["a" * 90, "b"]), 40)
        self.assertEqual(render.common_width([]), 0)


class Card(Isolated):
    def card(self, rule: dict, examples: list, *extra: str) -> str:
        code, out, err = self.cli("rule", "test", "--render", "--json", json.dumps(rule), "--examples",
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
        builtin = self.card({"match": {"builtin": "grep-recursive"}, "message": "m"}, [{"cmd": "grep -r x"}])
        self.assertIn("**Match** builtin = `grep-recursive`", builtin)
        self.assertNotIn("**Raw**", builtin)

    def test_match_line_shows_every_field(self) -> None:
        rule = {"match": {"program": "grep", "args": "-r", "builtin": "grep-recursive", "regex": "zz"}, "message": "m"}
        self.assertIn("**Match** program = `grep`; args = `-r`; regex = `zz`; builtin = `grep-recursive`\n",
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
        self.cli("rule", "set", "no-pkill", "retry=none", "--project")
        code, out, _ = self.cli("rule", "test", "--render", "--id", "no-pkill", "pkill a", "ls")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### no-pkill · deny · global+project\n"))
        self.assertIn("inferred", out)

    def test_positional_commands_and_source_flag(self) -> None:
        code, out, _ = self.cli("rule", "test", "--render", "--json", json.dumps(PKILL), "pkill a", "ls",
                                "--source", "yours")
        self.assertEqual(code, 0)
        self.assertEqual(out.count(" yours\n"), 2)

    def test_positional_then_examples(self) -> None:
        code, out, _ = self.cli("rule", "test", "--render", "--json", json.dumps(PKILL), "pkill a",
                                "--examples", json.dumps([{"cmd": "pkill b", "source": "yours"}, "pkill c"]))
        self.assertEqual(code, 0)
        self.assertEqual([r.split("`")[1].rstrip() for r in self.rows(out)], ["pkill a", "pkill b", "pkill c"])
        self.assertIn(" yours\n", out)

    def test_examples_from_file_and_stdin(self) -> None:
        path = self.tmp / "ex file.json"
        path.write_text(json.dumps([{"cmd": "pkill a", "source": "yours"}]))
        code, from_file, _ = self.cli("rule", "test", "--render", "--json", json.dumps(PKILL), "--examples",
                                      f"@{path}")
        self.assertEqual(code, 0)
        with mock.patch("sys.stdin", io.StringIO(path.read_text())):
            code, from_stdin, _ = self.cli("rule", "test", "--render", "--json", json.dumps(PKILL), "--examples", "-")
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
                code, out, err = self.cli("rule", "test", "--render", "--json", json.dumps(PKILL), "--examples", bad)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(fragment, err)

    def test_commands_are_required_without_examples(self) -> None:
        code, _, err = self.cli("rule", "test", "--render", "--json", json.dumps(PKILL))
        self.assertEqual(code, 2)
        self.assertIn("at least one command", err)

    def test_render_only_flags_are_rejected_elsewhere(self) -> None:
        self.assertEqual(self.cli("rule", "test", "--json", json.dumps(PKILL), "--intent", "x", "ls")[0], 2)
        self.assertEqual(self.cli("rule", "test", "--json", json.dumps(PKILL), "--id-name", "x", "ls")[0], 2)
        self.cli("rule", "add", "r", "--json", json.dumps(PKILL))
        self.assertEqual(self.cli("rule", "test", "--render", "--id", "r", "--scope", "project", "ls")[0], 2)

    def test_plain_rule_test_output_is_unchanged_by_examples(self) -> None:
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(PKILL), "--examples", '["pkill a"]', "ls")
        self.assertEqual(code, 0)
        self.assertIn("  -      ls\n  match  pkill a\n", out)

    def test_render_output_is_unfenced(self) -> None:
        out = self.card(PKILL, GOLDEN_EXAMPLES)
        self.assertNotIn("```\n", out)
        self.assertNotIn("|", out.replace("pgrep -fl node | xargs", ""))


class StatusRender(Isolated):
    def status(self, *extra: str) -> str:
        code, out, err = self.cli("status", "--render", *extra)
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
        self.put(self.gpath, {"rules": {"bad": {"match": {"regex": "("}, "message": "x"}}})
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
        self.cli("rule", "add", "p-rule", "--json", self.rule(), "--project")
        self.cli("mode", "declare", "gm")
        self.cli("mode", "declare", "pm", "--project")
        for scope, rule, mode in (("global", "g-rule", "gm"), ("project", "p-rule", "pm"), ("managed", "m-rule", None)):
            with self.subTest(scope=scope):
                out = self.status("--scope", scope)
                self.assertIn("1 rule ·", out)
                self.assertIn(f"`{rule}`", out)
                self.assertEqual(f"`{mode}`" in out, mode is not None)
        empty = self.status("--scope", "project", "--path", str(self.tmp / "none.json"))
        self.assertIn("p-rule", empty)
        self.cli("rule", "rm", "p-rule", "--project")
        self.assertIn("No rules with a project entry.", self.status("--scope", "project"))

    def test_rule_flag_prints_one_row(self) -> None:
        self.cli("rule", "add", "a-rule", "--json", self.rule())
        self.cli("rule", "add", "b-rule", "--json", self.rule(action="warn"))
        self.assertEqual(self.status("--rule", "b-rule"), "- `b-rule` warn · global · enabled\n")
        code, _, err = self.cli("status", "--render", "--rule", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("no rule 'ghost'", err)
        code, _, err = self.cli("status", "--rule", "a-rule")
        self.assertEqual(code, 2)
        self.assertIn("--rule needs --render", err)

    def test_long_ids_are_capped(self) -> None:
        long = "r" * 50
        self.cli("rule", "add", long, "--json", self.rule())
        self.cli("rule", "add", "short", "--json", self.rule())
        out = self.status()
        self.assertIn(f"- `{long}` deny", out)
        self.assertIn("- `short" + " " * 35 + "` deny", out)

    def test_plain_status_honours_scope_and_problems(self) -> None:
        self.cli("rule", "add", "g-rule", "--json", self.rule())
        self.cli("rule", "add", "p-rule", "--json", self.rule(), "--project")
        out = self.cli("status", "--scope", "project")[1]
        self.assertIn("p-rule [project]", out)
        self.assertNotIn("g-rule", out)
        self.put(self.gpath, "{nope")
        out = self.cli("status", "--problems")[1]
        self.assertTrue(out.startswith("problems:\n  - unreadable global state file"))
        self.assertNotIn("rules:", out)

    def test_agent_can_render(self) -> None:
        code, _, _ = self.cli("status", "--render", agent=True)
        self.assertEqual(code, 0)


class InputSources(Isolated):
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
        self.assertIn("match  strings x", out)
        with mock.patch("sys.stdin", io.StringIO(self.RULE)):
            code, out, _ = self.cli("rule", "test", "--json", "-", "strings x")
        self.assertEqual(code, 0)
        self.assertIn("match  strings x", out)

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
        for verb in (("rule", "add", "x"), ("rule", "test", "--render", "ls")):
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
        code, _, err = self.cli("rule", "test", "--render", "--json", "-", "--examples", "-")
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
        with mock.patch("sys.stdin", io.StringIO('{"action": "warn"}')):
            self.assertEqual(self.cli("rule", "set", "r", "--json", "-", "retry=same-command")[0], 0)
        rule = self.get(self.gpath)["rules"]["r"]
        self.assertEqual((rule["action"], rule["retry"]), ("warn", "same-command"))

    def test_set_json_errors(self) -> None:
        self.cli("rule", "add", "r", "--json", self.RULE)
        for bad in ('[1]', '{"colour": "red"}', '{"message": 3}', '{"modes": [1]}'):
            with self.subTest(bad=bad):
                self.assertEqual(self.cli("rule", "set", "r", "--json", bad)[0], 2)
        self.assertEqual(self.cli("rule", "set", "r")[0], 2)
        self.assertEqual(self.get(self.gpath)["rules"]["r"]["message"], "Don't use strings.")


class Wrapped(unittest.TestCase):
    def test_parser_flags(self) -> None:
        from shellwords import simple_commands

        flags = {c.name: c.wrapped for c in simple_commands("sudo pkill x; ls | wc; echo $(cat f)")}
        self.assertEqual(flags, {"pkill": True, "ls": True, "wc": True, "echo": False, "cat": True})


if __name__ == "__main__":
    unittest.main()
