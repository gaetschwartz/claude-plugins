from __future__ import annotations  # noqa: I001

import contextlib
import json
import os
import stat
import unittest
from collections.abc import Iterator
from typing import Any, ClassVar

from helpers import AST_PIN, AstIsolated, Isolated, ast_mode

import astrun
import matching
import policy
import wrappers

PG = "pg" + "rep"
KILL = "ki" + "ll"
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
        got = self.kinds(XARGS_KILL, [f"{PG} x | xargs {KILL} -9", f"xargs {KILL} -9 < pids", f"ps | sudo xargs {KILL}"])
        self.assertEqual({c for c, k in got.items() if k}, {f"{PG} x | xargs {KILL} -9", f"ps | sudo xargs {KILL}"})

    def test_wrappers_are_looked_through(self) -> None:
        commands = ["sudo pkill -f vite", "sudo -u bob pkill x", "env A=1 B=2 pkill x", "env -i -u A pkill x",
                    "timeout 5 pkill x", "timeout -s KILL 5s pkill x", "nice -n 10 pkill x", "nohup pkill x",
                    "time pkill x", "command pkill x", "exec pkill x", "builtin pkill x", "stdbuf -oL pkill x",
                    "setsid pkill x", "ionice -c 3 pkill x", "xargs -0 -n1 pkill", "xargs -I {} pkill {}",
                    "watch -n 5 pkill x", "doas pkill x", "sudo -- pkill x", "/usr/bin/sudo /bin/pkill x",
                    "sudo env timeout 5 nice -n 1 pkill x", "FOO=1 sudo pkill x", "sudo pkill x > /dev/null 2>&1"]
        got = self.kinds(BY_NAME, commands)
        self.assertEqual({c for c, k in got.items() if not k}, set())
        self.assertEqual(set(got.values()), {"wrapped"})

    def test_shell_strings_are_code_even_in_single_quotes(self) -> None:
        commands = ["bash -c 'pkill x'", 'sh -c "a; pkill x"', "zsh -c 'echo; killall y'", "bash -lc 'pkill x'",
                    "bash -o pipefail -c 'pkill x'", "eval pkill x", "eval 'pkill x'", 'eval "a; pkill x"',
                    "script -c 'pkill x' out", "sudo bash -c 'pkill x'", "bash -c \"bash -c 'pkill x'\"",
                    "x | bash -c 'a | pkill z'", "bash -c 'FOO=1 pkill x'"]
        self.assertEqual({c for c, k in self.kinds(BY_NAME, commands).items() if not k}, set())

    def test_not_a_command_is_not_matched(self) -> None:
        commands = ["command -v pkill", "command -V killall", "sudo -l", "ssh host pkill x", "find . -exec pkill {} ;",
                    "bash script.sh", "bash <<EOF\npkill x\nEOF", "python -c 'pkill x'", "sudo", "echo pkill | cat",
                    "nice", "env A=1"]
        self.assertEqual({c for c, k in self.kinds(BY_NAME, commands).items() if k}, set())

    def test_wrapped_tag_matches_the_existing_semantics(self) -> None:
        wrapped = ["sudo pkill x", "bash -c 'pkill x'", "xargs pkill", "timeout 5 pkill x", "echo $(pkill x)",
                   "echo `pkill x`", "ps | pkill x", "pkill x | cat", "env A=1 pkill x", "sh -c 'a; pkill x'",
                   "cat <(pkill x)", "echo foo$(pkill x)"]
        direct = ["pkill x", "/usr/bin/pkill x", "FOO=1 pkill x", "a; pkill x", "a && pkill x", "(pkill x)",
                  "{ pkill x; }", "pkill x &", "a\npkill x", "if pkill x; then b; fi"]
        got = self.kinds({"kind": "command", "has": {"field": "name", "regex": "(^|/)pkill$"}}, wrapped + direct)
        self.assertEqual({c for c, k in got.items() if k == "wrapped"}, set(wrapped))
        self.assertEqual({c for c, k in got.items() if k == "direct"}, set(direct))

    def test_direct_hit_beats_a_wrapped_one(self) -> None:
        got = self.kinds({"kind": "command", "has": {"field": "name", "regex": "^pkill$"}}, ["sudo pkill a; pkill b"])
        self.assertEqual(got, {"sudo pkill a; pkill b": "direct"})

    def test_user_wrapper_entries_extend_the_table(self) -> None:
        command = "mywrap -x 3 --fast pkill a"
        self.assertIsNone(self.kinds(BY_NAME, [command])[command])
        table = wrappers.effective({"wrappers": {"mywrap": {"flagsWithValue": ["-x"]}}})
        rule = rule_of(BY_NAME)
        self.assertEqual(matching.evaluate(command, {"r": rule}, table).kinds["r"], "wrapped")

    def test_hole_inside_a_substitution_pattern_never_matches(self) -> None:
        got = self.kinds({"pattern": f"{KILL} $($$$)"}, [f"{KILL} $({PG} x)"])
        self.assertEqual(set(got.values()), {None})

    def test_trailing_hole_also_selects_the_bare_command(self) -> None:
        got = self.kinds({"pattern": "pkill $$$"}, ["pkill", "pkill x", "xargs pkill", "a | pkill", "FOO=1 pkill x",
                                                      "/usr/bin/pkill", "'pkill' x", "\\pkill x"])
        self.assertTrue(all(got.values()), got)
        self.assertIsNone(self.kinds({"pattern": "pkill $$$"}, ["pkills x", "echo pkill", "xpkill"])["pkills x"])
        inside = self.kinds({"pattern": f"xargs {KILL} $$$", "inside": {"kind": "pipeline", "stopBy": "end"}},
                            [f"ps | xargs {KILL}", f"xargs {KILL}"])
        self.assertEqual({c for c, k in inside.items() if k}, {f"ps | xargs {KILL}"})

    def test_prefixes_and_path_names_do_not_hide_a_command(self) -> None:
        got = self.kinds({"pattern": "pkill -9 $$$"}, ["FOO=1 BAR=2 pkill -9 x", "/usr/bin/pkill -9 x",
                                                         "sudo /bin/pkill -9 x", "env A=1 /usr/bin/pkill -9",
                                                         "bash -c 'FOO=1 /bin/pkill -9 x'", "FOO=$(a) pkill -9 x",
                                                         "FOO=1 sudo pkill -9 x", "./pkill -9 x"])
        self.assertTrue(all(got.values()), got)
        self.assertEqual(got["FOO=1 BAR=2 pkill -9 x"], "direct")
        self.assertEqual(got["/usr/bin/pkill -9 x"], "direct")
        self.assertEqual(got["sudo /bin/pkill -9 x"], "wrapped")

    def test_other_fields_are_alternatives(self) -> None:
        rule = policy.with_defaults({"match": {"program": "kill", "ast": BY_NAME, "regex": "zzz"}, "message": "m"})
        for command, expected in (("kill 1", True), ("pkill x", True), ("echo zzz", True), ("ls", False)):
            self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"] is not None, expected, command)

    def test_broken_tree_also_uses_the_lexer_commands(self) -> None:
        response = astrun.call({"op": "eval", "command": "if a; then b", "rules": {"r": BY_NAME},
                                     "lexed": [{"src": "pkill x", "wrapped": False}]})
        self.assertTrue(response["broken"])
        self.assertEqual(response["verdicts"], {"r": "direct"})
        clean = astrun.call({"op": "eval", "command": "a; b", "rules": {"r": BY_NAME},
                                  "lexed": [{"src": "pkill x", "wrapped": False}]})
        self.assertFalse(clean["broken"])
        self.assertEqual(clean["verdicts"], {"r": None})

    def test_unbalanced_quotes_count_as_broken(self) -> None:
        for command in ('echo "unterminated', "a |", "echo $(", "cat <<EOF\nx"):
            self.assertTrue(astrun.call({"op": "eval", "command": command, "rules": {}})["broken"], command)
        for command in ("cat <<EOF\nEOF", "x=", "echo ''", "a && b", "f() { :; }", "echo $((1+2))"):
            self.assertFalse(astrun.call({"op": "eval", "command": command, "rules": {}})["broken"], command)

    def test_wrapper_depth_is_limited_and_reported(self) -> None:
        command = "sudo " * 8 + "pkill x"
        response = astrun.call({"op": "eval", "command": command, "rules": {"r": BY_NAME},
                                     "wrappers": wrappers.effective()})
        self.assertTrue(response["broken"])

    def test_invalid_rules_are_reported_per_rule(self) -> None:
        good = rule_of(BY_NAME)
        bad = rule_of({"kind": "no_such_kind"})
        ev = matching.evaluate("pkill x", {"good": good, "bad": bad})
        self.assertEqual(ev.kinds["good"], "direct")
        self.assertIn("bad", ev.invalid)
        self.assertEqual([k for k, _ in ev.warnings()], ["ast-invalid:bad"])


class Validation(Isolated):
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

    def test_ast_pin_is_consistent(self) -> None:
        self.assertEqual(astrun.PIN, AST_PIN)
        self.assertIn(f"ast-grep-py=={AST_PIN}", astrun.uv_command("uv", True))


class PlainLexer(unittest.TestCase):
    def test_assignment_prefix_inside_a_shell_string_is_looked_through(self) -> None:
        from shellwords import simple_commands

        for command in ("bash -c 'FOO=1 pkill x'", "eval 'A=1 pkill x'", "eval A=1 pkill x", "watch -n 1 'A=1 pkill x'"):
            self.assertIn("pkill", [c.name for c in simple_commands(command)], command)

    def test_user_wrapper_flags_reach_the_plain_lexer(self) -> None:
        from shellwords import simple_commands

        table = wrappers.effective({"wrappers": {"mywrap": {"flagsWithValue": ["-x"]}}})
        self.assertEqual([c.name for c in simple_commands("mywrap -x 3 pkill a", 0, table)], ["pkill"])
        self.assertEqual([c.name for c in simple_commands("mywrap -x 3 pkill a")], ["mywrap"])


class WrapperTable(unittest.TestCase):
    def test_defaults_cover_the_required_wrappers(self) -> None:
        for name in ("sudo", "doas", "env", "timeout", "nice", "nohup", "time", "command", "exec", "builtin", "stdbuf",
                     "setsid", "ionice", "xargs", "watch", "bash", "sh", "zsh", "dash", "ksh", "eval"):
            self.assertIn(name, wrappers.DEFAULTS)
            wrappers.validate(name, wrappers.DEFAULTS[name])

    def test_validation(self) -> None:
        wrappers.validate("x", {"flagsWithValue": ["-a"], "shellString": "-c", "skip": 1, "assignments": True,
                                "noCommandFlags": ["-v"]})
        for entry in ([], {"bogus": 1}, {"flagsWithValue": ["a"]}, {"flagsWithValue": "-a"}, {"shellString": "c"},
                      {"skip": -1}, {"skip": True}, {"skip": 9}, {"assignments": "yes"}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                wrappers.validate("x", entry)
        for name in ("", "a/b", "a b", 3):
            with self.assertRaises(ValueError):
                wrappers.validate(name, {})

    def test_layers_only_add(self) -> None:
        managed = {"wrappers": {"sudo": {"flagsWithValue": ["-X"], "skip": 3}, "mine": {"shellString": "-e"}}}
        glob = {"wrappers": {"mine": {"shellString": "-z", "flagsWithValue": ["-q"]}, "sudo": {"skip": 2}}}
        table = wrappers.effective(managed, glob, {"wrappers": {"mine": {"flagsWithValue": ["-r"], "assignments": True}}})
        self.assertIn("-X", table["sudo"]["flagsWithValue"])
        self.assertIn("-u", table["sudo"]["flagsWithValue"])
        self.assertEqual(table["sudo"]["skip"], 3)
        self.assertEqual(table["mine"]["shellString"], "-e")
        self.assertEqual(table["mine"]["flagsWithValue"], ["-q", "-r"])
        self.assertTrue(table["mine"]["assignments"])
        self.assertEqual(table["timeout"]["skip"], 1)

    def test_builtin_scalars_cannot_be_altered(self) -> None:
        table = wrappers.effective({"wrappers": {"timeout": {"skip": 4}, "bash": {"shellString": "-x"}}})
        self.assertEqual(table["timeout"]["skip"], 1)
        self.assertEqual(table["bash"]["shellString"], "-c")

    def test_invalid_entries_are_skipped_and_reported(self) -> None:
        layer = {"wrappers": {"ok": {}, "bad": {"skip": "x"}}}
        self.assertEqual(set(wrappers.effective(layer)) - set(wrappers.DEFAULTS), {"ok"})
        self.assertEqual(len(wrappers.problems("global", layer)), 1)
        self.assertEqual(len(wrappers.problems("global", {"wrappers": []})), 1)

    def test_policy_layering_and_project_switch(self) -> None:
        managed, _ = policy.managed_layer([("/m", {"wrappers": {"m1": {"flagsWithValue": ["-a"]}}})])
        glob = {"wrappers": {"g1": {}}}
        project = {"wrappers": {"p1": {}}}
        names = set(policy.effective_wrappers(managed, glob, project)) - set(wrappers.DEFAULTS)
        self.assertEqual(names, {"m1", "g1", "p1"})
        off = {**project, "enabled": False}
        self.assertNotIn("p1", policy.effective_wrappers(managed, glob, off))


class WrapperCli(Isolated):
    def test_add_list_rm(self) -> None:
        code, out, _ = self.cli("wrapper", "add", "mywrap", "--json", '{"flagsWithValue": ["-x"]}')
        self.assertEqual(code, 0, out)
        self.assertIn("added wrapper mywrap", out)
        stored = self.get(self.gpath)["wrappers"]["mywrap"]
        self.assertEqual(stored["flagsWithValue"], ["-x"])
        self.assertEqual(stored["setBy"]["by"], "user")
        code, out, _ = self.cli("wrapper", "list")
        self.assertIn("mywrap [global]: flags-with-value -x", out)
        self.assertIn("sudo [builtin]", out)
        code, out, _ = self.cli("wrapper", "list", "--scope", "global")
        self.assertEqual(out.strip().splitlines(), [line for line in out.splitlines() if "mywrap" in line])
        self.assertEqual(self.cli("wrapper", "rm", "mywrap")[0], 0)
        self.assertNotIn("mywrap", self.get(self.gpath)["wrappers"])
        code, _, err = self.cli("wrapper", "rm", "mywrap")
        self.assertEqual(code, 2)
        self.assertIn("no wrapper 'mywrap'", err)
        code, _, err = self.cli("wrapper", "rm", "sudo")
        self.assertEqual(code, 2)
        self.assertIn("built-in", err)

    def test_invalid_entries_are_rejected(self) -> None:
        for raw in ('{"skip": "x"}', '{"bogus": 1}', "[]", "not json"):
            code, _, err = self.cli("wrapper", "add", "w", "--json", raw)
            self.assertEqual(code, 2, raw)
            self.assertIn("error:", err)
        self.assertFalse(self.gpath.exists())

    def test_scopes_and_path(self) -> None:
        self.assertEqual(self.cli("wrapper", "add", "pw", "--json", "{}", "--scope", "project")[0], 0)
        self.assertIn("pw", self.get(self.ppath)["wrappers"])
        extra = self.tmp / "extra.json"
        self.assertEqual(self.cli("wrapper", "add", "mw", "--json", "{}", "--scope", "managed", "--path",
                                  str(extra))[0], 0)
        self.assertEqual(stat.S_IMODE(extra.stat().st_mode), 0o644)
        code, out, _ = self.cli("wrapper", "list", "--path", str(extra))
        self.assertIn("mw [managed]", out)
        self.assertIn("pw [project]", out)
        code, _, err = self.cli("wrapper", "add", "x", "--json", "{}", "--path", str(extra))
        self.assertEqual(code, 2)
        self.assertIn("--scope managed", err)

    def test_agent_needs_as_user_and_managed_reports_not_writable(self) -> None:
        code, _, err = self.cli("wrapper", "add", "w", "--json", "{}", agent=True)
        self.assertEqual(code, 3)
        self.assertIn("--as-user", err)
        self.assertEqual(self.cli("wrapper", "add", "w", "--json", "{}", "--as-user", agent=True)[0], 0)
        self.assertEqual(self.cli("wrapper", "rm", "w", agent=True)[0], 3)
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        if os.access(locked, os.W_OK):
            self.skipTest("running as a user that ignores directory permissions")
        code, _, err = self.cli("wrapper", "add", "w", "--json", "{}", "--scope", "managed", "--path",
                                str(locked / "g.json"))
        self.assertEqual(code, 2)
        self.assertIn("sudo python3", err)

    def test_managed_wrappers_reach_the_hook_table(self) -> None:
        self.put(self.mpath, {"wrappers": {"mw": {"flagsWithValue": ["-x"]}}})
        import store

        managed, problems = store.load_managed()
        self.assertEqual(problems, [])
        self.assertEqual(policy.effective_wrappers(managed, {}, {})["mw"]["flagsWithValue"], ["-x"])
        self.put(self.mpath, {"wrappers": {"mw": {"skip": "no"}}})
        managed, problems = store.load_managed()
        self.assertTrue(any("mw" in p for p in problems))
        self.assertNotIn("mw", policy.effective_wrappers(managed, {}, {}))


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

    def test_bare_and_prefixed_commands_match_a_trailing_hole_pattern(self) -> None:
        out = self.cli("rule", "test", "--json", json.dumps(self.RULE), "pkill", "FOO=1 pkill x", "/usr/bin/pkill")[1]
        self.assertIn("match  pkill\n", out)
        self.assertIn("match  FOO=1 pkill x", out)
        self.assertIn("match  /usr/bin/pkill", out)

    def test_compile_errors_exit_2_on_test_add_and_set(self) -> None:
        bad = json.dumps({"match": {"ast": {"kind": "nope"}}, "message": "m"})
        for argv in (("rule", "test", "--json", bad, "x"), ("rule", "add", "r", "--json", bad)):
            code, _, err = self.cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("match.ast does not compile", err)
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
        out = self.cli("rule", "test", "--id", "r", "sudo pkill x", "ls")[1]
        self.assertIn("match  sudo pkill x", out)
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
        self.assertIn("rule r: match.ast does not compile", out)

    def test_rule_ast_prints_tree_and_wrapper_units(self) -> None:
        code, out, _ = self.cli("rule", "ast", "sudo -u bob pkill -f x | grep y")
        self.assertEqual(code, 0)
        for line in ("pipeline", "command_name «sudo»", "word «pkill»", "tree: through sudo, source: pkill -f x | grep y",
                     "units: 2 (1 as written, 1 through wrappers or shell strings)"):
            self.assertIn(line, out)
        out = self.cli("rule", "ast", "bash -c 'a; pkill x'")[1]
        self.assertIn("tree: through bash", out)
        self.assertIn("raw_string «'a; pkill x'»", out)

    def test_rule_ast_output_is_sanitised(self) -> None:
        out = self.cli("rule", "ast", "echo 'a\nb'\x1b[31m done")[1]
        self.assertNotIn("\x1b", out)
        self.assertIn("⏎", out)
        self.assertIn("␛", out)
        self.assertEqual(self.cli("rule", "ast", "  ")[0], 2)

    def test_rule_ast_flags_a_broken_tree(self) -> None:
        self.assertIn("ERROR or MISSING", self.cli("rule", "ast", "if a; then b")[1])


class Hook(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {
            "by-name": {"match": {"ast": BY_NAME}, "message": "No kill by name."},
            "nested": {"match": {"ast": NESTED}, "message": "No nested search.", "action": "warn"},
        }})

    def test_denies_through_wrappers_and_contexts(self) -> None:
        for n, command in enumerate(["pkill x", "sudo pkill x", "bash -c 'killall x'", f"echo ok; xargs pkill < {PG}"]):
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
        self.assertIn("match.ast rule bad does not compile", out["systemMessage"])
        again = self.hook("pkill y")
        assert again is not None
        self.assertNotIn("systemMessage", again)

    def test_user_wrapper_reaches_the_hook(self) -> None:
        self.assertIsNone(self.hook("mywrap -x 1 pkill a"))
        self.assertEqual(self.cli("wrapper", "add", "mywrap", "--json", '{"flagsWithValue": ["-x"]}')[0], 0)
        out = self.hook("mywrap -x 1 pkill a", session="s9")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_project_wrapper_layer(self) -> None:
        self.put(self.ppath, {"wrappers": {"projwrap": {}}})
        out = self.hook("projwrap pkill a", session="p1")
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
            self.assertIsNotNone(self.hook("sudo pkill x"))
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
        import re

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


class UvPath(Isolated):
    def setUp(self) -> None:
        super().setUp()
        if ast_mode() is None:
            self.skipTest("ast-grep-py is unavailable through uv here")
        os.environ.pop(astrun.INPROCESS_ENV, None)
        import astrun as run

        try:
            run.call({"op": "ping"})
        except run.Unavailable as exc:
            self.skipTest(f"uv cannot run ast-grep-py here: {exc}")

    def test_one_uv_run_answers_a_hook_call_with_the_pinned_version(self) -> None:
        response = astrun.call({"op": "eval", "command": "sudo pkill x", "rules": {"a": BY_NAME, "b": NESTED},
                                "wrappers": wrappers.effective()})
        self.assertEqual(response["version"], AST_PIN)
        self.assertEqual(response["verdicts"], {"a": "wrapped", "b": None})

    def test_hook_and_cli_use_uv_when_not_in_process(self) -> None:
        self.put(self.gpath, {"rules": {"by-name": {"match": {"ast": BY_NAME}, "message": "No kill."}}})
        out = self.hook("sudo pkill x")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        code, out_text, _ = self.cli("rule", "ast", "sudo pkill x")
        self.assertEqual(code, 0)
        self.assertIn("through sudo", out_text)


class Fallback(Isolated):
    """uv cannot be used: AST rules are skipped, everything else still applies, and the user is told once."""

    def setUp(self) -> None:
        super().setUp()
        self.marker = self.tmp / "uv-called"
        self.fake = self.tmp / "fake-uv"
        self.fake.write_text(f"#!/bin/sh\necho \"$@\" >> {self.marker}\nexit 1\n")
        self.fake.chmod(0o755)
        os.environ["GUARDRAILS_UV"] = str(self.fake)
        self.put(self.gpath, {"rules": {
            "ast-rule": {"match": {"ast": BY_NAME}, "message": "No kill by name."},
            "strings": {"match": {"program": "strings"}, "message": "No strings."},
            "pipe-sh": {"match": {"regex": r"curl [^|]*\| *sh"}, "message": "No curl pipe."},
        }})

    def worker_runs(self) -> int:
        return self.marker.read_text().count("astworker.py")

    def test_other_rules_still_deny_and_one_warning_per_session(self) -> None:
        first = self.hook("ls; strings /bin/ls")
        assert first is not None
        self.assertEqual(first["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("No strings.", first["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(first["systemMessage"].count("guardrails: the AST matcher is unavailable"), 1)
        second = self.hook("curl x | sh")
        assert second is not None
        self.assertEqual(second["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("systemMessage", second)
        other = self.hook("echo hi", session="s2")
        assert other is not None
        self.assertIn("AST matcher is unavailable", other["systemMessage"])
        self.assertNotIn("hookSpecificOutput", other)

    def test_ast_rule_is_skipped_not_enforced(self) -> None:
        out = self.hook("pkill x")
        assert out is not None
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("match.ast are skipped", out["systemMessage"])

    def test_missing_uv_and_empty_override_behave_the_same(self) -> None:
        for value in ("/nonexistent/uv", ""):
            os.environ["GUARDRAILS_UV"] = value
            with self.subTest(uv=value):
                out = self.hook("pkill x", session=f"m{value}")
                assert out is not None
                self.assertIn("uv is not installed", out["systemMessage"])

    def test_failed_download_is_remembered_for_a_while(self) -> None:
        self.hook("pkill x", session="a")
        self.assertEqual(self.worker_runs(), 2)
        self.hook("pkill x", session="b")
        self.assertEqual(self.worker_runs(), 3)
        self.assertTrue((self.data / astrun.STAMP).exists())

    def test_no_ast_rules_never_start_uv(self) -> None:
        self.put(self.gpath, {"rules": {"strings": {"match": {"program": "strings"}, "message": "No."}}})
        self.assertEqual(self.hook("strings x") is not None, True)
        self.assertIsNone(self.hook("ls"))
        self.assertFalse(self.marker.exists())

    def test_disabled_or_filtered_ast_rules_never_start_uv(self) -> None:
        self.put(self.gpath, {"rules": {"a": {"match": {"ast": BY_NAME}, "message": "m", "enabled": False},
                                        "b": {"match": {"ast": BY_NAME}, "message": "m", "requires": ["nope-xyz"]}}})
        self.assertIsNone(self.hook("pkill x"))
        self.assertFalse(self.marker.exists())

    def test_rule_test_reports_degradation_and_exits_zero(self) -> None:
        rule = json.dumps({"match": {"ast": BY_NAME, "regex": "zzz"}, "message": "m"})
        code, out, _ = self.cli("rule", "test", "--json", rule, "pkill x", "zzz")
        self.assertEqual(code, 0)
        self.assertIn("the AST matcher could not run", out)
        self.assertIn("-      pkill x", out)
        self.assertIn("match  zzz", out)
        code, out, _ = self.cli("rule", "test", "--render", "--json", rule, "pkill x")
        self.assertEqual(code, 0)
        self.assertIn("**Note** the AST matcher could not run", out)

    def test_rule_add_accepts_an_unverifiable_ast_rule_with_a_note(self) -> None:
        code, out, _ = self.cli("rule", "add", "r2", "--json", json.dumps({"match": {"ast": BY_NAME}, "message": "m"}))
        self.assertEqual(code, 0)
        self.assertIn("not compile-checked", out)
        code, _, _err = self.cli("rule", "add", "r3", "--json", json.dumps({"match": {"ast": {"bogus": 1}},
                                                                            "message": "m"}))
        self.assertEqual(code, 2)

    def test_rule_ast_and_status_fail_clearly(self) -> None:
        code, _, err = self.cli("rule", "ast", "ls")
        self.assertEqual(code, 2)
        self.assertIn("AST engine is unavailable", err)
        out = self.cli("status")[1]
        self.assertIn("cannot be evaluated", out)


class SessionStart(Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.marker = self.tmp / "uv-called"
        fake = self.tmp / "fake-uv"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> {self.marker}\nexit 0\n")
        fake.chmod(0o755)
        os.environ["GUARDRAILS_UV"] = str(fake)

    def warm(self) -> None:
        import subprocess
        import time

        from helpers import HOOKS

        proc = subprocess.run(["bash", str(HOOKS / "guardrails.sh"), "warm"], input=json.dumps({"cwd": str(self.proj)}),
                              capture_output=True, text=True, check=False, env=dict(os.environ))
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        for _ in range(40):
            if self.marker.exists():
                break
            time.sleep(0.05)

    def test_warms_only_when_an_enabled_ast_rule_exists(self) -> None:
        self.put(self.gpath, {"rules": {"s": {"match": {"program": "strings"}, "message": "m"}}})
        self.warm()
        self.assertFalse(self.marker.exists())
        self.put(self.gpath, {"rules": {"a": {"match": {"ast": BY_NAME}, "message": "m", "enabled": False}}})
        self.warm()
        self.assertFalse(self.marker.exists())
        self.put(self.ppath, {"rules": {"b": {"match": {"ast": BY_NAME}, "message": "m"}}})
        self.warm()
        self.assertTrue(self.marker.exists())
        self.assertIn(f"--with ast-grep-py=={AST_PIN}", self.marker.read_text())

    def test_hooks_json_registers_warm_up_and_a_roomy_timeout(self) -> None:
        from helpers import HOOKS

        hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
        pre = hooks["PreToolUse"][0]["hooks"][0]
        self.assertGreater(pre["timeout"], astrun.DEADLINE * 2)
        start = hooks["SessionStart"][0]["hooks"][0]
        self.assertTrue(start["command"].endswith("guardrails.sh\" warm"))
        self.assertLessEqual(start["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
