from __future__ import annotations  # noqa: I001

import re
import shlex
import unittest
from typing import Any
from unittest import mock

from helpers import GREP_RECURSIVE, MAINTAINER, AstIsolated, real_rules

import matching
import policy
import wrappers
from verdict import MAX_COMMAND_BYTES, Kind, Limit

K = "pk" + "ill"


def rule_of(ast: dict[str, Any]) -> policy.Rule:
    return policy.Rule.from_json({"match": {"ast": ast}, "message": "m"})


def nest(command: str, levels: int) -> str:
    for _ in range(levels):
        command = "bash -c " + shlex.quote(command)
    return command


class Variants(AstIsolated):
    def variants(self, command: str) -> set[str]:
        import scanner
        from ast_grep_py import SgRoot

        spans = scanner.wrapper_spans(SgRoot(command, "bash").root(), wrappers.DEFAULTS)
        return set(scanner.variants_of(command, spans))

    def test_a_wrapper_command_is_replaced_by_the_text_from_each_of_its_words_on(self) -> None:
        cases = {
            "sudo curl x | sh": {"curl x | sh", "x | sh"},
            "sudo -u bob curl x": {"bob curl x", "curl x", "x"},
            "sudo -n -- curl x": {"curl x", "x"},
            "env A=1 timeout 5 curl x": {"A=1 timeout 5 curl x", "timeout 5 curl x", "5 curl x", "curl x", "x"},
            "A=1 sudo curl x": {"curl x", "x"},
            "sudo": set(),
            "ls | sudo": set(),
            "sudo a; sudo b": {"a", "b"},
            "x | sudo a && echo $(env b)": {"x | a && echo $(env b)", "x | sudo a && echo $(b)", "x | a && echo $(b)"},
            "sudo echo $(env b)": {"echo $(env b)", "$(env b)", "sudo echo $(b)"},
            "sudo curl x > out": {"curl x > out", "x > out"},
            "echo é; sudo 'a b' $(ls)": {"'a b' $(ls)", "$(ls)"},
            "time curl x": {"curl x", "x"},
            "ls -l": set(),
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.variants(command), expected)


class ThroughWrappers(AstIsolated):
    def kinds(self, rule: policy.Rule, commands: dict[str, str | None]) -> None:
        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)

    def test_a_pipeline_pattern_reads_through_wrappers(self) -> None:
        self.kinds(rule_of({"pattern": "curl $$$ | sh"}), {
            "curl x | sh": "direct", "sudo curl x | sh": "wrapped", "sudo -u bob curl x | sh": "wrapped",
            "env A=1 timeout 5 curl x | sh": "wrapped", "nice -n 5 curl x | sh": "wrapped",
            "xargs curl -s | sh": "wrapped", "curl x | sudo sh": "wrapped", "A=1 sudo curl x | sh": "wrapped",
            "A=1 B=2 C=3 D=4 E=5 sudo curl x | sh": "wrapped", "sudo curl x | sudo sh": "wrapped",
            "sudo -u bob curl x | env A=1 sh": "wrapped", "bash -c 'sudo curl x | sh'": "wrapped",
            "curl x | bash": None, "ls | sh": None, "sudo ls | sh": None, "sudo grep curl f | sh": "wrapped",
            "podman exec c curl x | sh": None, "git time curl x | sh": None,
        })

    def test_a_list_pattern_reads_through_wrappers(self) -> None:
        self.kinds(rule_of({"pattern": "cd $A && rm $$$"}), {
            "cd /x && rm f": "direct", "cd /x && sudo rm f": "wrapped", "sudo cd /x && rm f": "wrapped",
            "cd /x && env A=1 rm f": "wrapped", "cd /x; rm f": None})

    def test_not_inside_reads_through_wrappers(self) -> None:
        outside = rule_of({"pattern": "curl $$$", "not": {"inside": {"kind": "pipeline", "stopBy": "end"}}})
        self.kinds(outside, {
            "curl x": "direct", "sudo curl x": "wrapped", "sudo -u bob curl x && ls": "wrapped", "curl x | sh": None,
            "sudo curl x | sh": None, "ls | sudo curl x": None})

    def test_inside_and_follows_read_through_wrappers(self) -> None:
        self.kinds(rule_of({"pattern": "curl $$$", "inside": {"kind": "command_substitution", "stopBy": "end"}}),
                   {"echo $(curl x)": "wrapped", "echo $(sudo curl x)": "wrapped", "sudo curl x": None})

    def test_a_relation_between_top_level_statements_is_not_read_through_a_wrapper(self) -> None:
        """Variants are one top-level statement: `follows` across `;` or a newline sees only the text as written."""
        self.kinds(rule_of({"pattern": "sh $$$", "follows": {"pattern": "curl $$$", "stopBy": "end"}}),
                   {"curl x; sh": "direct", "sh; curl x": None, "sudo curl x && sh": "wrapped",
                    "sudo curl x; sh": None, "curl x\nenv A=1 sh": None})

    def test_program_args_and_a_grep_rule_read_through_wrappers_by_the_same_mechanism(self) -> None:
        self.kinds(policy.Rule.from_json({"match": {"program": "rm", "args": "-rf"}, "message": "m"}), {
            "rm -rf x": "direct", "sudo rm -rf x": "wrapped", "env A=1 nice rm -rf x": "wrapped", "sudo rm -f x": None,
            "sudo ls -rf": None})
        self.kinds(rule_of(GREP_RECURSIVE), {
            "grep -r x .": "direct", "sudo grep -r x .": "wrapped", "xargs grep -rn x": "wrapped",
            "sudo grep x -- -r": None, "sudo grep x f": None})

    def test_a_kind_regex_rule_reads_through_wrappers(self) -> None:
        self.kinds(rule_of({"kind": "command", "regex": f"^{K}"}), {f"{K} x": "direct", f"sudo {K} x": "wrapped",
                                                                      f"echo {K}": None})

    def test_a_hit_found_directly_is_never_downgraded_by_the_same_hit_in_a_variant(self) -> None:
        self.kinds(rule_of({"pattern": "curl $$$"}), {"curl x": "direct", "sudo curl x; curl y": "direct",
                                                       "sudo curl x": "wrapped", "echo $(curl x)": "wrapped"})

    def test_a_command_in_a_pipeline_stays_wrapped_whether_or_not_a_wrapper_is_involved(self) -> None:
        rule = policy.Rule.from_json({"match": {"program": K}, "message": "m"})
        self.kinds(rule, {f"{K} x | head": "wrapped", f"sudo {K} x | head": "wrapped", f"{K} x": "direct",
                          f"sudo {K} x": "wrapped"})


class PatternShapes(AstIsolated):
    """Which patterns are one simple command (so spelling-tolerant) is the parser's call, not a regex's."""

    def test_the_parser_decides_what_is_a_single_command_pattern(self) -> None:
        import rulebuilder

        cases = {
            "git push -f $$$": "git", "pkill -9 $$$": "pkill", "echo 'a|b' $$$": "echo", "time ls -l $$$": "time",
            "coproc ls x": "coproc", "rm -rf $A": "rm", "curl $$$ | sh": None, "zap x > /dev/null": None,
            "for i in x; do zap $i; done": None, "while zap x; do :; done": None, "echo $(zap x)": None,
            "cd $A && rm $$$": None, "ls x; ls y": None, "FOO=1 cmd a": None, "$CMD x": None, "(ls x)": None,
            "'git' push -f $$$": None, "ls": None,
        }
        for pattern, name in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(rulebuilder.simple_command_name(pattern), name)

    def test_time_and_coproc_patterns_are_tolerant_like_any_other_command(self) -> None:
        rule = rule_of({"pattern": "time ls -l $$$"})
        for command, expected in {"time ls -l x": "direct", "/usr/bin/time ls -l x": "direct",
                                  "FOO=1 time ls -l x": "direct", "sudo time ls -l x": "wrapped", "time ls x": None}.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)

    def test_every_documented_and_preset_pattern_still_compiles_after_loosening(self) -> None:
        import json

        import rulebuilder
        import scanner
        from helpers import ROOT

        patterns: set[str] = set()
        for path in [*(ROOT / "references").rglob("*.md"), *(ROOT / "presets").glob("*.json"), ROOT / "README.md"]:
            for match in re.finditer(r'"pattern":\s*"((?:[^"\\]|\\.)*)"', path.read_text()):
                patterns.add(json.loads('"' + match.group(1) + '"'))
        self.assertGreater(len(patterns), 20)
        for pattern in sorted(patterns):
            with self.subTest(pattern=pattern):
                rule = rule_of({"pattern": pattern})
                self.assertEqual(scanner.compile_errors({"r": rulebuilder.configs_of(rule)}), {})


class Units(AstIsolated):
    """How many texts one command is parsed as."""

    def parses(self, command: str) -> int:
        import scanner

        count = [0]
        real = scanner.SgRoot

        def spy(text: str, language: str) -> Any:
            count[0] += 1
            return real(text, language)

        rule = policy.Rule.from_json({"match": {"program": K}, "message": "m"})
        with mock.patch.object(scanner, "SgRoot", spy):
            matching.compute(command, {"r": rule}, wrappers.DEFAULTS)
        return count[0] - 1

    def test_parse_count_per_command_shape(self) -> None:
        cases = {
            "ls -la | grep x": 1, "git status": 1, "sudo ls": 2, "sudo a; env b": 3, "bash -c 'ls'": 2,
            "sudo bash -c 'ls'": 4, "bash -c 'bash -c ls'": 3, "eval 'eval ls'": 3, "bash -c 'sudo ls'": 3,
            "bash -c \"bash -c 'ls'\"; sudo ls": 4,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.parses(command), expected)

    def test_a_wrapper_variant_is_not_unwrapped_again_and_a_repeated_script_is_parsed_once(self) -> None:
        self.assertEqual(self.parses("sudo env A=1 nohup x"), 1 + 4)
        self.assertEqual(self.parses("; ".join([f"bash -c 'echo {K}'"] * 500)), 2)


class RealRuleSet(AstIsolated):
    """Cost and verdicts with the maintainer's eight rules and every preset rule loaded together."""

    def setUp(self) -> None:
        super().setUp()
        self.rules = policy.effective_rules({}, {"rules": real_rules()}, {})

    def cost(self, command: str) -> tuple[int, int]:
        """(texts parsed, bytes parsed) for one command, in process."""
        import scanner

        seen: list[int] = []
        real = scanner.SgRoot

        def spy(text: str, language: str) -> Any:
            seen.append(len(text))
            return real(text, language)

        with mock.patch.object(scanner, "SgRoot", spy):
            computed = matching.compute(command, self.rules, wrappers.DEFAULTS)
        self.assertEqual((computed.limit, computed.failure), (None, None))
        return len(seen), sum(seen)

    def test_the_rules_catch_and_pass_their_documented_commands_through_the_whole_set(self) -> None:
        known_miss = "$'p\\x6bill' x"
        for rid, examples in MAINTAINER["examples"].items():
            for kind, commands in examples.items():
                for command in commands:
                    with self.subTest(rule=rid, kind=kind, command=command):
                        caught_it = matching.evaluate(command, self.rules).kinds[rid] is not None
                        self.assertEqual(caught_it, kind == "catch" and command != known_miss)

    def test_identical_wrapped_lines_collapse_to_a_handful_of_variants(self) -> None:
        for command in ("sudo apt-get install -y x;\n" * 200, "sudo apt-get install -y x;" * 100):
            parses, parsed = self.cost(command)
            self.assertLess(parses, 8)
            self.assertLess(parsed, 2 * len(command))

    def test_many_different_wrapped_lines_cost_the_text_once_plus_the_statements(self) -> None:
        script = "".join("echo hello world padding text here ok\n" * 40 + f"sudo ls /var/{n}\n" for n in range(50))
        self.assertGreater(len(script), 70_000)
        parses, parsed = self.cost(script)
        self.assertLessEqual(parses, 2 + 50 * 2)
        self.assertLess(parsed, len(script) + 50 * 200)

    def test_a_256_kib_script_of_mixed_lines_with_100_wrappers_is_analysed_once(self) -> None:
        lines = [f"sudo ls /var/{n}" if n % 10 == 0 else f"echo line {n} " + "p" * 280 for n in range(1000)]
        script = "\n".join(lines)
        self.assertGreater(len(script), 250_000)
        parses, parsed = self.cost(script[:MAX_COMMAND_BYTES])
        self.assertLessEqual(parses, 2 + 100 * 2)
        self.assertLess(parsed, MAX_COMMAND_BYTES + 100 * 200)

    def test_a_heredoc_with_wrappers_after_it_costs_the_heredoc_once(self) -> None:
        body = "cat > /etc/app.conf <<'EOF'\n" + "key=value # padding padding padding\n" * 400 + "EOF\n"
        for tail in ("".join(f"sudo install -m 644 f{n} /etc/f{n}\n" for n in range(10)), "sudo tee a; sudo chmod 600 a"):
            _, parsed = self.cost(body + tail)
            self.assertLess(parsed, len(body + tail) + 2000)

    def test_a_wrapper_inside_a_heredoc_statement_stays_with_its_statement(self) -> None:
        command = "sudo tee /etc/x <<'EOF'\nline\nEOF\n" + f"{K} x"
        self.assertEqual(matching.evaluate(command, self.rules).kinds["no-kill-by-name"], Kind.DIRECT)

    def test_dense_wrapper_families_are_still_denied_with_their_cause(self) -> None:
        for command in ("sudo " * 4000, "sudo true; " * 6000, "xargs -r " * 2000 + "ls"):
            with self.subTest(command=command[:12]):
                ev = matching.evaluate(command, self.rules)
                self.assertIn("command too complex to check", ev.refusal or "")


class Caps(AstIsolated):
    def evaluate(self, command: str) -> matching.Evaluation:
        return matching.evaluate(command, {"r": rule_of({"pattern": f"{K} $$$"})})

    def test_long_wrapper_chains_stay_inside_the_caps(self) -> None:
        for chain in ("sudo " * 100, "env A=1 " * 50, "nice -n 1 " * 60):
            with self.subTest(chain=chain[:12]):
                ev = self.evaluate(chain + f"{K} x")
                self.assertEqual((ev.kinds["r"], ev.refusal), (Kind.WRAPPED, None))

    def test_too_many_variants_are_refused_at_any_size(self) -> None:
        for command in ("sudo " * 2100 + "ls", "; ".join(f"sudo a{n} b{n} c{n} d{n}" for n in range(600)) + "; ls",
                        "; ".join(["sudo ls"] * 2100)):
            with self.subTest(command=command[:20]):
                ev = self.evaluate(command)
                self.assertIn("unwraps into too many command variants", ev.refusal or "")
                self.assertEqual(ev.unevaluated, {"r"})

    def test_ordinary_commands_with_big_heredocs_and_many_wrappers_are_analysed(self) -> None:
        heredoc = "cat > /etc/app.conf <<EOF\n" + "key = value\n" * 700 + "EOF\n"
        cases = {"8 KiB heredoc and 10 sudo lines": heredoc + "".join(f"sudo install -m 644 f{n} /etc/f{n}\n" for n in range(10)),
                 "15 KiB heredoc and 3 sudo lines": "x = 1\n" * 2500 + "sudo tee a\nsudo chmod 600 a\nsudo chown root a\n",
                 "200 apt-get lines": "sudo apt-get install -y x;\n" * 200,
                 "100 systemctl lines": "sudo systemctl restart foo;\n" * 100,
                 "64 KiB script, 50 wrappers": ("echo " + "y" * 60 + "\n") * 1000 + "sudo ls\n" * 50}
        for name, command in cases.items():
            with self.subTest(name):
                ev = self.evaluate(command + f"{K} x")
                self.assertEqual((ev.refusal, ev.kinds["r"]), (None, Kind.DIRECT))

    def test_the_work_budget_is_a_cap_of_its_own_with_its_own_message(self) -> None:
        with mock.patch("scanner.MAX_VARIANT_BYTES", 100):
            ev = self.evaluate("echo x\nsudo " + "y" * 200 + " a b c\n")
        self.assertIn("unwraps into too much command text to parse", ev.refusal or "")

    def test_a_direct_hit_stands_when_the_variants_are_limited(self) -> None:
        ev = self.evaluate(f"{K} x; " + "sudo " * 2100 + "ls")
        self.assertEqual((ev.kinds["r"], ev.refusal_kind is not None), (Kind.DIRECT, True))

    def test_shell_strings_and_variants_have_separate_budgets(self) -> None:
        many = "; ".join(f"bash -c 'echo {n}'" for n in range(60))
        ev = self.evaluate(many + "; sudo " * 40 + K + " x")
        self.assertEqual((ev.kinds["r"], ev.refusal), (Kind.WRAPPED, None))

    def test_scripts_beyond_the_caps_name_their_cause(self) -> None:
        cases = ((nest(f"{K} x", 9), Limit.DEPTH), ("; ".join(f"bash -c 'echo {n}'" for n in range(80)), Limit.UNITS),
                 (nest("x" * 100_000, 4), Limit.SIZE), ("eval " * 40 + K + " x", Limit.DEPTH))
        import scanner
        from ast_grep_py import SgRoot  # noqa: F401

        configs = {"r": scanner.rulebuilder.configs_of(rule_of({"pattern": f"{K} $$$"}))}
        for command, limit in cases:
            with self.subTest(limit=limit):
                self.assertEqual(scanner.Scanner(configs, wrappers.DEFAULTS).run(command).limit, limit)
        clean = scanner.Scanner(configs, wrappers.DEFAULTS).run("bash -c 'true'; " * 600)
        self.assertIsNone(clean.limit)


class Text(AstIsolated):
    def verdict(self, command: str, ast: dict[str, Any] | None = None) -> Kind | None:
        return matching.evaluate(command, {"r": rule_of(ast or {"pattern": f"{K} $$$"})}).kinds["r"]

    def test_offsets_survive_multibyte_text_before_the_region(self) -> None:
        for prefix in ("echo 'é é é'", "echo 日本語 \U0001F600", "x=é"):
            self.assertEqual(self.verdict(f"{prefix} && echo $({K} x)"), "wrapped", prefix)
            self.assertEqual(self.verdict(f"{prefix}; {K} x"), "direct", prefix)
            self.assertEqual(self.verdict(f"{prefix}; sudo {K} x"), "wrapped", prefix)
            self.assertEqual(self.verdict(f"{prefix}; bash -c '{K} x'"), "wrapped", prefix)

    def test_unusual_characters_reach_the_engine_intact(self) -> None:
        for text in ("echo é\U0001F600", "echo a b", "echo a\u0085b", "echo \"q\" 'r' \\s"):
            self.assertEqual(self.verdict(text, {"pattern": text}), "direct", text)

    def test_nul_and_lone_surrogates_do_not_break_the_engine(self) -> None:
        self.assertEqual(self.verdict(f"echo a\x00b; {K} x"), "direct")
        self.assertEqual(self.verdict(f"echo \ud800; {K} x"), "direct")

    def test_the_hit_range_is_in_characters(self) -> None:
        import rulebuilder
        import scanner

        configs = {"r": rulebuilder.configs_of(rule_of({"pattern": f"{K} $$$"}))}
        hit = scanner.Scanner(configs, ()).run(f"echo é; {K} x").hits[0]
        self.assertEqual((hit.kind, hit.start, hit.end), (Kind.DIRECT, 8, 8 + len(f"{K} x")))

    def test_a_wrapped_bare_name_after_a_syntax_error_tree_is_still_found(self) -> None:
        rule = policy.Rule.from_json({"match": {"program": [K, "killall"]}, "message": "m"})
        cases = {f"{{ ; }}; xargs -r {K}": "wrapped", "{ ; }; sudo killall": "wrapped", f"{{ ; }}; sudo -n {K}": "wrapped",
                 f"{{ ; }}; nohup {K}": "wrapped", f"{{ ; }}; {K}": "direct", f"{{ ; }}; sudo {K} x": "wrapped",
                 f"echo 'x; {K} y": None, f"echo {K} 'unterminated": None, f"{{ ; }}; echo {K}": None,
                 f"if x; then echo {K}": None, f"man {K} 'x": None}
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)

    def test_unquote_one_shell_word(self) -> None:
        import scanner

        for word, text in (("'a b'", "a b"), ('"a \\"b\\" \\$x \\\\"', 'a "b" $x \\'), ("pkill", "pkill"), ("''", ""),
                           ("'bash -c '\"'\"'x'\"'\"''", "bash -c 'x'"), ("a\\ b", "a b"), ('"x"\'y\'z', "xyz")):
            self.assertEqual(scanner.unquote(word), text, word)

    def test_tree_rows_keep_the_leaf_rules(self) -> None:
        units, limit = matching.tree("echo \"\" 'a b' x")
        shown = {(kind, text) for _, kind, text in units[0].rows}
        self.assertIsNone(limit)
        self.assertIn(("string", None), shown)
        self.assertIn(("raw_string", "'a b'"), shown)
        self.assertIn(("command_name", "echo"), shown)

    def test_a_broken_tree_is_reported_not_hidden(self) -> None:
        for command in ('echo "unterminated', "a |", "echo $(", "if a; then b"):
            self.assertTrue(matching.tree(command)[0][0].broken, command)
        for command in ("cat <<EOF\nEOF", "x=", "echo ''", "a && b", "f() { :; }", "echo $((1+2))", "cat <<EOF\n$(a)\nEOF"):
            self.assertFalse(matching.tree(command)[0][0].broken, command)


if __name__ == "__main__":
    unittest.main()
