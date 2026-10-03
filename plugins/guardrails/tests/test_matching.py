from __future__ import annotations  # noqa: I001

import shlex
import unittest
from typing import Any

from helpers import GREP_RECURSIVE, AstIsolated

import matching
import policy
import wrappers

K = "pk" + "ill"
ALL_KILLS = [K, "killall"]


def nest(command: str, levels: int) -> str:
    for _ in range(levels):
        command = "bash -c " + shlex.quote(command)
    return command


def rule_of(**match: Any) -> policy.Rule:
    return policy.with_defaults({"match": match, "message": "m"})


class Matching(AstIsolated):
    def kinds(self, rule: policy.Rule, commands: dict[str, str | None], names: wrappers.Names | None = None) -> None:
        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}, names).kinds["r"], expected)

    def test_program_forms(self) -> None:
        self.kinds(rule_of(program=K), {
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

    def test_obfuscated_and_dynamic_names_cannot_be_analysed(self) -> None:
        self.kinds(rule_of(program=K), {f"$'p\\x6b{K[2:]}' x": None, f"p''{K[1:]} x": None, f"p\\{K[1:]} x": None,
                                        f"P={K}; $P x": None, f"alias k={K}; k x": None, f"bash -c $'{K} x'": None,
                                        f"eval $'{K} x'": None})

    def test_unbalanced_quotes_are_not_commands_but_a_complete_command_before_them_is(self) -> None:
        self.kinds(rule_of(program=K), {f"echo 'x; {K} y": None, f"{K} y; echo 'x": "direct"})
        self.kinds(rule_of(program="kill", args="-9"), {"kill -9 1 \"x": "direct"})

    def test_names_with_regex_characters_are_matched_literally(self) -> None:
        for name in ("g++", "a.b", "x-y", "a^b", "x,y", "é"):
            with self.subTest(name=name):
                self.kinds(rule_of(program=name), {f"{name} x": "direct", f"sudo {name} x": "wrapped", f"/bin/{name}": "direct",
                                                   "g xx": None, "ab x": None, "x x": None, "a_b x": None})

    def test_program_list_and_each_name(self) -> None:
        self.kinds(rule_of(program=ALL_KILLS), {f"{K} x": "direct", "killall node": "direct", "sudo killall x": "wrapped",
                                                "kill 1": None})

    def test_wrapper_branch_reads_the_wrappers_own_words(self) -> None:
        commands = [f"sudo {K} x", f"sudo -u bob {K} x", f"sudo -nu bob {K} x", f"env A=1 B=2 {K} x", f"env - {K} x",
                    f"timeout 5 {K} x", f"timeout -s KILL 5s {K} x", f"nice -n 10 {K} x", f"nohup {K} x", f"time {K} x",
                    f"command {K} x", f"exec {K} x", f"builtin {K} x", f"stdbuf -oL {K} x", f"setsid {K} x",
                    f"ionice -c 3 {K} x", f"xargs -I{{}} {K} {{}}", f"xargs -0 -n1 {K}", f"watch -n 5 {K} x",
                    f"doas {K} x", f"sudo -- {K} x", f"/usr/bin/sudo /bin/{K} x", f"sudo env timeout 5 nice -n 1 {K} x",
                    f"FOO=1 sudo {K} x", f"sudo {K} x > /dev/null 2>&1", f"echo a | xargs {K}", f"sudo 'sudo' {K}"]
        self.kinds(rule_of(program=K), {c: "wrapped" for c in commands})

    def test_wrappers_are_not_looked_through_when_they_are_unknown_or_bare(self) -> None:
        self.kinds(rule_of(program=K), {f"mywrap -x 3 {K} a": None, f"ssh host {K}": None, "sudo": None, "env A=1": None})
        names = wrappers.resolve([("g", {"wrappers": {"mywrap": {}}})])[0]
        self.kinds(rule_of(program=K), {f"mywrap -x 3 {K} a": "wrapped"}, names)

    def test_wrapper_names_are_programs_too(self) -> None:
        self.kinds(rule_of(program=["sudo", "xargs"]), {"sudo ls": "direct", "xargs ls": "direct", "ls": None})

    def test_a_wrapper_word_match_is_the_declared_false_positive(self) -> None:
        self.kinds(rule_of(program=K), {f"sudo grep {K} file": "wrapped", f"command -v {K}": "wrapped"})

    def test_args_is_a_regex_over_the_whole_command(self) -> None:
        rule = rule_of(program="kill", args=r"(^|\s)-(9|KILL|SIGKILL)(\s|$)|(^|\s)-s\s+(9|KILL|SIGKILL)(\s|$)")
        self.kinds(rule, {"kill -9 1": "direct", "kill -KILL 1": "direct", "kill -s 9 1": "direct", "kill -9": "direct",
                          "sudo kill -9 1": "wrapped", "xargs kill -9": "wrapped", "kill 1": None, "kill -99 1": None,
                          "echo kill -9": None, "kill -15 9": None, "pkill -9 x": None})

    def test_args_alone_and_with_regex_stay_alternatives_only_where_documented(self) -> None:
        with self.assertRaises(policy.Invalid):
            policy.validate_rule({"match": {"args": "x"}, "message": "m"})
        rule = rule_of(program="kill", regex="zzz")
        self.kinds(rule, {"kill 1": "direct", "echo zzz": "direct", "ls": None})

    def test_the_recursive_grep_rule_of_the_modern_cli_preset(self) -> None:
        self.kinds(rule_of(ast=GREP_RECURSIVE), {
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

    def test_ast_alternatives_and_regex_combine(self) -> None:
        rule = rule_of(program="kill", ast={"pattern": f"{K} $$$"}, regex="zzz")
        self.kinds(rule, {"kill 1": "direct", f"{K} x": "direct", "echo zzz": "direct", "ls": None,
                          f"sudo {K} x": "wrapped", "sudo kill 1": "wrapped", f"/usr/bin/{K} x": "direct"})

    def test_direct_beats_wrapped_on_the_same_rule(self) -> None:
        self.kinds(rule_of(program=K), {f"sudo {K} a; {K} b": "direct", f"{K} a | cat; sudo {K} b": "wrapped"})

    def test_ast_rules_naming_a_command_get_a_wrapper_branch_and_others_do_not(self) -> None:
        self.kinds(rule_of(ast={"pattern": f"{K} $$$"}), {f"{K} x": "direct", f"sudo {K} x": "wrapped",
                                                          f"xargs {K}": "wrapped", f"/usr/bin/{K} x": "direct",
                                                          f"'{K}' x": "direct", f"echo {K}": None})
        named = {"kind": "command", "has": {"field": "name", "regex": f"^{K}$"}}
        self.kinds(rule_of(ast=named), {f"{K} x": "direct", f"sudo {K} x": "wrapped"})
        by_text = {"kind": "command", "regex": f"^{K}"}
        self.kinds(rule_of(ast=by_text), {f"{K} x": "direct", f"sudo {K} x": "wrapped"})


class ShellStrings(AstIsolated):
    def kinds(self, rule: policy.Rule, commands: dict[str, str | None]) -> None:
        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)

    def test_scripts_of_shells_and_eval_are_scanned(self) -> None:
        self.kinds(rule_of(program=K), {
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
        self.kinds(rule_of(program=K), {
            f"bash <<EOF\n{K} x\nEOF": "wrapped", f"sh <<'EOF'\necho a\n{K} x\nEOF": "wrapped",
            f"sudo bash <<EOF\n{K} x\nEOF": "wrapped", f"bash <<< '{K} x'": "wrapped", f"sh -s <<< \"{K} x\"": "wrapped",
            f"script -c '{K} x' out": "wrapped",
            f"cat <<EOF\n{K} x\nEOF": None, f"cat <<< '{K} x'": None, "bash <<EOF\necho hi\nEOF": None})

    def test_substitutions_in_unquoted_heredoc_bodies_run_and_the_rest_is_data(self) -> None:
        self.kinds(rule_of(program=K), {
            f"cat <<EOF\n`{K} x`\nEOF": "wrapped", f"cat <<EOF\nfoo `{K} x` bar\nEOF": "wrapped",
            f"cat <<-EOF\n\t`{K} x`\n\tEOF": "wrapped", f"cat <<EOF\n\t$({K} x)\nEOF": "wrapped",
            f"cat <<EOF\n  $({K} x)\nEOF": "wrapped", f"x=$(cat <<EOF\n`{K} x`\nEOF\n)": "wrapped",
            f"cat <<EOF\n{K} x\n`echo hi`\nEOF": None, f"cat <<'EOF'\n`{K} x`\nEOF": None,
            f"cat <<EOF\necho `echo hi` {K}\nEOF": None})

    def test_scripts_nest(self) -> None:
        self.kinds(rule_of(program=K), {nest(f"{K} x", 7): "wrapped",
                                        f"eval \"bash -c 'eval {K} x'\"": "wrapped",
                                        "sudo bash -c 'sudo bash -c \"sudo " + f"{K} x" + "\"'": "wrapped"})

    def test_a_script_is_judged_by_its_own_text(self) -> None:
        rule = rule_of(ast={"kind": "command", "has": {"field": "name", "regex": f"^{K}$"},
                            "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}})
        self.kinds(rule, {f"{K} x": "direct", f"if true; then {K} x; fi": None, f"bash -c '{K} x'": "wrapped",
                          f"bash -c 'if true; then {K} x; fi'": None, f"bash -c 'if a; then b; fi; {K} x'": "wrapped",
                          f"eval 'if a; then {K} x; fi'": None})

    def test_quoting_of_the_script_is_undone_once(self) -> None:
        self.kinds(rule_of(program=K), {f"bash -c \"{K} \\$HOME\"": "wrapped", f"bash -c '{K} \"x y\"'": "wrapped",
                                        f"bash -c \"bash -c '{K} x'\"": "wrapped", f"bash -c '{K}' '{K}'": "wrapped"})

    def test_distinct_scripts_beyond_the_caps_refuse_the_command(self) -> None:
        rules = {"r": rule_of(program=K)}
        many = "; ".join(f"bash -c 'echo {n}'" for n in range(80))
        for command, reason in ((nest(f"{K} x", 9), "nests shell strings too deeply"),
                                (many, "unpacks into too many shell strings"),
                                (nest("x" * 100_000, 4), "unpacks into too much shell text")):
            with self.subTest(reason=reason):
                self.assertIn(reason, matching.evaluate(command, rules).refusal or "")
        self.assertIsNone(matching.evaluate(nest(f"{K} x", 8), rules).refusal)

    def test_repeated_scripts_are_scanned_once(self) -> None:
        ev = matching.evaluate("; ".join([f"bash -c 'echo {K}'"] * 500), {"r": rule_of(program=K)})
        self.assertIsNone(ev.refusal)


class WrappedTag(AstIsolated):
    def test_tagging_matches_the_documented_meaning(self) -> None:
        wrapped = ["sudo pkill x", "bash -c 'pkill x'", "xargs pkill", "timeout 5 pkill x", "echo $(pkill x)",
                   "echo `pkill x`", "ps | pkill x", "pkill x | cat", "env A=1 pkill x", "sh -c 'a; pkill x'",
                   "cat <(pkill x)", "echo foo$(pkill x)", "eval pkill x"]
        direct = ["pkill x", "/usr/bin/pkill x", "FOO=1 pkill x", "a; pkill x", "a && pkill x", "(pkill x)",
                  "{ pkill x; }", "pkill x &", "a\npkill x", "if pkill x; then b; fi", "while pkill x; do c; done",
                  "for i in 1; do pkill x; done", "a || pkill x"]
        for ast in ({"kind": "command", "has": {"field": "name", "regex": "(^|/)pkill$"}}, None):
            rule = rule_of(ast=ast) if ast else rule_of(program="pkill")
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
                rule = rule_of(ast={"pattern": pattern})
                ev = matching.evaluate(hit, {"r": rule})
                self.assertEqual(ev.invalid, {})
                self.assertIsNotNone(ev.kinds["r"], hit)
                self.assertIsNone(matching.evaluate(miss, {"r": rule}).kinds["r"], miss)

    def test_a_single_command_pattern_tolerates_assignments_paths_and_quotes_and_wrapped_paths(self) -> None:
        rule = rule_of(ast={"pattern": "git push -f $$$"})
        for command, expected in {"git push -f o": "direct", "FOO=1 git push -f o": "direct", "A=1 B=2 git push -f": "direct",
                                  "/usr/bin/git push -f o": "direct", "'git' push -f o": "direct",
                                  "sudo git push -f o": "wrapped", "sudo /usr/bin/git push -f o": "wrapped",
                                  "sudo -u bob 'git' push -f o": "wrapped", "FOO=1 sudo git push -f o": "wrapped",
                                  "git push o": None, "git pull -f": None, "echo git push -f": None}.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)

    def test_pattern_text_cannot_inject_into_a_regex(self) -> None:
        rule = rule_of(ast={"pattern": "a.b+ -x $$$"})
        for command, expected in {"a.b+ -x": "direct", "aXb+ -x": None, "a.bbb -x": None, "sudo a.b+ -x": "wrapped",
                                  "sudo aXb -x": None}.items():
            self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected, command)

    def test_wrapper_options_are_not_grep_options(self) -> None:
        rule = rule_of(ast=GREP_RECURSIVE)
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
                    self.assertEqual(matching.evaluate(command, {"r": rule_of(ast=ast)}).kinds["r"], expected)

    def test_a_relation_is_judged_on_the_real_tree_even_behind_a_wrapper(self) -> None:
        rule = rule_of(ast={"pattern": "npm publish $$$", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}})
        for command, expected in {"sudo npm publish": "wrapped", "if a; then sudo npm publish; fi": None,
                                  "if a; then b; fi; sudo npm publish": "wrapped"}.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)


if __name__ == "__main__":
    unittest.main()
