from __future__ import annotations  # noqa: I001

import contextlib
import json
import re
from collections.abc import Iterator
from typing import Any, ClassVar
from unittest import mock

from helpers import AstIsolated, caught, render

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


def rule_of(ast: dict[str, Any], **extra: Any) -> policy.Rule:
    return policy.Rule.from_json({"match": {"ast": ast, **extra}, "message": "m"})


class Kinds(AstIsolated):
    def kinds(self, ast: dict[str, Any], commands: list[str], **extra: Any) -> dict[str, str | None]:
        rule = rule_of(ast, **extra)
        return {c: matching.evaluate(c, {"r": rule}).kinds["r"] for c in commands}

    def test_pattern_matches_commands_and_not_data(self) -> None:
        got = self.kinds(BY_NAME, ["pkill node", "killall Finder", "echo pkill", "echo 'pkill x'", "man pkill",
                                   "cat <<EOF\npkill x\nEOF", "cat <<'EOF'\nkillall x\nEOF", "ls > pkill"])
        self.assertEqual({c for c, k in got.items() if k}, {"pkill node", "killall Finder"})

    def test_double_quoted_substitution_runs_single_quoted_text_does_not(self) -> None:
        got = self.kinds(NESTED, [f'echo "{KILL} $({PG} x)"', f"echo '{KILL} $({PG} x)'", f"{KILL} $({PG} x)",
                                  f"echo foo$({PG} x)", f"echo `{PG} x`", f"cat <<EOF\n$({PG} x)\nEOF",
                                  f"cat <<'EOF'\n$({PG} x)\nEOF"])
        self.assertEqual({c for c, k in got.items() if k},
                         {f'echo "{KILL} $({PG} x)"', f"{KILL} $({PG} x)", f"echo foo$({PG} x)", f"echo `{PG} x`",
                          f"cat <<EOF\n$({PG} x)\nEOF"})

    def test_context_relations(self) -> None:
        got = self.kinds(NESTED, [f"{PG} x", f"if {PG} -q x; then a; fi", f"{PG} x | head", f"a && {PG} x",
                                  f"while {PG} x; do :; done", f"for p in $({PG} x); do b; done", f"{PG} -f vite;"])
        self.assertEqual({c for c, k in got.items() if k},
                         {f"if {PG} -q x; then a; fi", f"{PG} x | head", f"a && {PG} x",
                          f"while {PG} x; do :; done", f"for p in $({PG} x); do b; done"})

    def test_xargs_kill_needs_a_pipeline(self) -> None:
        got = self.kinds(XARGS_KILL, [f"{PG} x | xargs {KILL} -9", f"xargs {KILL} -9 < pids", f"ps | xargs {KILL}"])
        self.assertEqual({c for c, k in got.items() if k}, {f"{PG} x | xargs {KILL} -9", f"ps | xargs {KILL}"})

    def test_command_patterns_are_also_tried_behind_wrappers_and_inside_shell_strings(self) -> None:
        got = self.kinds(BY_NAME, ["sudo pkill x", "env A=1 pkill x", "xargs pkill", "sudo -u bob killall x", "pkill x",
                                   "bash -c 'pkill x'", "eval pkill x", "sudo bash -c 'killall x'",
                                   "x | bash -c 'a | pkill z'", "echo pkill", "sudo echo x"])
        self.assertEqual({c: k for c, k in got.items() if k},
                         {"sudo pkill x": "wrapped", "env A=1 pkill x": "wrapped", "xargs pkill": "wrapped",
                          "sudo -u bob killall x": "wrapped", "pkill x": "direct", "bash -c 'pkill x'": "wrapped",
                          "eval pkill x": "wrapped", "sudo bash -c 'killall x'": "wrapped",
                          "x | bash -c 'a | pkill z'": "wrapped"})

    def test_a_kind_command_rule_with_a_name_also_gets_the_wrapper_branch(self) -> None:
        named = {"kind": "command", "has": {"field": "name", "regex": "^pkill$"}}
        got = self.kinds(named, ["pkill x", "FOO=1 pkill x", "sudo -u a pkill x", "bash -c 'pkill x'", "echo pkill"])
        self.assertEqual({c: k for c, k in got.items() if k},
                         {"pkill x": "direct", "FOO=1 pkill x": "direct", "sudo -u a pkill x": "wrapped",
                          "bash -c 'pkill x'": "wrapped"})

    def test_relations_stay_on_the_real_tree_behind_a_wrapper(self) -> None:
        rule = {"pattern": f"{PG} $$$", "inside": {"kind": "command_substitution", "stopBy": "end"}}
        got = self.kinds(rule, [f"echo $(sudo {PG} x)", f"sudo {PG} x", f"sudo echo $({PG} x)"])
        self.assertEqual({c for c, k in got.items() if k}, {f"echo $(sudo {PG} x)", f"sudo echo $({PG} x)"})

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

    def test_invalid_rules_are_reported_per_rule(self) -> None:
        ev = matching.evaluate("pkill x", {"good": rule_of(BY_NAME), "bad": rule_of({"kind": "no_such_kind"})})
        self.assertEqual(ev.kinds["good"], "direct")
        self.assertIn("bad", ev.invalid)
        self.assertEqual([k for k, _ in ev.warnings()], ["rule-invalid:bad"])

    def test_a_broken_tree_is_reported_not_hidden(self) -> None:
        def broken(command: str) -> bool:
            return matching.tree(command)[0][0].broken

        for command in ('echo "unterminated', "a |", "echo $(", "if a; then b"):
            self.assertTrue(broken(command), command)
        for command in ("cat <<EOF\nEOF", "x=", "echo ''", "a && b", "f() { :; }", "echo $((1+2))", "cat <<EOF\n$(a)\nEOF"):
            self.assertFalse(broken(command), command)

    def test_a_broken_tree_still_yields_the_commands_it_could_read(self) -> None:
        got = self.kinds(BY_NAME, ['pkill x "unterminated', "a; pkill x; if b; then", "pkill x\necho 'unterminated"])
        self.assertTrue(all(got.values()), got)


class RuleSize(AstIsolated):
    """No rule, however large or numerous, can push the others off the engine."""

    HUGE: ClassVar[dict[str, Any]] = {"kind": "command", "regex": "x" * 600000}
    DENY_SUBST: ClassVar[dict[str, Any]] = {"match": {"ast": {"kind": "command_substitution"}},
                                            "message": "No substitutions."}

    def test_one_oversized_project_rule_cannot_disable_the_others(self) -> None:
        self.put(self.gpath, {"rules": {"subst": self.DENY_SUBST}})
        self.put(self.ppath, {"rules": {"huge": {"match": {"ast": self.HUGE}, "message": "m", "action": "warn"}}})
        first = self.hook("echo $(ls)", session="a")
        assert first is not None
        self.assertEqual(first["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("rule huge is invalid ('match.ast' is", first["systemMessage"])
        self.assertNotIn("Argument list too long", json.dumps(first))
        again = self.hook("echo $(ls)", session="a")
        assert again is not None
        self.assertEqual(again["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("systemMessage", again)

    def test_the_cli_refuses_an_oversized_rule_with_exit_2(self) -> None:
        rule = json.dumps({"match": {"ast": self.HUGE}, "message": "m"})
        for argv in (("rule", "add", "r", "--json", rule), ("rule", "test", "--json", rule, "x")):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("over the 16 KiB limit", err)
        self.assertFalse(self.gpath.exists())
        self.put(self.ppath, {"rules": {"huge": {"match": {"ast": self.HUGE}, "message": "m"}}})
        self.assertIn("rule huge: 'match.ast' is", self.cli("status", "--problems")[1])

    def test_a_rule_that_does_not_compile_is_skipped_by_name_beside_good_ones(self) -> None:
        ev = matching.evaluate("echo $(ls)", {"bad": rule_of({"kind": "no_such_kind"}),
                                             "good": rule_of({"kind": "command_substitution"})})
        self.assertEqual(ev.kinds, {"bad": None, "good": "direct"})
        self.assertEqual(list(ev.invalid), ["bad"])




BAD_ASTS: dict[str, dict[str, Any]] = {
    "an unknown key": {"bogus": 1}, "a wrong type": {"kind": 5}, "an empty relation": {"inside": "x"},
    "a bad stopBy": {"inside": {"kind": "command"}, "stopBy": "sideways"}, "a bad pattern": {"pattern": {"selector": "x"}},
    "no matcher": {"stopBy": "end"}, "an unknown kind": {"kind": "no_such_kind"}, "a bad list": {"any": [1]},
}


class Validation(AstIsolated):
    """ast-grep itself judges a match.ast rule: a bad one is refused when added and skipped when loaded."""

    def test_a_bad_ast_rule_is_refused_at_add_time_with_ast_greps_message(self) -> None:
        for name, ast in BAD_ASTS.items():
            rule = json.dumps({"match": {"ast": ast}, "message": "m"})
            for argv in (("rule", "add", "r", "--json", rule), ("rule", "test", "--json", rule, "x")):
                with self.subTest(name, verb=argv[1]):
                    code, _, err = self.cli(*argv)
                    self.assertEqual(code, 2)
                    self.assertIn("the rule does not compile: ", err)
                    self.assertGreater(len(err.split("compile: ", 1)[1].strip()), 10)
        self.assertFalse(self.gpath.exists())

    def test_a_bad_ast_rule_in_state_is_skipped_with_a_warning_and_the_others_run(self) -> None:
        for n, (name, ast) in enumerate(BAD_ASTS.items()):
            self.put(self.gpath, {"rules": {"bad": {"match": {"ast": ast}, "message": "m"},
                                            "good": {"match": {"program": "pkill"}, "message": "No."}}})
            with self.subTest(name):
                out = self.hook("pkill x", session=f"v{n}")
                assert out is not None
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("rule bad does not compile", out["systemMessage"])

    def test_the_shape_of_match_ast_and_its_size_are_checked_before_ast_grep_sees_it(self) -> None:
        for ast in ({}, [], "x", {"kind": "command", "regex": "x" * 20000}):
            with self.subTest(ast=str(ast)[:20]), self.assertRaises(policy.Invalid):
                policy.Rule.from_json({"match": {"ast": ast}, "message": "m"})
        policy.Rule.from_json({"match": {"ast": {"pattern": "a $$$", "inside": {"kind": "pipeline"}}}, "message": "m"})

    def test_ast_alone_is_a_matcher(self) -> None:
        policy.Rule.from_json({"match": {"ast": {"kind": "command"}}, "message": "m"})
        with self.assertRaises(policy.Invalid):
            policy.Rule.from_json({"match": {"args": "x"}, "message": "m"})


class Cli(AstIsolated):
    RULE: ClassVar[dict[str, Any]] = {"match": {"ast": {"pattern": "pkill $$$"}}, "message": "no pkill"}

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
        self.assertIn('**Match** ast = `{"pattern":"pkill $$$"}`', out)
        self.assertIn("**Raw** `pkill $$$`", out)

    def test_raw_prefers_regex_and_joins_patterns(self) -> None:
        both = {"match": {"ast": {"any": [{"pattern": "a $$$"}, {"pattern": "b $$$"}]}}, "message": "m"}
        out = self.cli("rule", "test", "--json", json.dumps(both), "a x")[1]
        self.assertIn("**Raw** `a $$$ | b $$$`", out)
        with_regex = {"match": {**both["match"], "regex": "zzz"}, "message": "m"}
        out = self.cli("rule", "test", "--json", json.dumps(with_regex), "a x")[1]
        self.assertIn("**Raw** `zzz`", out)
        self.assertIn("ast = ", out)
        self.assertIn("regex = `zzz`", out)

    def test_a_bare_command_matches_a_trailing_hole_pattern(self) -> None:
        out = self.cli("rule", "test", "--json", json.dumps(self.RULE), "pkill", "/usr/bin/pkill", "pkill x")[1]
        self.assertEqual(caught(out), {"pkill": True, "/usr/bin/pkill": True, "pkill x": True})

    def test_compile_errors_exit_2_on_test_add_and_set(self) -> None:
        bad = json.dumps({"match": {"ast": {"kind": "nope"}}, "message": "m"})
        for argv in (("rule", "test", "--json", bad, "x"), ("rule", "add", "r", "--json", bad)):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("does not compile", err)
        self.assertFalse(self.gpath.exists())
        self.assertEqual(self.cli("rule", "add", "r", "--json", json.dumps(self.RULE))[0], 0)
        code, _, err = self.cli("rule", "set", "r", "--json", '{"ast": {"regex": "("}}')
        self.assertEqual(code, 2)
        self.assertIn("does not compile", err)
        code, _, err = self.cli("rule", "set", "r", "--json", '{"ast": {"bogus": 1}}')
        self.assertEqual(code, 2)
        self.assertIn("unknown field `bogus`", err)
        self.assertEqual(self.cli("rule", "set", "r", "--json", '{"ast": {"pattern": "killall $$$"}}')[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["r"]["match"]["ast"], {"pattern": "killall $$$"})

    def test_installed_rule_can_be_tested_by_id(self) -> None:
        self.assertEqual(self.cli("rule", "add", "r", "--json", json.dumps(self.RULE))[0], 0)
        out = self.cli("rule", "test", "--id", "r", "bash -c 'pkill x'", "ls")[1]
        self.assertEqual(caught(out), {"bash -c 'pkill x'": True, "ls": False})

    def test_status_describes_and_checks_ast_rules(self) -> None:
        self.assertEqual(self.cli("rule", "add", "r", "--json", json.dumps(self.RULE))[0], 0)
        out = self.cli("status")[1]
        self.assertIn("`r` deny · global · enabled", out)
        self.assertNotIn("**Problems**", out)
        state = self.get(self.gpath)
        state["rules"]["r"]["match"]["ast"] = {"kind": "nope"}
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
            "by-name": {"match": {"ast": BY_NAME}, "message": "No kill by name."},
            "nested": {"match": {"ast": NESTED}, "message": "No nested search.", "action": "warn"},
        }})

    def test_denies_in_contexts_and_shell_strings(self) -> None:
        for n, command in enumerate(["pkill x", "bash -c 'killall x'", f"echo ok; echo $(pkill < {PG})"]):
            out = self.hook(command, session=f"s{n}")
            assert out is not None, command
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", command)
            self.assertIn("[guardrails:by-name]", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_heredoc_and_quotes_are_not_false_positives(self) -> None:
        for n, command in enumerate(["cat <<'EOF' > notes.md\npkill x\nEOF", "echo 'use pkill here'", "man pkill"]):
            self.assertIsNone(self.hook(command, session=f"q{n}"), command)

    def test_warn_rule_with_context(self) -> None:
        out = self.hook(f"{KILL} $({PG} x)")
        assert out is not None
        self.assertIn("No nested search.", out["hookSpecificOutput"]["additionalContext"])

    def test_invalid_rule_is_skipped_with_one_warning_per_session(self) -> None:
        self.put(self.gpath, {"rules": {"bad": {"match": {"ast": {"kind": "nope"}}, "message": "m"},
                                        "by-name": {"match": {"ast": BY_NAME}, "message": "No."}}})
        out = self.hook("pkill x")
        assert out is not None
        self.assertIn("deny", json.dumps(out))
        self.assertIn("rule bad does not compile", out["systemMessage"])
        again = self.hook("pkill y")
        assert again is not None
        self.assertNotIn("systemMessage", again)

    def test_user_and_project_wrapper_names_reach_the_hook(self) -> None:
        self.put(self.gpath, {"rules": {"p": {"match": {"program": PK}, "message": "No."}}})
        self.assertIsNone(self.hook(f"mywrap -x 1 {PK} a"))
        self.assertEqual(self.cli("wrapper", "add", "mywrap")[0], 0)
        out = self.hook(f"mywrap -x 1 {PK} a", session="s9")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.put(self.ppath, {"wrappers": {"projwrap": {}}})
        out = self.hook(f"projwrap {PK} a", session="p1")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_matching_runs_once_and_outside_the_state_lock(self) -> None:
        
        import store

        inside_lock: list[bool] = []
        held = [False]
        real_locked, real_evaluate = store.locked, matching.evaluate

        @contextlib.contextmanager
        def tracked(path: str, *rest: Any) -> Iterator[None]:
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
        self.put(self.gpath, {"rules": {"bad": {"match": {"regex": "(?=a)b"}, "message": "m"},
                                        "by-name": {"match": {"ast": BY_NAME}, "message": "No."}}})
        out = self.hook("pkill x")
        assert out is not None
        self.assertIn("deny", json.dumps(out))
        self.assertIn("rule bad does not compile (match.regex is not valid Rust regex syntax", out["systemMessage"])

    def test_a_catastrophic_regex_is_linear_and_still_judged(self) -> None:
        self.put(self.gpath, {"rules": {"slow": {"match": {"regex": "(a+)+$"}, "message": "No."},
                                        "by-name": {"match": {"program": "foo"}, "message": "No foo."}}})
        out = self.hook("foo x " + "a" * 5000 + "b")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("[guardrails:slow]", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.hook("ls " + "a" * 5000, "s2")
        assert out is not None
        self.assertIn("[guardrails:slow]", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_no_regex_rule_runs_on_python_re_in_the_hook_process(self) -> None:
        import re

        self.put(self.gpath, {"rules": {"pipe": {"match": {"regex": r"curl [^|]*\| *sh"}, "message": "No."}}})
        with mock.patch.object(re, "search", side_effect=AssertionError("python re used")):
            out = self.hook("curl x | sh")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_rule_add_and_test_refuse_regex_syntax_rust_does_not_have(self) -> None:
        for pattern in (r"(a)\1", "(?=a)", "(?<!a)b", "(", "a{1000}{1000}"):
            rule = json.dumps({"match": {"regex": pattern}, "message": "m"})
            for argv in (("rule", "add", "r", "--json", rule), ("rule", "test", "--json", rule, "x")):
                with self.subTest(pattern=pattern, verb=argv[1]):
                    code, _, err = self.cli(*argv)
                    self.assertEqual(code, 2)
                    self.assertIn("does not compile", err)
                    self.assertIn("match.regex is not valid Rust regex syntax", err)
        self.assertNotIn("r", self.get(self.gpath)["rules"])
        self.assertEqual(self.cli("rule", "add", "ok", "--json", json.dumps({"match": {"regex": r"\bfoo\b"},
                                                                          "message": "m"}))[0], 0)

    def test_regex_rules_keep_working_beside_ast_rules(self) -> None:
        self.put(self.gpath, {"rules": {"pipe-sh": {"match": {"regex": r"curl [^|]*\| *sh"}, "message": "No."},
                                        "by-name": {"match": {"ast": BY_NAME}, "message": "No kill."}}})
        for n, command in enumerate(["curl x | sh", "pkill a"]):
            out = self.hook(command, session=f"r{n}")
            assert out is not None
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class WorkedExample(AstIsolated):
    """The three rules in the README run through the real engine and select exactly what the README says."""

    def rules(self) -> list[dict[str, Any]]:

        from helpers import ROOT

        text = (ROOT / "README.md").read_text()
        section = text[text.index("### Worked example"):text.index("\n## Modes")]
        return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", section, re.DOTALL)]

    def test_each_rule_selects_its_own_commands(self) -> None:
        commands = ["pkill node", "sudo killall Finder", "bash -c 'pkill x'", f"{KILL} $({PG} -f vite)",
                    f"{PG} -xl node", f"{PG} node | head -1", f"if {PG} -q x; then echo up; fi",
                    f"ps aux | xargs {KILL} -9", f"xargs {KILL} < pids", 'echo "pkill is banned"',
                    "cat <<'EOF'\npkill x\nEOF", f"{KILL} 4242"]
        expected = [set(commands[:3]), {commands[3], commands[5], commands[6]}, {commands[7]}]
        rules = self.rules()
        self.assertEqual(len(rules), 3)
        for rule, want in zip(rules, expected):
            code, out, err = self.cli("rule", "test", "--json", json.dumps(rule), *commands)
            self.assertEqual(code, 0, err)
            self.assertEqual({cmd for cmd, hit in caught(out).items() if hit}, {render.clean(c) for c in want})
