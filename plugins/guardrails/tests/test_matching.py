from __future__ import annotations  # noqa: I001

import shlex
import unittest
from typing import Any

from helpers import GREP_RECURSIVE, AstIsolated

import matching
import policy

K = "pk" + "ill"
ALL_KILLS = [K, "killall"]


def nest(command: str, levels: int) -> str:
    for _ in range(levels):
        command = "bash -c " + shlex.quote(command)
    return command


def rule_of(match: dict[str, Any], **extra: Any) -> policy.Rule:
    return policy.Rule.from_json({"match": match, "message": "m", **extra})


class Matching(AstIsolated):
    def test_command_atom_forms(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {
            f"{K} x": "direct", f"/usr/bin/{K} x": "direct", f"'{K}' x": "direct", f'"{K}" x': "direct",
            f"FOO=1 BAR=2 {K} x": "direct", f"{K} >/dev/null -9 y": "direct", f"a; {K} x": "direct",
            f"a && {K} x": "direct", f"({K} x)": "direct", f"{{ {K} x; }}": "direct", f"if {K} x; then b; fi": "direct",
            f"{K} x | head": "wrapped", f"ps | {K} x": "wrapped", f"echo $({K} x)": "wrapped",
            f"echo `{K} x`": "wrapped", f"cat <({K} x)": "wrapped", f'echo "$({K} x)"': "wrapped",
            f"echo {K}": None, f"man {K}": None, f"echo '{K} x'": None, f"echo '$({K} x)'": None, f"ls > {K}": None,
            f"cat <<'EOF'\n{K} x\nEOF": None, f"cat <<EOF\n{K} x\nEOF": None, f"git commit -m 'fix {K}'": None,
            f"{K}s x": None, f"x{K} y": None, f"ssh host {K} x": None, f"find . -exec {K} {{}} ;": None,
            "bash script.sh": None, f"python -c '{K} x'": None,
        })

    def test_command_atom_sees_these_forms_and_quoted_heredocs_stay_data(self) -> None:
        forms = ["xargs -I{} KILL {}", "xargs -I{} sh -c 'KILL {}'", "env - KILL x", 'env "A=1 B" KILL x',
                 "bash -c -- 'KILL x'", "sudo -nu bob KILL x", "sudo -Eu bob KILL x", "sudo -iu bob KILL x",
                 "xargs -i KILL {}", "cat <<EOF\n$(KILL x)\nEOF", "echo $(cat <<EOF\n$(KILL x)\nEOF\n)",
                 'echo "$(nm $(KILL z))"', "sudo -u bob -- KILL x"]
        rule = rule_of({"command": [K, "killall"]})
        for command in forms:
            with self.subTest(command=command):
                self.assertIsNotNone(matching.evaluate(command.replace("KILL", K), {"r": rule}).kinds["r"])
        self.assert_kinds(rule, {f"cat <<'EOF'\n$({K} x)\nEOF": None, f"cat <<\"EOF\"\n`{K} x`\nEOF": None})

    def test_obfuscated_and_dynamic_names_cannot_be_analysed(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {f"$'p\\x6b{K[2:]}' x": None, f"p''{K[1:]} x": None, f"p\\{K[1:]} x": None,
                                        f"P={K}; $P x": None, f"alias k={K}; k x": None, f"bash -c $'{K} x'": None,
                                        f"eval $'{K} x'": None})

    def test_unbalanced_quotes_are_not_commands_but_a_complete_command_before_them_is(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {f"echo 'x; {K} y": None, f"{K} y; echo 'x": "direct"})
        self.assert_kinds(rule_of({"command": "kill", "args": "-9"}), {"kill -9 1 \"x": "direct"})

    def test_names_with_regex_characters_are_matched_literally(self) -> None:
        for name in ("g++", "a.b", "x-y", "a^b", "x,y", "é"):
            with self.subTest(name=name):
                self.assert_kinds(rule_of({"command": name}), {f"{name} x": "direct", f"sudo {name} x": "wrapped", f"/bin/{name}": "direct",
                                                   "g xx": None, "ab x": None, "x x": None, "a_b x": None})

    def test_command_atom_list_and_each_name(self) -> None:
        self.assert_kinds(rule_of({"command": ALL_KILLS}), {f"{K} x": "direct", "killall node": "direct", "sudo killall x": "wrapped",
                                                "kill 1": None})

    def test_wrapper_branch_reads_the_wrappers_own_words(self) -> None:
        commands = [f"sudo {K} x", f"sudo -u bob {K} x", f"sudo -nu bob {K} x", f"env A=1 B=2 {K} x", f"env - {K} x",
                    f"timeout 5 {K} x", f"timeout -s KILL 5s {K} x", f"nice -n 10 {K} x", f"nohup {K} x", f"time {K} x",
                    f"command {K} x", f"exec {K} x", f"builtin {K} x", f"stdbuf -oL {K} x", f"setsid {K} x",
                    f"ionice -c 3 {K} x", f"xargs -I{{}} {K} {{}}", f"xargs -0 -n1 {K}", f"watch -n 5 {K} x",
                    f"doas {K} x", f"sudo -- {K} x", f"/usr/bin/sudo /bin/{K} x", f"sudo env timeout 5 nice -n 1 {K} x",
                    f"FOO=1 sudo {K} x", f"sudo {K} x > /dev/null 2>&1", f"echo a | xargs {K}", f"sudo 'sudo' {K}"]
        self.assert_kinds(rule_of({"command": K}), {c: "wrapped" for c in commands})

    def test_wrappers_are_not_looked_through_when_they_are_unknown_or_bare(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {f"mywrap -x 3 {K} a": None, f"ssh host {K}": None, "sudo": None, "env A=1": None})

    def test_wrapper_names_are_commands_too(self) -> None:
        self.assert_kinds(rule_of({"command": ["sudo", "xargs"]}), {"sudo ls": "direct", "xargs ls": "direct", "ls": None})

    def test_a_wrapper_word_match_is_the_declared_false_positive(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {f"sudo grep {K} file": "wrapped", f"command -v {K}": "wrapped"})

    def test_args_is_a_regex_over_the_whole_command(self) -> None:
        rule = rule_of({"command": "kill", "args": r"(^|\s)-(9|KILL|SIGKILL)(\s|$)|(^|\s)-s\s+(9|KILL|SIGKILL)(\s|$)"})
        self.assert_kinds(rule, {"kill -9 1": "direct", "kill -KILL 1": "direct", "kill -s 9 1": "direct", "kill -9": "direct",
                          "sudo kill -9 1": "wrapped", "xargs kill -9": "wrapped", "kill 1": None, "kill -99 1": None,
                          "echo kill -9": None, "kill -15 9": None, "pkill -9 x": None})

    def test_args_only_narrows_a_command_atom_and_alternatives_are_an_explicit_any(self) -> None:
        for match in ({"args": "x"}, {"has": {"args": "x"}}, {"command": "kill", "args": 9}):
            with self.subTest(match=match), self.assertRaises(policy.Invalid):
                rule_of(match)
        rule = rule_of({"any": [{"command": "kill"}, {"kind": "program", "regex": "zzz"}]})
        self.assert_kinds(rule, {"kill 1": "direct", "echo zzz": "direct", "ls": None})

    def test_the_recursive_grep_rule_of_the_modern_cli_preset(self) -> None:
        self.assert_kinds(rule_of(GREP_RECURSIVE), {
            "grep -r foo .": "direct", "grep -rn foo .": "direct", "grep -nr foo .": "direct", "grep -R foo .": "direct",
            "egrep -r foo .": "direct", "fgrep -rl foo .": "direct", "grep --recursive foo .": "direct",
            "grep --dereference-recursive foo": "direct", "grep -d recurse foo .": "direct",
            "grep --directories=recurse foo .": "direct", "grep --directories recurse foo": "direct",
            "grep -A3 -r foo .": "direct", "grep -e foo -r .": "direct", "/usr/bin/grep -r x": "direct",
            "sudo grep -rn foo .": "wrapped", "find . | xargs grep -rl foo": "wrapped", "ps | grep -r x": "wrapped",
            "sudo -u bob -- grep -rn foo .": "wrapped", "env -- grep -R x": "wrapped", "sudo -- grep foo -- -r": None,
            "grep foo file": None, "grep -n foo file": None, "grep -e r file": None, "grep -er file": None,
            "grep -d skip foo": None, "grep foo -- -r": None, "rg -r x": None, "git grep -n x": None,
            "echo grep -r": None, "grep -A3 foo file": None,
        })

    def test_atoms_patterns_and_regexes_combine_under_any(self) -> None:
        rule = rule_of({"any": [{"command": "kill"}, {"pattern": f"{K} $$$"}, {"kind": "program", "regex": "zzz"}]})
        self.assert_kinds(rule, {"kill 1": "direct", f"{K} x": "direct", "echo zzz": "direct", "ls": None,
                          f"sudo {K} x": "wrapped", "sudo kill 1": "wrapped", f"/usr/bin/{K} x": "direct"})

    def test_direct_beats_wrapped_on_the_same_rule(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {f"sudo {K} a; {K} b": "direct", f"{K} a | cat; sudo {K} b": "wrapped"})

    def test_ast_rules_naming_a_command_get_a_wrapper_branch_and_others_do_not(self) -> None:
        self.assert_kinds(rule_of({"pattern": f"{K} $$$"}), {f"{K} x": "direct", f"sudo {K} x": "wrapped",
                                                          f"xargs {K}": "wrapped", f"/usr/bin/{K} x": "direct",
                                                          f"'{K}' x": "direct", f"echo {K}": None})
        named = {"kind": "command", "has": {"field": "name", "regex": f"^{K}$"}}
        self.assert_kinds(rule_of(named), {f"{K} x": "direct", f"sudo {K} x": "wrapped"})
        by_text = {"kind": "command", "regex": f"^{K}"}
        self.assert_kinds(rule_of(by_text), {f"{K} x": "direct", f"sudo {K} x": "wrapped"})


class ShellStrings(AstIsolated):
    def test_scripts_of_shells_and_eval_are_scanned(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {
            f"bash -c '{K} x'": "wrapped", f'sh -c "a; {K} x"': "wrapped", f"zsh -c 'echo; {K} y'": "wrapped",
            f"dash -c '{K} x'": "wrapped", f"ksh -c '{K} x'": "wrapped", f"bash -lc '{K} x'": "wrapped",
            f"bash -ic '{K} x'": "wrapped", f"bash -o pipefail -c '{K} x'": "wrapped", f"sudo bash -c '{K} x'": "wrapped",
            f"env A=1 /bin/bash -c '{K} x'": "wrapped", f"bash -c {K}": "wrapped", f"bash -c '{K} x' arg0": "wrapped",
            f"eval {K} x": "wrapped", f"eval '{K} x'": "wrapped", f'eval "a; {K} x"': "wrapped",
            f"eval a; eval {K} x": "wrapped", f"eval 'a;' '{K}' x": "wrapped", f"x | bash -c 'a | {K} z'": "wrapped",
            f"bash -c 'FOO=1 {K} x'": "wrapped", f'bash -c "echo \\"{K} x\\""': None,
            f"bash -c 'echo {K}'": None, f"bash -c \"echo '{K} x'\"": None, "bash script.sh -c": None,
            f"ls -c '{K} x'": None, f"bash -s {K}": None, f"python -c '{K} x'": None,
        })

    def test_heredocs_and_here_strings_fed_to_a_shell_are_scripts(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {
            f"bash <<EOF\n{K} x\nEOF": "wrapped", f"sh <<'EOF'\necho a\n{K} x\nEOF": "wrapped",
            f"sudo bash <<EOF\n{K} x\nEOF": "wrapped", f"bash <<< '{K} x'": "wrapped", f"sh -s <<< \"{K} x\"": "wrapped",
            f"script -c '{K} x' out": "wrapped",
            f"cat <<EOF\n{K} x\nEOF": None, f"cat <<< '{K} x'": None, "bash <<EOF\necho hi\nEOF": None})

    def test_substitutions_in_unquoted_heredoc_bodies_run_and_the_rest_is_data(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {
            f"cat <<EOF\n`{K} x`\nEOF": "wrapped", f"cat <<EOF\nfoo `{K} x` bar\nEOF": "wrapped",
            f"cat <<-EOF\n\t`{K} x`\n\tEOF": "wrapped", f"cat <<EOF\n\t$({K} x)\nEOF": "wrapped",
            f"cat <<EOF\n  $({K} x)\nEOF": "wrapped", f"x=$(cat <<EOF\n`{K} x`\nEOF\n)": "wrapped",
            f"cat <<EOF\n{K} x\n`echo hi`\nEOF": None, f"cat <<'EOF'\n`{K} x`\nEOF": None,
            f"cat <<EOF\necho `echo hi` {K}\nEOF": None})

    def test_scripts_nest(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {nest(f"{K} x", 7): "wrapped",
                                        f"eval \"bash -c 'eval {K} x'\"": "wrapped",
                                        "sudo bash -c 'sudo bash -c \"sudo " + f"{K} x" + "\"'": "wrapped"})

    def test_a_script_is_judged_by_its_own_text(self) -> None:
        rule = rule_of({"kind": "command", "has": {"field": "name", "regex": f"^{K}$"},
                            "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}})
        self.assert_kinds(rule, {f"{K} x": "direct", f"if true; then {K} x; fi": None, f"bash -c '{K} x'": "wrapped",
                          f"bash -c 'if true; then {K} x; fi'": None, f"bash -c 'if a; then b; fi; {K} x'": "wrapped",
                          f"eval 'if a; then {K} x; fi'": None})

    def test_quoting_of_the_script_is_undone_once(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {f"bash -c \"{K} \\$HOME\"": "wrapped", f"bash -c '{K} \"x y\"'": "wrapped",
                                        f"bash -c \"bash -c '{K} x'\"": "wrapped", f"bash -c '{K}' '{K}'": "wrapped"})

class WrappedTag(AstIsolated):
    def test_tagging_matches_the_documented_meaning(self) -> None:
        wrapped = ["sudo pkill x", "bash -c 'pkill x'", "xargs pkill", "timeout 5 pkill x", "echo $(pkill x)",
                   "echo `pkill x`", "ps | pkill x", "pkill x | cat", "env A=1 pkill x", "sh -c 'a; pkill x'",
                   "cat <(pkill x)", "echo foo$(pkill x)", "eval pkill x"]
        direct = ["pkill x", "/usr/bin/pkill x", "FOO=1 pkill x", "a; pkill x", "a && pkill x", "(pkill x)",
                  "{ pkill x; }", "pkill x &", "a\npkill x", "if pkill x; then b; fi", "while pkill x; do c; done",
                  "for i in 1; do pkill x; done", "a || pkill x"]
        for ast in ({"kind": "command", "has": {"field": "name", "regex": "(^|/)pkill$"}}, None):
            rule = rule_of(ast) if ast else rule_of({"command": "pkill"})
            for commands, expected in ((wrapped, "wrapped"), (direct, "direct")):
                for command in commands:
                    if ast and command.startswith(("sudo", "xargs", "timeout", "env ")):
                        continue
                    with self.subTest(command=command, ast=bool(ast)):
                        self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)


class PatternShapes(AstIsolated):
    def test_patterns_that_are_not_one_simple_command_are_left_to_ast_grep_as_written(self) -> None:
        for pattern, hit, miss in (("curl $$$ | sh", "curl x | sh", "curl x | cat"), ("zap x > /dev/null", "zap x > /dev/null", "zap x"),
                                   ("for i in x; do zap $i; done", "for i in x; do zap $i; done", "zap x"),
                                   ("while zap x; do :; done", "while zap x; do :; done", "zap x"),
                                   ("if zap x; then ls; fi", "if zap x; then ls; fi", "zap x"),
                                   ("zap x && ls", "zap x && ls", "zap x"),
                                   ("echo $(zap x)", "echo $(zap x)", "zap x")):
            with self.subTest(pattern=pattern):
                rule = rule_of({"pattern": pattern})
                ev = matching.evaluate(hit, {"r": rule})
                self.assertEqual(ev.invalid, {})
                self.assertIsNotNone(ev.kinds["r"], hit)
                self.assertIsNone(matching.evaluate(miss, {"r": rule}).kinds["r"], miss)

    def test_pattern_text_cannot_inject_into_a_regex(self) -> None:
        rule = rule_of({"pattern": "a.b+ -x $$$"})
        for command, expected in {"a.b+ -x": "direct", "aXb+ -x": None, "a.bbb -x": None, "sudo a.b+ -x": "wrapped",
                                  "sudo aXb -x": None}.items():
            self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected, command)

    def test_wrapper_options_are_not_grep_options(self) -> None:
        rule = rule_of(GREP_RECURSIVE)
        for command, expected in {"find . | xargs -r grep -l foo": None, "xargs -0 -r grep foo": None,
                                  "xargs -r -- grep x": None, "sudo -r role grep foo f": None,
                                  "xargs -r grep -r foo": "wrapped", "sudo -r role grep -rn foo .": "wrapped",
                                  "grep -r x .": "direct", "xargs grep -rl foo": "wrapped"}.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)


class Relations(AstIsolated):
    def test_negated_relations_work_on_the_real_tree(self) -> None:
        publish = {"pattern": "npm publish $$$", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}}
        follows = {"pattern": "git push $$$", "not": {"follows": {"pattern": "git pull $$$", "stopBy": "end"}}}
        for ast, commands in ((publish, {"npm publish": "direct", "if true; then npm publish; fi": None,
                                         "if [ -n \"$T\" ]; then npm publish; fi": None, "a && npm publish": "direct"}),
                              (follows, {"git push": "direct", "git pull; git push": None, "git pull && git push": None,
                                         "git fetch; git push": "direct"})):
            for command, expected in commands.items():
                with self.subTest(command=command):
                    self.assertEqual(matching.evaluate(command, {"r": rule_of(ast)}).kinds["r"], expected)

if __name__ == "__main__":
    unittest.main()
