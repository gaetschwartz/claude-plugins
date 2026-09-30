from __future__ import annotations  # noqa: I001

import contextlib
import io
import json
import os
import re
import time
from collections.abc import Iterator
from typing import Any, ClassVar
from unittest import mock

from helpers import REAL_WANTED, AstIsolated

import astbin
import astcli
import astrun
import astworker
import engine
import matching
import policy
import wrappers

PG = "pg" + "rep"
KILL = "ki" + "ll"
PK = "pk" + "ill"
NEST = [{"kind": k} for k in ("command_substitution", "pipeline", "list", "while_statement", "if_statement",
                              "for_statement")]
BY_NAME: dict[str, Any] = {"any": [{"pattern": "pkill $$$"}, {"pattern": "killall $$$"}]}
NESTED: dict[str, Any] = {"pattern": f"{PG} $$$", "inside": {"any": NEST, "stopBy": "end"}}
XARGS_KILL: dict[str, Any] = {"pattern": f"xargs {KILL} $$$", "inside": {"kind": "pipeline"}}


def rule_of(ast: dict[str, Any], **extra: Any) -> policy.Rule:
    return policy.with_defaults({"match": {"ast": ast, **extra}, "message": "m"})


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
            return self.call({"op": "tree", "command": command})["units"][0]["broken"]

        for command in ('echo "unterminated', "a |", "echo $(", "if a; then b"):
            self.assertTrue(broken(command), command)
        for command in ("cat <<EOF\nEOF", "x=", "echo ''", "a && b", "f() { :; }", "echo $((1+2))", "cat <<EOF\n$(a)\nEOF"):
            self.assertFalse(broken(command), command)

    def test_a_broken_tree_still_yields_the_commands_it_could_read(self) -> None:
        got = self.kinds(BY_NAME, ['pkill x "unterminated', "a; pkill x; if b; then", "pkill x\necho 'unterminated"])
        self.assertTrue(all(got.values()), got)


class Worker(AstIsolated):
    """The CLI-backed worker: batching, offsets, sanitising and its bounds."""

    def verdict(self, command: str, ast: dict[str, Any] | None = None, **request: Any) -> Any:
        rules = {"r": rule_of(ast or {"pattern": f"{KILL} $$$"})}
        return self.call({"op": "eval", "command": command, "rules": rules, "wrappers": list(wrappers.DEFAULTS),
                          **request})["verdicts"]["r"]

    def test_one_unit_alone_and_the_same_unit_in_a_batch_give_the_same_hits(self) -> None:
        from astcli import Cli, Src

        engine_ = astbin.locate(str(self.data))
        cli = Cli(engine_.binary, time.monotonic() + 30, engine_.version)
        rules = {"r": {"rule": {"pattern": "echo $$$"}}}
        texts = ["echo a", "ls", "echo 'é' && echo \U0001F600 b", "sudo echo x | cat"]
        alone = [cli.scan(rules, [Src(t)])[0] for t in texts]
        self.assertEqual(alone, cli.scan(rules, [Src(t) for t in texts]))
        self.assertTrue(any(alone))

    def test_ignore_files_around_the_temp_directory_cannot_hide_a_unit(self) -> None:
        import tempfile

        scratch = self.tmp / "scratch"
        scratch.mkdir()
        (scratch / ".gitignore").write_text("*\n")
        (scratch / ".ignore").write_text("*\n")
        with mock.patch.object(tempfile, "tempdir", str(scratch)):
            self.assertEqual(self.verdict(f"bash -c '{KILL} x'"), "wrapped")
        self.assertEqual([p.name for p in scratch.iterdir() if p.name.startswith("guardrails-")], [])

    def test_offsets_survive_multibyte_text_before_the_region(self) -> None:
        for prefix in ("echo 'é é é'", "echo 日本語 \U0001F600", "x=é"):
            self.assertEqual(self.verdict(f"{prefix} && echo $({KILL} x)"), "wrapped", prefix)
            self.assertEqual(self.verdict(f"{prefix}; {KILL} x"), "direct", prefix)
            self.assertEqual(self.verdict(f"{prefix}; bash -c '{KILL} x'"), "wrapped", prefix)

    def test_rules_with_unusual_characters_reach_the_engine_intact(self) -> None:
        for text in ("echo \u00e9\U0001F600", "echo a\u2028b", "echo a\u0085b", "echo \"q\" 'r' \\s"):
            self.assertEqual(self.verdict(text, {"pattern": text}), "direct", text)
        self.assertEqual(self.verdict("echo a\u2028b", {"kind": "word", "regex": "a\u2028b"}), "direct")

    def test_nul_and_lone_surrogates_do_not_break_the_engine(self) -> None:
        self.assertEqual(self.verdict(f"echo a\x00b; {KILL} x"), "direct")
        self.assertEqual(self.verdict(f"echo \ud800; {KILL} x"), "direct")

    def limit(self, command: str) -> str | None:
        rules = {"r": rule_of({"pattern": f"{KILL} $$$"})}
        return self.call({"op": "eval", "command": command, "rules": rules, "wrappers": []})["limit"]

    def test_limits_name_their_real_cause(self) -> None:
        self.assertEqual(self.limit("eval " * 40 + KILL + " x"), "depth")
        self.assertEqual(self.limit("; ".join(f"bash -c 'echo {n}'" for n in range(80))), "units")
        self.assertEqual(self.limit("bash -c 'true'; " * 600), None)

    def test_the_same_input_gives_the_same_answer_however_slow_the_machine_is(self) -> None:
        from astcli import Cli

        commands = ["eval " * 40 + KILL + " x", "; ".join(f"bash -c 'echo {n}'" for n in range(80)), f"bash -c '{KILL} x'"]
        request = {"op": "eval", "rules": {"r": rule_of(BY_NAME)}, "wrappers": []}
        fast = [self.call({**request, "command": c}) for c in commands]
        real_scan = Cli.scan

        def slow(self_: Cli, *args: Any, **kwargs: Any) -> Any:
            time.sleep(0.1)
            return real_scan(self_, *args, **kwargs)

        with mock.patch.object(Cli, "scan", slow), mock.patch.object(astrun, "DEADLINE", 60.0):
            laggy = [self.call({**request, "command": c}) for c in commands]
        self.assertEqual(fast, laggy)

    def test_expansion_is_proportional_to_the_distinct_input(self) -> None:
        self.put(self.gpath, {"rules": {"by-name": {"match": {"ast": BY_NAME}, "message": "No."},
                                        "prog": {"match": {"program": PK}, "message": "No."}}})
        shapes = {"500 deep": "$(" * 500 + PK + " x" + ")" * 500, "wide list": "a;" * 3000 + PK + " x",
                  "wide substitutions": "echo $(ls); " * 1200 + PK + " x",
                  "wide wrappers": "sudo true; " * 1400 + PK + " x", "same shell string": "bash -c 'ls'; " * 2000 + PK + " x"}
        for n, (name, command) in enumerate(shapes.items()):
            started = time.monotonic()
            out = self.hook(command, session=f"s{n}")
            self.assertLess(time.monotonic() - started, 2.5, name)
            assert out is not None
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", name)

    def test_a_repeated_script_is_scanned_once(self) -> None:
        scans: list[int] = []
        real_scan = astcli.Cli.scan

        def spy(cli: astcli.Cli, rules: Any, sources: list[Any]) -> Any:
            scans.append(len(sources))
            return real_scan(cli, rules, sources)

        with mock.patch.object(astcli.Cli, "scan", spy):
            response = self.call({"op": "eval", "command": "; ".join([f"bash -c 'echo {KILL}'"] * 5000),
                                  "rules": {"r": rule_of(BY_NAME)}, "wrappers": []})
        self.assertEqual(response["verdicts"], {"r": None})
        self.assertEqual(scans, [1, 1])

    def test_tree_nodes_keep_the_leaf_rules(self) -> None:
        nodes = self.call({"op": "tree", "command": "echo \"\" 'a b' x"})["units"][0]["nodes"]
        shown = {(kind, text) for _, kind, text in nodes}
        self.assertIn(("string", None), shown)
        self.assertIn(("raw_string", "'a b'"), shown)
        self.assertIn(("command_name", "echo"), shown)

    def test_unquote_one_shell_word(self) -> None:
        for word, text in (("'a b'", "a b"), ('"a \\"b\\" \\$x \\\\"', 'a "b" $x \\'), ("pkill", "pkill"), ("''", ""),
                           ("'bash -c '\"'\"'x'\"'\"''", "bash -c 'x'"), ("a\\ b", "a b"), ('"x"\'y\'z', "xyz")):
            self.assertEqual(astworker.unquote(word), text, word)


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
        self.assertIn("match.ast rule huge is larger than 16 KiB and is skipped", first["systemMessage"])
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

    def test_the_worker_skips_an_oversized_rule_by_name_and_runs_the_rest(self) -> None:
        response = self.call({"op": "eval", "command": "echo $(ls)",
                              "rules": {"a": rule_of(self.HUGE), "b": rule_of({"kind": "command_substitution"})}})
        self.assertEqual(response["verdicts"], {"b": "direct"})
        self.assertEqual(list(response["errors"]), ["a"])
        self.assertIn("larger than 16 KiB", response["errors"]["a"])

    def test_many_rules_are_capped_in_order_and_never_put_on_the_command_line(self) -> None:
        rules = {f"r{i:04d}": rule_of({"pattern": f"tool{i} --flag-{'y' * 100} $$$"}) for i in range(2500)}
        rules["r9999"] = rule_of({"kind": "command_substitution"})
        seen: list[list[str]] = []
        real = astcli.subprocess.run

        def spy(cmd: list[str], **kwargs: Any) -> Any:
            seen.append(cmd)
            return real(cmd, **kwargs)

        with mock.patch.object(astcli.subprocess, "run", spy):
            response = self.call({"op": "eval", "command": "tool1 --flag-" + "y" * 100 + " a", "rules": rules})
        self.assertEqual(response["verdicts"]["r0001"], "direct")
        skipped = sorted(response["errors"])
        self.assertTrue(skipped and skipped[-1] == "r9999")
        self.assertEqual(skipped, sorted(set(skipped)))
        self.assertIn("together exceed 256 KiB", response["errors"]["r9999"])
        self.assertTrue(seen)
        self.assertTrue(all(sum(len(part) for part in cmd) < 2048 for cmd in seen))
        again = self.call({"op": "eval", "command": "tool1 --flag-" + "y" * 100 + " a", "rules": rules})
        self.assertEqual(sorted(again["errors"]), skipped)

    def test_a_rule_that_does_not_compile_is_skipped_by_name_beside_good_ones(self) -> None:
        response = self.call({"op": "eval", "command": "echo $(ls)",
                              "rules": {"bad": rule_of({"kind": "no_such_kind"}),
                                        "good": rule_of({"kind": "command_substitution"})}})
        self.assertEqual(response["verdicts"], {"good": "direct"})
        self.assertEqual(list(response["errors"]), ["bad"])




class Validation(AstIsolated):
    def test_static_validation(self) -> None:
        ok = {"pattern": "a $$$", "inside": {"kind": "pipeline", "stopBy": "end"}, "not": {"kind": "list"},
              "any": [{"kind": "command"}], "all": [{"regex": "x"}], "follows": {"kind": "command", "field": "name"},
              "precedes": {"kind": "word"}, "has": {"kind": "word", "stopBy": {"kind": "command"}}}
        policy.validate_rule({"match": {"ast": ok}, "message": "m"})
        policy.validate_rule({"match": {"ast": {"pattern": {"context": "a $$$", "selector": "command"}}},
                              "message": "m"})
        for ast in ({}, [], "x", {"pattern": ""}, {"pattern": 3}, {"bogus": 1}, {"kind": ""}, {"inside": "x"},
                    {"any": []}, {"any": [1]}, {"all": "x"}, {"stopBy": "sideways"}, {"nthChild": 1}, {"matches": "x"},
                    {"inside": {"range": {}}}, {"pattern": {"selector": "x"}}):
            with self.subTest(ast=ast), self.assertRaises(policy.Invalid):
                policy.validate_rule({"match": {"ast": ast}, "message": "m"})

    def test_ast_alone_is_a_matcher(self) -> None:
        policy.validate_rule({"match": {"ast": {"kind": "command"}}, "message": "m"})
        with self.assertRaises(policy.Invalid):
            policy.validate_rule({"match": {"args": "x"}, "message": "m"})


class Cli(AstIsolated):
    RULE: ClassVar[dict[str, Any]] = {"match": {"ast": {"pattern": "pkill $$$"}}, "message": "no pkill"}

    def test_rule_test_reports_real_verdicts_and_marks_wrapped(self) -> None:
        code, out, _ = self.cli("rule", "test", "--json", json.dumps(self.RULE), "pkill x", "sudo pkill x",
                                "echo 'pkill x'", "ls")
        self.assertEqual(code, 0)
        self.assertIn("match  pkill x", out)
        self.assertIn("match  sudo pkill x", out)
        self.assertIn("-      echo 'pkill x'", out)
        code, out, _ = self.cli("rule", "test", "--render", "--json", json.dumps(self.RULE), "pkill x",
                                "sudo pkill x", "a | pkill b", "echo pkill")
        rows = {line.split("`")[1].strip(): line for line in out.splitlines() if line.startswith("- ")}
        self.assertTrue(rows["sudo pkill x"].endswith("· wrapped"))
        self.assertTrue(rows["a | pkill b"].endswith("· wrapped"))
        self.assertFalse(rows["pkill x"].endswith("· wrapped"))
        self.assertIn('**Match** ast = `{"pattern":"pkill $$$"}`', out)
        self.assertIn("**Raw** `pkill $$$`", out)

    def test_raw_prefers_regex_and_joins_patterns(self) -> None:
        both = {"match": {"ast": {"any": [{"pattern": "a $$$"}, {"pattern": "b $$$"}]}}, "message": "m"}
        out = self.cli("rule", "test", "--render", "--json", json.dumps(both), "a x")[1]
        self.assertIn("**Raw** `a $$$ | b $$$`", out)
        with_regex = {"match": {**both["match"], "regex": "zzz"}, "message": "m"}
        out = self.cli("rule", "test", "--render", "--json", json.dumps(with_regex), "a x")[1]
        self.assertIn("**Raw** `zzz`", out)
        self.assertIn("ast = ", out)
        self.assertIn("regex = `zzz`", out)

    def test_a_bare_command_matches_a_trailing_hole_pattern(self) -> None:
        out = self.cli("rule", "test", "--json", json.dumps(self.RULE), "pkill", "/usr/bin/pkill", "pkill x")[1]
        self.assertIn("match  pkill\n", out)
        self.assertIn("match  /usr/bin/pkill\n", out)
        self.assertIn("match  pkill x\n", out)

    def test_compile_errors_exit_2_on_test_add_and_set(self) -> None:
        bad = json.dumps({"match": {"ast": {"kind": "nope"}}, "message": "m"})
        for argv in (("rule", "test", "--json", bad, "x"), ("rule", "add", "r", "--json", bad)):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("does not compile", err)
        self.assertFalse(self.gpath.exists())
        self.assertEqual(self.cli("rule", "add", "r", "--json", json.dumps(self.RULE))[0], 0)
        code, _, err = self.cli("rule", "set", "r", 'ast={"regex": "("}')
        self.assertEqual(code, 2)
        self.assertIn("does not compile", err)
        code, _, err = self.cli("rule", "set", "r", 'ast={"bogus": 1}')
        self.assertEqual(code, 2)
        self.assertIn("unsupported keys", err)
        self.assertEqual(self.cli("rule", "set", "r", "--json", '{"ast": {"pattern": "killall $$$"}}')[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["r"]["match"]["ast"], {"pattern": "killall $$$"})

    def test_installed_rule_can_be_tested_by_id(self) -> None:
        self.assertEqual(self.cli("rule", "add", "r", "--json", json.dumps(self.RULE))[0], 0)
        out = self.cli("rule", "test", "--id", "r", "bash -c 'pkill x'", "ls")[1]
        self.assertIn("match  bash -c 'pkill x'", out)
        self.assertIn("-      ls", out)

    def test_status_describes_and_checks_ast_rules(self) -> None:
        self.assertEqual(self.cli("rule", "add", "r", "--json", json.dumps(self.RULE))[0], 0)
        out = self.cli("status")[1]
        self.assertIn('ast={"pattern":"pkill $$$"}', out)
        self.assertNotIn("problems:", out)
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
        from unittest import mock

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
            got = {line[9:] for line in out.splitlines() if line.startswith("  match")}
            self.assertEqual(got, want)


class SessionStart(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.use_engine(False)
        self.popen = mock.patch.object(astrun.subprocess, "Popen").start()
        mock.patch.object(astbin, "wanted", REAL_WANTED).start()
        self.addCleanup(mock.patch.stopall)

    def warm_up(self) -> None:
        engine.run_warm(io.StringIO(json.dumps({"cwd": str(self.proj)})))

    def test_warms_only_when_an_enabled_rule_needs_the_parser(self) -> None:
        for rules in ({"s": {"match": {"regex": "strings"}, "message": "m"}},
                      {"a": {"match": {"ast": BY_NAME}, "message": "m", "enabled": False}}):
            self.put(self.gpath, {"rules": rules})
            self.warm_up()
        self.popen.assert_not_called()
        self.put(self.ppath, {"rules": {"b": {"match": {"ast": BY_NAME}, "message": "m"}}})
        self.warm_up()
        self.popen.assert_called_once()

    def test_the_detached_install_gets_a_scrubbed_environment_and_respects_the_backoff(self) -> None:
        self.put(self.gpath, {"rules": {"b": {"match": {"ast": BY_NAME}, "message": "m"}}})
        hostile = {"UV_FIND_LINKS": "/evil", "PIP_INDEX_URL": "http://evil", "PYTHONPATH": "/evil",
                   "NODE_OPTIONS": "--require /evil", "HTTPS_PROXY": "http://proxy:1", "PATH": "/repo/bin"}
        with mock.patch.dict(os.environ, hostile):
            self.warm_up()
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0][-2:], ["warm-install", str(self.data)])
        self.assertEqual(kwargs["env"]["HTTPS_PROXY"], "http://proxy:1")
        self.assertEqual(kwargs["env"]["PATH"], "/usr/bin:/bin")
        for name in ("UV_FIND_LINKS", "PIP_INDEX_URL", "PYTHONPATH", "NODE_OPTIONS", "CLAUDE_PROJECT_DIR"):
            self.assertNotIn(name, kwargs["env"])
        self.assertTrue(kwargs["start_new_session"])
        astbin.record_failure(str(self.data), "offline")
        self.popen.reset_mock()
        self.warm_up()
        self.popen.assert_not_called()

    def test_a_usable_engine_or_an_unsupported_platform_never_warms(self) -> None:
        self.put(self.gpath, {"rules": {"b": {"match": {"ast": BY_NAME}, "message": "m"}}})
        with mock.patch.object(astbin, "detect", side_effect=astbin.Missing("unsupported platform")):
            self.warm_up()
        self.popen.assert_not_called()

    def test_hooks_json_registers_warm_up_and_a_roomy_timeout(self) -> None:
        from helpers import HOOKS

        hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
        pre = hooks["PreToolUse"][0]["hooks"][0]
        self.assertGreater(pre["timeout"], astrun.DEADLINE * 2)
        start = hooks["SessionStart"][0]["hooks"][0]
        self.assertTrue(start["command"].endswith("guardrails.sh\" warm"))
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash|Monitor")
        self.assertLessEqual(start["timeout"], 10)
