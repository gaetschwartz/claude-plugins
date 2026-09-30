from __future__ import annotations  # noqa: I001

import contextlib
import io
import json
import os
import re
import stat
import time
import unittest
from pathlib import Path
from collections.abc import Iterator
from typing import Any, ClassVar
from unittest import mock

from helpers import AST_PIN, AstIsolated, Isolated, ast_mode

import astrun
import engine
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

    def test_lexer_commands_are_always_united_with_the_tree(self) -> None:
        for command in ("if a; then b", "a; b"):
            response = astrun.call({"op": "eval", "command": command, "rules": {"r": BY_NAME},
                                    "lexed": [{"src": "pkill x", "wrapped": False}]})
            self.assertEqual(response["verdicts"], {"r": "direct"}, command)
        self.assertTrue(astrun.call({"op": "eval", "command": "if a; then b", "rules": {}})["broken"])

    def test_unbalanced_quotes_count_as_broken(self) -> None:
        for command in ('echo "unterminated', "a |", "echo $(", "cat <<EOF\nx"):
            self.assertTrue(astrun.call({"op": "eval", "command": command, "rules": {}})["broken"], command)
        for command in ("cat <<EOF\nEOF", "x=", "echo ''", "a && b", "f() { :; }", "echo $((1+2))"):
            self.assertFalse(astrun.call({"op": "eval", "command": command, "rules": {}})["broken"], command)

    def test_limits_are_reported_not_silent(self) -> None:
        deep = astrun.call({"op": "eval", "command": "sudo " * 40 + "pkill x", "rules": {"r": BY_NAME},
                            "wrappers": wrappers.effective()})
        self.assertTrue(deep["limited"])
        fine = astrun.call({"op": "eval", "command": "sudo " * 12 + "pkill x", "rules": {"r": BY_NAME},
                            "wrappers": wrappers.effective()})
        self.assertFalse(fine["limited"])
        self.assertEqual(fine["verdicts"], {"r": "wrapped"})

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
        text = Path(astrun.REQUIREMENTS).read_text()
        self.assertTrue(text.startswith(f"ast-grep-py=={AST_PIN} "))
        self.assertGreaterEqual(text.count("--hash=sha256:"), 16)
        self.assertNotIn("--index-url", text)
        venv_step, install_step = astrun.uv_steps("uv", "/t/venv", True)
        for flag in ("--no-config", "--require-hashes", "--only-binary", "--offline", "--default-index"):
            self.assertIn(flag, install_step)
        self.assertIn("--no-config", venv_step)
        steps = astrun.pip_steps("/t/venv")
        for flag in ("--require-hashes", "--isolated", "--only-binary", "--index-url", "--no-deps"):
            self.assertIn(flag, steps[1])
        self.assertIn("-I", steps[0])


class PlainLexer(unittest.TestCase):
    def test_assignment_prefix_inside_a_shell_string_is_looked_through(self) -> None:
        from shellwords import simple_commands

        for command in ("bash -c 'FOO=1 pkill x'", "eval 'A=1 pkill x'", "eval A=1 pkill x", "watch -n 1 'A=1 pkill x'"):
            self.assertIn("pkill", [c.name for c in simple_commands(command)], command)

    def test_user_wrapper_flags_reach_the_plain_lexer(self) -> None:
        from shellwords import simple_commands

        table = wrappers.effective({"wrappers": {"mywrap": {"flagsWithValue": ["-x"]}}})
        self.assertEqual([c.name for c in simple_commands("mywrap -x 3 pkill a", 0, table)], ["mywrap", "pkill"])
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

    def test_lower_layers_only_introduce_new_names(self) -> None:
        managed = {"wrappers": {"sudo": {"flagsWithValue": ["-X"], "skip": 3}, "mine": {"shellString": "-e"}}}
        glob = {"wrappers": {"mine": {"shellString": "-z", "flagsWithValue": ["-q"]}, "fresh": {"skip": 1}}}
        project = {"wrappers": {"mine": {"assignments": True}, "timeout": {"skip": 4}, "p": {}}}
        table, notes = wrappers.resolve([("m", managed), ("g", glob), ("p", project)])
        self.assertEqual(table["sudo"], wrappers.DEFAULTS["sudo"])
        self.assertEqual(table["timeout"], wrappers.DEFAULTS["timeout"])
        self.assertEqual(table["mine"], {"shellString": "-e"})
        self.assertEqual(table["fresh"], {"skip": 1})
        self.assertIn("p", table)
        self.assertEqual(len(notes), 4)
        self.assertTrue(all("ignored" in n for n in notes))
        self.assertTrue(any("built in" in n and "sudo" in n for n in notes))
        self.assertTrue(any("higher layer" in n and "mine" in n for n in notes))

    def test_invalid_entries_are_skipped_and_reported(self) -> None:
        layer = {"wrappers": {"ok": {}, "bad": {"skip": "x"}}}
        table, notes = wrappers.resolve([("global state", layer)])
        self.assertEqual(set(table) - set(wrappers.DEFAULTS), {"ok"})
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(wrappers.resolve([("g", {"wrappers": []})])[1]), 1)

    def test_policy_layering_and_project_switch(self) -> None:
        managed, _ = policy.managed_layer([("/m", {"wrappers": {"m1": {"flagsWithValue": ["-a"]}}})])
        self.assertEqual(policy.wrapper_problems(managed, {}, {}), [])
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


class RealVenv(Isolated):
    def setUp(self) -> None:
        super().setUp()
        if ast_mode() is None:
            self.skipTest("ast-grep-py cannot be installed here")
        patch = mock.patch.object(astrun, "BUILD_ALLOWED", True)
        patch.start()
        self.addCleanup(patch.stop)
        try:
            astrun.ensure(str(self.data), 120.0)
        except astrun.Unavailable as exc:
            self.skipTest(f"the venv cannot be built here: {exc}")

    def test_hash_verified_install_answers_with_the_pinned_version(self) -> None:
        response = astrun.call({"op": "eval", "command": "sudo pkill x", "rules": {"a": BY_NAME, "b": NESTED},
                                "wrappers": wrappers.effective()}, str(self.data))
        self.assertEqual(response["version"], AST_PIN)
        self.assertEqual(response["verdicts"], {"a": "wrapped", "b": None})
        venv = astrun.venv_dir(str(self.data))
        self.assertTrue(astrun.ready(venv))
        self.assertTrue(venv.startswith(str(self.data)))

    def test_hook_and_cli_use_the_installed_wheel(self) -> None:
        self.put(self.gpath, {"rules": {"by-name": {"match": {"ast": BY_NAME}, "message": "No kill."}}})
        out = self.hook("sudo pkill x")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        code, out_text, _ = self.cli("rule", "ast", "sudo pkill x")
        self.assertEqual(code, 0)
        self.assertIn("through sudo", out_text)

    def test_a_tampered_requirements_hash_refuses_to_install(self) -> None:
        bad = self.tmp / "bad-requirements.txt"
        bad.write_text(re.sub(r"--hash=sha256:[0-9a-f]{8}", "--hash=sha256:00000000",
                              Path(astrun.REQUIREMENTS).read_text()))
        with mock.patch.object(astrun, "REQUIREMENTS", str(bad)), self.assertRaises(astrun.Unavailable) as ctx:
            astrun.ensure(str(self.tmp / "fresh"))
        self.assertIn("failed", str(ctx.exception))
        self.assertFalse(astrun.ready(astrun.venv_dir(str(self.tmp / "fresh"))))


class Fallback(Isolated):
    """No venv: AST rules apply by command name, everything else as usual, and the session is told once."""

    def setUp(self) -> None:
        super().setUp()
        self.warm = mock.patch.object(astrun, "warm").start()
        self.ensure = mock.patch.object(astrun, "ensure", side_effect=astrun.Unavailable("stub: no venv")).start()
        self.addCleanup(mock.patch.stopall)
        self.put(self.gpath, {"rules": {
            "ast-rule": {"match": {"ast": BY_NAME}, "message": "No kill by name."},
            "strings": {"match": {"program": "strings"}, "message": "No strings."},
            "pipe-sh": {"match": {"regex": r"curl [^|]*\| *sh"}, "message": "No curl pipe."},
        }})

    def test_other_rules_still_deny_and_one_warning_per_session(self) -> None:
        first = self.hook("ls; strings /bin/ls")
        assert first is not None
        self.assertEqual(first["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = first["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("No strings.", reason)
        self.assertIn("the AST matcher is unavailable", reason)
        self.assertEqual(first["systemMessage"].count("guardrails: the AST matcher is unavailable"), 1)
        second = self.hook("curl x | sh")
        assert second is not None
        self.assertEqual(second["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("systemMessage", second)
        self.assertNotIn("unavailable", second["hookSpecificOutput"]["permissionDecisionReason"])
        other = self.hook("echo hi", session="s2")
        assert other is not None
        self.assertIn("AST matcher is unavailable", other["systemMessage"])
        self.assertIn("AST matcher is unavailable", other["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", other["hookSpecificOutput"])

    def test_the_hook_never_builds_anything_and_warms_once_per_session(self) -> None:
        self.hook("pkill x", session="w1")
        self.hook("pkill y", session="w1")
        self.hook("pkill z", session="w2")
        self.ensure.assert_not_called()
        self.assertEqual(self.warm.call_count, 2)

    def test_the_reason_says_the_venv_is_not_built(self) -> None:
        out = self.hook("pkill x")
        assert out is not None
        self.assertIn("venv is not built yet", out["systemMessage"])

    def test_ast_rule_applies_by_command_name_and_says_so(self) -> None:
        out = self.hook("pkill x")
        assert out is not None
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("[guardrails:ast-rule] No kill by name.", reason)
        self.assertIn("this rule applied because the command mentions", reason)
        self.assertIn("pkill", reason)
        self.assertIn("unavailable", reason)

    def test_commands_mentioning_no_rule_name_pass(self) -> None:
        out = self.hook("ls -la /tmp")
        assert out is not None
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_quoting_cannot_hide_a_name(self) -> None:
        for n, command in enumerate(["p''kill x", 'p""kill x', "p\\kill x", "$'p\\x6bill' x", '"pkill" x',
                                     "$'\\160kill' x", "'pk''ill' x", "sudo p''kill x"]):
            out = self.hook(command, f"q{n}")
            self.assertTrue(out is not None and out["hookSpecificOutput"].get("permissionDecision") == "deny", command)

    def test_declared_mentions_override_the_derived_names(self) -> None:
        self.put(self.gpath, {"rules": {"r": {"match": {"ast": BY_NAME, "mentions": ["zap"]}, "message": "No."}}})
        passed = self.hook("pkill x", session="q1")
        assert passed is not None
        self.assertNotIn("permissionDecision", passed["hookSpecificOutput"])
        denied = self.hook("sudo zap now", session="q2")
        assert denied is not None
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_warn_rules_warn_by_name(self) -> None:
        self.put(self.gpath, {"rules": {"w": {"match": {"ast": BY_NAME}, "message": "Careful.", "action": "warn"}}})
        out = self.hook("killall Finder")
        assert out is not None
        self.assertIn("Careful.", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("unavailable", out["hookSpecificOutput"]["additionalContext"])

    def test_no_ast_rules_never_look_for_the_venv(self) -> None:
        self.put(self.gpath, {"rules": {"strings": {"match": {"program": "strings"}, "message": "No."}}})
        with mock.patch.object(astrun, "call", side_effect=AssertionError("called")):
            self.assertIsNotNone(self.hook("strings x"))
            self.assertIsNone(self.hook("ls"))

    def test_disabled_or_filtered_ast_rules_never_look_for_the_venv(self) -> None:
        self.put(self.gpath, {"rules": {"a": {"match": {"ast": BY_NAME}, "message": "m", "enabled": False},
                                        "b": {"match": {"ast": BY_NAME}, "message": "m", "requires": ["nope-xyz"]}}})
        with mock.patch.object(astrun, "call", side_effect=AssertionError("called")):
            self.assertIsNone(self.hook("pkill x"))

    def test_rule_test_reports_degradation_and_exits_zero(self) -> None:
        rule = json.dumps({"match": {"ast": BY_NAME, "regex": "zzz"}, "message": "m"})
        code, out, _ = self.cli("rule", "test", "--json", rule, "pkill x", "zzz")
        self.assertEqual(code, 0)
        self.assertIn("the AST matcher could not run", out)
        self.assertIn("applied only by command name", out)
        self.assertIn("match  pkill x", out)
        self.assertIn("-      ls -la", self.cli("rule", "test", "--json", rule, "ls -la")[1])
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


class Builder(Isolated):
    """Building the venv, with the external commands stubbed."""

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[list[str]] = []
        self.result = mock.Mock(returncode=1, stderr="nope", stdout="")

        def fake_run(cmd: list[str], payload: Any, timeout: float, env: dict[str, str], cwd: str) -> Any:
            self.calls.append(cmd)
            return self.result

        for patch in (mock.patch.object(astrun, "_run", fake_run),
                      mock.patch.object(astrun, "uv_path", return_value="/stub/uv"),
                      mock.patch.object(astrun, "find_interpreter", return_value="/stub/python3.14")):
            patch.start()
            self.addCleanup(patch.stop)
        self.dir = str(self.tmp / "d")

    def test_a_failed_build_writes_the_stamp_and_later_calls_skip_it(self) -> None:
        with self.assertRaises(astrun.Unavailable):
            astrun.ensure(self.dir)
        first = len(self.calls)
        self.assertGreater(first, 0)
        self.assertTrue((Path(self.dir) / astrun.STAMP).exists())
        with self.assertRaises(astrun.Unavailable):
            astrun.ensure(self.dir)
        self.assertEqual(len(self.calls), first)

    def test_a_timed_out_build_writes_the_stamp_too(self) -> None:
        def hang(*_: Any) -> Any:
            raise astrun.Unavailable("timed out after 4.0s")

        with mock.patch.object(astrun, "_run", hang), self.assertRaises(astrun.Unavailable):
            astrun.ensure(self.dir)
        self.assertTrue((Path(self.dir) / astrun.STAMP).exists())

    def test_uv_steps_when_uv_is_found(self) -> None:
        with self.assertRaises(astrun.Unavailable):
            astrun.ensure(self.dir)
        self.assertEqual(self.calls[0][:2], ["/stub/uv", "venv"])
        self.assertIn("/stub/python3.14", self.calls[0])

    def test_pip_steps_when_uv_is_missing(self) -> None:
        with mock.patch.object(astrun, "uv_path", return_value=None), self.assertRaises(astrun.Unavailable):
            astrun.ensure(self.dir)
        self.assertEqual(self.calls[0][:4], ["/stub/python3.14", "-I", "-m", "venv"])

    def test_a_ready_venv_is_never_replaced_or_rebuilt(self) -> None:
        venv = Path(astrun.venv_dir(self.dir))
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\n")
        (venv / "bin" / "python").chmod(0o755)
        (venv / astrun.READY).write_text(astrun.requirements_digest() + "\n")
        self.assertEqual(astrun.ensure(self.dir), str(venv / "bin" / "python"))
        self.assertEqual(self.calls, [])

    def test_a_concurrent_builder_that_finishes_first_wins(self) -> None:
        venv = Path(astrun.venv_dir(self.dir))

        def builder(cmd: list[str], payload: Any, timeout: float, env: dict[str, str], cwd: str) -> Any:
            if "venv" in cmd and cmd[1] == "venv":
                Path(cmd[-1]).mkdir(parents=True)
                (Path(cmd[-1]) / "bin").mkdir()
                (Path(cmd[-1]) / "bin" / "python").write_text("new")
                (Path(cmd[-1]) / "bin" / "python").chmod(0o755)
            elif cmd[1] == "pip":
                (venv / "bin").mkdir(parents=True)
                (venv / "bin" / "python").write_text("winner")
                (venv / "bin" / "python").chmod(0o755)
                (venv / astrun.READY).write_text(astrun.requirements_digest() + "\n")
            return mock.Mock(returncode=0, stderr="", stdout="")

        with mock.patch.object(astrun, "_run", builder):
            astrun.ensure(self.dir)
        self.assertEqual((venv / "bin" / "python").read_text(), "winner")

    def test_stale_build_leftovers_are_reaped_and_fresh_ones_kept(self) -> None:
        root = Path(self.dir)
        root.mkdir()
        old, new = root / ".venv-old", root / ".venv-new"
        aside = root / "venv.old-1"
        for path in (old, new, aside):
            path.mkdir()
        past = time.time() - 3600
        os.utime(old, (past, past))
        os.utime(aside, (past, past))
        astrun.maintain(self.dir)
        self.assertFalse(old.exists())
        self.assertFalse(aside.exists())
        self.assertTrue(new.exists())

    def test_a_venv_python_that_fails_or_prints_garbage_is_unavailable(self) -> None:
        venv = Path(astrun.venv_dir(self.dir))
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\n")
        (venv / "bin" / "python").chmod(0o755)
        (venv / astrun.READY).write_text(astrun.requirements_digest() + "\n")
        for result in (mock.Mock(returncode=1, stdout="", stderr="Segmentation fault"),
                       mock.Mock(returncode=0, stdout="not json", stderr=""),
                       mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")):
            self.result = result
            with self.assertRaises(astrun.Unavailable):
                astrun.call({"op": "eval", "command": "x", "rules": {}}, self.dir)

    def test_the_lock_is_honoured(self) -> None:
        import fcntl

        root = Path(self.dir)
        root.mkdir()
        fd = os.open(root / astrun.LOCK, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(astrun.Unavailable) as ctx:
                astrun.ensure(self.dir, 0.3)
            self.assertIn("another process", str(ctx.exception))
            self.assertEqual(self.calls, [])
        finally:
            os.close(fd)

    def test_the_import_check_uses_the_remaining_budget(self) -> None:
        timeouts: list[float] = []

        def spy(cmd: list[str], payload: Any, timeout: float, env: dict[str, str], cwd: str) -> Any:
            timeouts.append(timeout)
            if cmd[1] == "venv":
                Path(cmd[-1]).mkdir(parents=True)
            return mock.Mock(returncode=0, stderr="", stdout="")

        with mock.patch.object(astrun, "_run", spy):
            astrun.ensure(self.dir, 3.0)
        self.assertTrue(timeouts and all(t <= 3.0 for t in timeouts), timeouts)


class SessionStart(Isolated):
    def test_warms_only_when_an_enabled_ast_rule_exists(self) -> None:
        with mock.patch.object(astrun, "warm") as warm:
            for rules in ({"s": {"match": {"program": "strings"}, "message": "m"}},
                          {"a": {"match": {"ast": BY_NAME}, "message": "m", "enabled": False}}):
                self.put(self.gpath, {"rules": rules})
                engine.run_warm(io.StringIO(json.dumps({"cwd": str(self.proj)})))
            warm.assert_not_called()
            self.put(self.ppath, {"rules": {"b": {"match": {"ast": BY_NAME}, "message": "m"}}})
            engine.run_warm(io.StringIO(json.dumps({"cwd": str(self.proj)})))
            warm.assert_called_once_with(str(self.data))

    def test_the_detached_build_gets_a_scrubbed_environment_and_respects_the_stamp(self) -> None:
        hostile = {"UV_FIND_LINKS": "/evil", "PIP_INDEX_URL": "http://evil", "PYTHONPATH": "/evil",
                   "UV_CACHE_DIR": "/cache"}
        with mock.patch.dict(os.environ, hostile), mock.patch.object(astrun.subprocess, "Popen") as popen:
            astrun.warm(str(self.data))
        args, kwargs = popen.call_args
        self.assertEqual(args[0][2:], ["warm-install", str(self.data)])
        self.assertEqual(kwargs["env"].get("UV_CACHE_DIR"), "/cache")
        for name in ("UV_FIND_LINKS", "PIP_INDEX_URL", "PYTHONPATH", "CLAUDE_PROJECT_DIR"):
            self.assertNotIn(name, kwargs["env"])
        self.assertTrue(kwargs["start_new_session"])
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / astrun.STAMP).write_text("")
        with mock.patch.object(astrun.subprocess, "Popen") as popen:
            astrun.warm(str(self.data))
        popen.assert_not_called()

    def test_hooks_json_registers_warm_up_and_a_roomy_timeout(self) -> None:
        from helpers import HOOKS

        hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
        pre = hooks["PreToolUse"][0]["hooks"][0]
        self.assertGreater(pre["timeout"], astrun.DEADLINE * 2)
        start = hooks["SessionStart"][0]["hooks"][0]
        self.assertTrue(start["command"].endswith("guardrails.sh\" warm"))
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash|Monitor")
        self.assertLessEqual(start["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
