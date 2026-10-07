from __future__ import annotations  # noqa: I001

import contextlib
import json
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from helpers import AstIsolated, caught, plain

import matching
import policy

PG = "pg" + "rep"
KILL = "ki" + "ll"
PK = "pk" + "ill"
NEST = [{"kind": k} for k in ("command_substitution", "pipeline", "list", "while_statement", "if_statement",
                              "for_statement")]
BY_NAME: dict[str, Any] = {"any": [{"pattern": "pkill $$$"}, {"pattern": "killall $$$"}]}
NESTED: dict[str, Any] = {"pattern": f"{PG} $$$", "inside": {"any": NEST, "stopBy": "end"}}
XARGS_KILL: dict[str, Any] = {"pattern": f"xargs {KILL} $$$", "inside": {"kind": "pipeline"}}


def rule_of(match: dict[str, Any]) -> policy.Rule:
    return policy.Rule.from_json({"match": match, "message": "m"})


class Kinds(AstIsolated):
    def kinds(self, match: dict[str, Any], commands: list[str]) -> dict[str, str | None]:
        rule = rule_of(match)
        return {c: matching.evaluate(c, {"r": rule}).kinds["r"] for c in commands}

    def test_command_patterns_are_also_tried_behind_wrappers_and_inside_shell_strings(self) -> None:
        got = self.kinds(BY_NAME, ["sudo pkill x", "env A=1 pkill x", "xargs pkill", "sudo -u bob killall x", "pkill x",
                                   "bash -c 'pkill x'", "eval pkill x", "sudo bash -c 'killall x'",
                                   "x | bash -c 'a | pkill z'", "echo pkill", "sudo echo x"])
        self.assertEqual({c: k for c, k in got.items() if k},
                         {"sudo pkill x": "wrapped", "env A=1 pkill x": "wrapped", "xargs pkill": "wrapped",
                          "sudo -u bob killall x": "wrapped", "pkill x": "direct", "bash -c 'pkill x'": "wrapped",
                          "eval pkill x": "wrapped", "sudo bash -c 'killall x'": "wrapped",
                          "x | bash -c 'a | pkill z'": "wrapped"})

    def test_a_command_pattern_accepts_any_spelling_of_the_name(self) -> None:
        got = self.kinds({"pattern": "pkill -9 $$$"}, ["pkill -9 x", "/usr/bin/pkill -9 x", "'pkill' -9 x",
                                                       "echo pkill -9 x", "pkill -8 x", "xpkill -9 x", "FOO=1 pkill -9 x",
                                                       "A=1 B=2 C=3 pkill -9 x", "A=1 B=2 C=3 D=4 pkill -9 x",
                                                       "A=1 echo pkill -9 x"])
        self.assertEqual({c for c, k in got.items() if k}, {"pkill -9 x", "/usr/bin/pkill -9 x", "'pkill' -9 x",
                                                            "FOO=1 pkill -9 x", "A=1 B=2 C=3 pkill -9 x"})

    def test_hole_inside_a_substitution_pattern_never_matches(self) -> None:
        got = self.kinds({"pattern": f"{KILL} $($$$)"}, [f"{KILL} $({PG} x)"])
        self.assertEqual(set(got.values()), {None})

    def test_trailing_hole_also_selects_the_bare_command(self) -> None:
        got = self.kinds({"pattern": "pkill $$$"}, ["pkill", "pkill x", "a | pkill", "echo $(pkill)"])
        self.assertTrue(all(got.values()), got)
        self.assertIsNone(self.kinds({"pattern": "pkill $$$"}, ["pkills x", "echo pkill", "xpkill"])["pkills x"])
        inside = self.kinds({"pattern": f"xargs {KILL} $$$", "inside": {"kind": "pipeline", "stopBy": "end"}},
                            [f"ps | xargs {KILL}", f"xargs {KILL}"])
        self.assertEqual({c for c, k in inside.items() if k}, {f"ps | xargs {KILL}"})

    def test_trailing_hole_in_a_pipeline_pattern_does_not_widen_to_its_first_command(self) -> None:
        got = self.kinds({"pattern": "curl $$$ | sh $$$"},
                         ["curl -O https://x.tgz", "curl x | sh", "curl x | sh -x", "curl x | bash", "ls | sh -x"])
        self.assertEqual({c for c, k in got.items() if k}, {"curl x | sh", "curl x | sh -x"})

    def test_a_broken_tree_still_yields_the_commands_it_could_read(self) -> None:
        got = self.kinds(BY_NAME, ['pkill x "unterminated', "a; pkill x; if b; then", "pkill x\necho 'unterminated"])
        self.assertTrue(all(got.values()), got)


class RuleSize(AstIsolated):
    """No rule, however large or numerous, can push the others off the engine."""

    HUGE: ClassVar[dict[str, Any]] = {"kind": "command", "regex": "x" * 600000}
    DENY_SUBST: ClassVar[dict[str, Any]] = {"match": {"kind": "command_substitution"},
                                            "message": "No substitutions."}

    def test_one_oversized_project_rule_cannot_disable_the_others(self) -> None:
        self.put(self.gpath, {"rules": {"subst": self.DENY_SUBST}})
        self.put(self.ppath, {"rules": {"huge": {"match": self.HUGE, "message": "m", "action": "warn"}}})
        first = self.hook("echo $(ls)", session="a")
        assert first is not None
        self.assertEqual(first["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("rule huge is invalid ('match' is", first["systemMessage"])
        self.assertNotIn("Argument list too long", json.dumps(first))
        again = self.hook("echo $(ls)", session="a")
        assert again is not None
        self.assertEqual(again["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("systemMessage", again)

    def test_the_cli_refuses_an_oversized_rule_with_exit_2(self) -> None:
        rule = json.dumps({"match": self.HUGE, "message": "m"})
        for argv in (("rule", "add", "r", "--json", rule), ("rule", "test", "--json", rule, "x")):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("over the 16 KiB limit", err)
        self.assertFalse(self.gpath.exists())
        self.put(self.ppath, {"rules": {"huge": {"match": self.HUGE, "message": "m"}}})
        self.assertIn("rule huge: 'match' is", self.cli("status", "--problems")[1])

BAD_ASTS: dict[str, dict[str, Any]] = {
    "a wrong type": {"kind": 5}, "an empty relation": {"inside": "x"},
    "a bad stopBy": {"inside": {"kind": "command"}, "stopBy": "sideways"}, "a bad pattern": {"pattern": {"selector": "x"}},
    "no matcher": {"stopBy": "end"}, "an unknown kind": {"kind": "no_such_kind"}, "a bad list": {"any": [1]},
}


class Validation(AstIsolated):
    """ast-grep itself judges a match: a bad one is refused when added and skipped when loaded."""

    def test_a_bad_ast_rule_is_refused_at_add_time_with_ast_greps_message(self) -> None:
        for name, ast in BAD_ASTS.items():
            rule = json.dumps({"match": ast, "message": "m"})
            for argv in (("rule", "add", "r", "--json", rule), ("rule", "test", "--json", rule, "x")):
                with self.subTest(name, verb=argv[1]):
                    code, _, err = self.cli(*argv)
                    self.assertEqual(code, 2)
                    self.assertIn("the rule does not compile: ", err)
                    self.assertGreater(len(err.split("compile: ", 1)[1].strip()), 10)
        self.assertFalse(self.gpath.exists())

    def test_a_bad_ast_rule_in_state_is_skipped_with_a_warning_and_the_others_run(self) -> None:
        for n, (name, ast) in enumerate(BAD_ASTS.items()):
            self.put(self.gpath, {"rules": {"bad": {"match": ast, "message": "m"},
                                            "good": {"match": {"command": "pkill"}, "message": "No."}}})
            with self.subTest(name):
                out = self.hook("pkill x", session=f"v{n}")
                assert out is not None
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("rule bad does not compile", out["systemMessage"])

    def test_the_shape_of_match_its_keys_and_its_size_are_checked_before_ast_grep_sees_it(self) -> None:
        for match in ({}, [], "x", {"kind": "command", "regex": "x" * 20000}, {"bogus": 1},
                      {"any": [{"has": {"kind": "word", "flavour": 1}}]}):
            with self.subTest(match=str(match)[:20]), self.assertRaises(policy.Invalid):
                policy.Rule.from_json({"match": match, "message": "m"})
        policy.Rule.from_json({"match": {"pattern": "a $$$", "inside": {"kind": "pipeline"}}, "message": "m"})


class Cli(AstIsolated):
    RULE: ClassVar[dict[str, Any]] = {"match": {"pattern": "pkill $$$"}, "message": "no pkill"}

    def test_rule_test_reports_real_verdicts_and_marks_wrapped(self) -> None:
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(self.RULE), "pkill x", "sudo pkill x",
                                "echo 'pkill x'", "ls")
        self.assertEqual(code, 0)
        self.assertEqual(caught(out), {"pkill x": True, "sudo pkill x": True, "echo 'pkill x'": False, "ls": False})
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(self.RULE), "pkill x",
                                "sudo pkill x", "a | pkill b", "echo pkill")
        rows = {line.split("`")[1].strip(): line for line in out.splitlines() if line.startswith("- ")}
        self.assertTrue(rows["sudo pkill x"].endswith("· wrapped"))
        self.assertTrue(rows["a | pkill b"].endswith("· wrapped"))
        self.assertFalse(rows["pkill x"].endswith("· wrapped"))
        self.assertIn('**Match** `{"pattern":"pkill $$$"}`', out)
        self.assertIn("**Raw** `pkill $$$`", out)

    def test_raw_joins_patterns_and_else_shows_the_regexes(self) -> None:
        both = {"match": {"any": [{"pattern": "a $$$"}, {"pattern": "b $$$"}]}, "message": "m"}
        out = self.cli("rule", "test", "--json", json.dumps(both), "a x")[1]
        self.assertIn("**Raw** `a $$$ | b $$$`", out)
        for match, raw in (({"kind": "program", "regex": "zzz"}, "zzz"), ({"command": "kill", "args": "-9"}, "-9")):
            out = self.cli("rule", "test", "--json", json.dumps({"match": match, "message": "m"}), "a x")[1]
            self.assertIn(f"**Raw** `{raw}`", out)
            self.assertIn(f"**Match** `{json.dumps(match, separators=(',', ':'))}`", out)

    def test_a_set_that_does_not_compile_exits_2_and_changes_nothing(self) -> None:
        self.assertEqual(self.cli("rule", "add", "r", "--json", json.dumps(self.RULE))[0], 0)
        code, _, err = self.cli("rule", "set", "r", "--json", '{"match": {"kind": "program", "regex": "(", "kind": "word"}}')
        self.assertEqual(code, 2)
        self.assertIn("'match.regex' is not a valid regex", err)
        code, _, err = self.cli("rule", "set", "r", "--json", '{"match": {"bogus": 1}}')
        self.assertEqual(code, 2)
        self.assertIn("unknown field match.bogus", err)
        self.assertEqual(self.cli("rule", "set", "r", "--json", '{"match": {"pattern": "killall $$$"}}')[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["r"]["match"], {"pattern": "killall $$$"})

    def test_status_describes_and_checks_ast_rules(self) -> None:
        self.assertEqual(self.cli("rule", "add", "r", "--json", json.dumps(self.RULE))[0], 0)
        out = self.cli("status")[1]
        self.assertIn("`r` deny · global · enabled", out)
        self.assertNotIn("**Problems**", out)
        state = self.get(self.gpath)
        state["rules"]["r"]["match"] = {"kind": "nope"}
        self.put(self.gpath, state)
        out = self.cli("status")[1]
        self.assertIn("rule r: does not compile", out)

    def test_rule_ast_prints_tree_and_shell_string_units(self) -> None:
        code, out, _ = self.cli("rule", "ast", "sudo -u bob pkill -f x | grep y")
        self.assertEqual(code, 0)
        for line in ("pipeline", "command_name «sudo»", "word «pkill»", "units: 1 (1 as written, 0 from shell strings)"):
            self.assertIn(line, out)
        out = self.cli("rule", "ast", "bash -c 'a; pkill x'")[1]
        self.assertIn("units: 2 (1 as written, 1 from shell strings)", out)
        self.assertIn("tree: shell string, source: a; pkill x", out)
        self.assertIn("raw_string «'a; pkill x'»", out)

    def test_rule_ast_output_is_sanitised(self) -> None:
        out = self.cli("rule", "ast", "echo 'a\nb'\x1b[31m done")[1]
        self.assertNotIn("\x1b", out)
        self.assertIn("⏎", out)
        self.assertIn("␛", out)
        self.assertEqual(self.cli("rule", "ast", "  ")[0], 2)

    def test_rule_ast_flags_a_broken_tree_and_a_clean_one_is_not_flagged(self) -> None:
        self.assertIn("ERROR or MISSING", self.cli("rule", "ast", "if a; then b")[1])
        self.assertNotIn("ERROR or MISSING", self.cli("rule", "ast", "cat <<EOF\n$(a)\nEOF")[1])


class Hook(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {
            "by-name": {"match": BY_NAME, "message": "No kill by name."},
            "nested": {"match": NESTED, "message": "No nested search.", "action": "warn"},
        }})

    def test_denies_in_contexts_and_shell_strings(self) -> None:
        for n, command in enumerate(["pkill x", "bash -c 'killall x'", f"echo ok; echo $(pkill < {PG})"]):
            out = self.hook(command, session=f"s{n}")
            assert out is not None, command
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", command)
            self.assertIn("[guardrails:by-name]", plain(out["hookSpecificOutput"]["permissionDecisionReason"]))

    def test_heredoc_and_quotes_are_not_false_positives(self) -> None:
        for n, command in enumerate(["cat <<'EOF' > notes.md\npkill x\nEOF", "echo 'use pkill here'", "man pkill"]):
            self.assertIsNone(self.hook(command, session=f"q{n}"), command)

    def test_warn_rule_with_context(self) -> None:
        out = self.hook(f"{KILL} $({PG} x)")
        assert out is not None
        self.assertIn("No nested search.", out["hookSpecificOutput"]["additionalContext"])

    def test_matching_runs_once_and_outside_the_state_lock(self) -> None:
        
        import store

        inside_lock: list[bool] = []
        held = [False]
        real_locked, real_evaluate = store.locked, matching.evaluate

        @contextlib.contextmanager
        def tracked(path: Path, *rest: Any) -> Generator[None]:
            with real_locked(path, *rest):
                held[0] = True
                try:
                    yield
                finally:
                    held[0] = False

        def spy(*args: Any, **kwargs: Any) -> matching.Evaluation:
            inside_lock.append(held[0])
            return real_evaluate(*args, **kwargs)

        with mock.patch.object(store, "locked", tracked), mock.patch.object(matching, "evaluate", spy):
            self.assertIsNotNone(self.hook("bash -c 'pkill x'"))
        self.assertEqual(inside_lock, [False])

    def test_a_regex_rule_that_rust_cannot_compile_is_skipped_and_named_beside_good_ones(self) -> None:
        self.put(self.gpath, {"rules": {"bad": {"match": {"kind": "program", "regex": "(?=a)b"}, "message": "m"},
                                        "by-name": {"match": BY_NAME, "message": "No."}}})
        out = self.hook("pkill x")
        assert out is not None
        self.assertIn("deny", json.dumps(out))
        self.assertIn("rule bad is invalid ('match.regex' is not a valid regex (error: look-around", out["systemMessage"])

    def test_a_catastrophic_regex_is_linear_and_still_judged(self) -> None:
        self.put(self.gpath, {"rules": {"slow": {"match": {"kind": "program", "regex": "(a+)+$"}, "message": "No."},
                                        "by-name": {"match": {"command": "foo"}, "message": "No foo."}}})
        out = self.hook("foo x " + "a" * 5000 + "b")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("[guardrails:slow]", plain(out["hookSpecificOutput"]["permissionDecisionReason"]))
        out = self.hook("ls " + "a" * 5000, "s2")
        assert out is not None
        self.assertIn("[guardrails:slow]", plain(out["hookSpecificOutput"]["permissionDecisionReason"]))

    def test_no_regex_rule_runs_on_python_re_in_the_hook_process(self) -> None:

        self.put(self.gpath, {"rules": {"pipe": {"match": {"kind": "program", "regex": r"curl [^|]*\| *sh"}, "message": "No."}}})
        with mock.patch.object(re, "search", side_effect=AssertionError("python re used")):
            out = self.hook("curl x | sh")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_rule_add_and_test_refuse_regex_syntax_rust_does_not_have(self) -> None:
        for pattern in (r"(a)\1", "(?=a)", "(?<!a)b", "(", "a{1000}{1000}"):
            rule = json.dumps({"match": {"kind": "program", "regex": pattern}, "message": "m"})
            for argv in (("rule", "add", "r", "--json", rule), ("rule", "test", "--json", rule, "x")):
                with self.subTest(pattern=pattern, verb=argv[1]):
                    code, _, err = self.cli(*argv)
                    self.assertEqual(code, 2)
                    self.assertIn("'match.regex' is not a valid regex", err)
        self.assertNotIn("r", self.get(self.gpath)["rules"])
        self.assertEqual(self.cli("rule", "add", "ok", "--json", json.dumps({"match": {"kind": "program", "regex": r"\bfoo\b"},
                                                                          "message": "m"}))[0], 0)
