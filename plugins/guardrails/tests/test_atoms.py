from __future__ import annotations  # noqa: I001

import json
from typing import Any, ClassVar

from helpers import AstIsolated

import matching
import policy

K = "pk" + "ill"


def rule_of(match: dict[str, Any], **extra: Any) -> policy.Rule:
    return policy.Rule.from_json({"match": match, "message": "m", **extra})


def keys_of(node: object) -> set[str]:
    if isinstance(node, list):
        return {key for item in node for key in keys_of(item)}
    if not isinstance(node, dict):
        return set()
    return set(node) | {key for value in node.values() for key in keys_of(value)}


class CommandAtom(AstIsolated):
    def test_the_name_matches_in_any_spelling_and_through_every_look_through(self) -> None:
        self.assert_kinds(rule_of({"command": K}), {
            f"{K} x": "direct", f"'{K}' x": "direct", f"/usr/bin/{K} x": "direct", f"FOO=1 {K} x": "direct",
            f"A=1 B=2 C=3 D=4 {K} x": "direct", K: "direct", f"sudo {K} x": "wrapped", f"bash -c '{K} x'": "wrapped",
            f"bash -c \"sh -c '{K} x'\"": "wrapped", f"bash <<EOF\n{K} x\nEOF": "wrapped", f"echo $({K} x)": "wrapped",
            f"echo {K}": None, f"man {K}": None, f"cat <<'EOF'\n{K} x\nEOF": None, f"{K}s x": None, "PKILL=1 ls": None,
        })

    def test_a_name_list_and_args_keep_the_semantics_of_a_regex_over_the_whole_command(self) -> None:
        self.assert_kinds(rule_of({"command": [K, "killall"]}), {"killall x": "direct", f"{K} x": "direct", "kill 1": None})
        nine = rule_of({"command": "kill", "args": r"(^|\s)-9(\s|$)"})
        self.assert_kinds(nine, {"kill -9 1": "direct", "/bin/kill -9 1": "direct", "FOO=1 kill -9 1": "direct",
                                 "sudo kill -9 1": "wrapped", "kill 1": None, "kill -99 1": None, "echo kill -9": None})

    def test_atoms_compose_with_all_any_not_inside_and_follows(self) -> None:
        unguarded = rule_of({"command": "rm", "not": {"inside": {"kind": "if_statement", "stopBy": "end"}}})
        self.assert_kinds(unguarded, {"rm x": "direct", "if true; then rm x; fi": None, "sudo rm x": "wrapped"})
        either = rule_of({"any": [{"command": "shred"}, {"command": "rm", "args": r"(^|\s)-[a-z]*P"}]})
        self.assert_kinds(either, {"shred f": "direct", "rm -P f": "direct", "rm f": None})
        piped = rule_of({"all": [{"command": "sh"}, {"inside": {"kind": "pipeline"}}],
                         "follows": {"command": ["curl", "wget"], "stopBy": "end"}})
        self.assert_kinds(piped, {"curl x | sh": "wrapped", "/usr/bin/wget -qO- x | sh": "wrapped",
                                  "'curl' x | sh": "wrapped", "sh x": None, "cat f | sh": None})
        after_cd = rule_of({"command": "git", "follows": {"command": "cd", "stopBy": "end"}, "inside": {"kind": "list"}})
        self.assert_kinds(after_cd, {"cd x && git pull": "direct", "git pull": None})

    def test_xargs_kill_fed_by_a_lookup_in_any_spelling(self) -> None:
        rule = rule_of({"command": "xargs", "args": r"\bkill\b", "inside": {"kind": "pipeline"},
                        "follows": {"command": ["ps", "pidof", "lsof"], "stopBy": "end"}})
        self.assert_kinds(rule, {
            "ps x | /usr/bin/xargs -r kill": "wrapped", "'ps' x | xargs kill": "wrapped",
            "lsof -ti :3000 | xargs kill -9": "wrapped", "pidof x | xargs -r kill": "wrapped",
            "echo 4242 | xargs kill": None, "ps aux | grep x": None, "ps x | xargs echo": None})
        handwritten = rule_of({"kind": "command", "regex": r"^xargs\b.*\bkill\b", "inside": {"kind": "pipeline"},
                               "follows": {"kind": "command", "has": {"field": "name", "regex": "^(ps|pidof|lsof)$"},
                                           "stopBy": "end"}})
        self.assert_kinds(handwritten, {"ps x | /usr/bin/xargs -r kill": None, "'ps' x | xargs kill": None})


class AssignmentAtom(AstIsolated):
    def test_a_command_prefix_assignment_by_name(self) -> None:
        rule = rule_of({"kind": "command", "has": {"assignment": {"name": "LD_PRELOAD"}}})
        self.assert_kinds(rule, {"LD_PRELOAD=/tmp/x.so ls": "direct", "A=1 LD_PRELOAD=x ls": "direct",
                                 "sudo LD_PRELOAD=x ls": "wrapped", "env LD_PRELOAD=x ls": "wrapped",
                                 "bash -c 'LD_PRELOAD=x ls'": "wrapped", "export LD_PRELOAD=x": None,
                                 "ls LD_PRELOAD=x": None, "LD_PRELOADED=x ls": None, "echo LD_PRELOAD=x": None})

    def test_value_is_exact_or_quoted_and_regexes_are_used_as_written(self) -> None:
        exact = rule_of({"assignment": {"name": "LD_PRELOAD", "value": "x.so"}})
        self.assert_kinds(exact, {"LD_PRELOAD=x.so ls": "direct", "LD_PRELOAD='x.so' ls": "direct",
                                  'LD_PRELOAD="x.so" ls': "direct", "export LD_PRELOAD=x.so": "direct",
                                  "LD_PRELOAD=xxso ls": None, "LD_PRELOAD=y.so ls": None})
        family = rule_of({"assignment": {"name": {"regex": "^(LD_|DYLD_)"}, "value": {"regex": r"\.(so|dylib)"}}})
        self.assert_kinds(family, {"DYLD_INSERT_LIBRARIES=/a.dylib x": "direct", "LD_LIBRARY_PATH=/lib x": None})
        self.assert_kinds(rule_of({"assignment": {}}), {"A=1 ls": "direct", "ls": None})


class WrapperAtom(AstIsolated):
    def test_the_wrapper_words_themselves_in_any_spelling(self) -> None:
        self.assert_kinds(rule_of({"wrapper": True}), {"sudo ls": "direct", "/usr/bin/env A=1 ls": "direct",
                                                     "xargs rm": "direct", "ls | xargs rm": "wrapped", "ls": None,
                                                     "echo sudo": None})
        self.assert_kinds(rule_of({"wrapper": ["sudo", "doas"]}), {"doas ls": "direct", "'sudo' ls": "direct",
                                                                 "env ls": None})

    def test_sudo_curl_piped_into_a_shell(self) -> None:
        rule = rule_of({"wrapper": ["sudo"], "has": {"kind": "word", "regex": "^curl$"},
                        "inside": {"kind": "pipeline", "has": {"command": ["sh", "bash"]}}})
        self.assert_kinds(rule, {"sudo curl x | sh": "wrapped", "/usr/bin/sudo curl x | bash": "wrapped",
                                 "curl x | sh": None, "sudo ls | sh": None, "sudo curl x > f": None})


class ViaAtom(AstIsolated):
    COMMANDS: ClassVar[list[str]] = [f"{K} x", f"sudo {K} x", f"ssh h {K} x", f"bash -c '{K} x'", f"sudo ssh h '{K} x'",
                                     f"sudo bash -c '{K} x'", f"ssh a 'ssh b {K} x'", f"watch {K} x", f"{K} x | cat"]

    def test_a_unit_is_matched_by_the_names_it_was_reached_through(self) -> None:
        cases = {
            "ssh": {1: None, 2: "wrapped", 3: None, 4: "wrapped", 5: None, 6: "wrapped"},
            "sudo": {1: "wrapped", 4: "wrapped", 5: "wrapped"},
            "bash": {3: "wrapped", 5: "wrapped"},
            "wrapper": {1: "wrapped", 4: "wrapped", 5: "wrapped"},
            "runner": {2: "wrapped", 3: "wrapped", 4: "wrapped", 5: "wrapped", 6: "wrapped", 7: "wrapped"},
            "watch": {7: "wrapped"},
        }
        for name, expected in cases.items():
            self.assert_kinds(rule_of({"command": K, "via": name}),
                              {c: expected.get(i) for i, c in enumerate(self.COMMANDS)})
        self.assert_kinds(rule_of({"command": K, "via": ["ssh", "sudo"]}),
                          {c: "wrapped" if i in (1, 2, 4, 5, 6) else None for i, c in enumerate(self.COMMANDS)})

    def test_via_composes_under_not_any_and_all(self) -> None:
        self.assert_kinds(rule_of({"command": K, "not": {"via": "ssh"}}), {
            f"{K} x": "direct", f"sudo {K} x": "wrapped", f"bash -c '{K} x'": "wrapped", f"ssh h {K} x": None,
            f"sudo ssh h '{K} x'": None, f"ssh a 'ssh b {K} x'": None, f"{K} x | cat": "wrapped"})
        self.assert_kinds(rule_of({"command": K, "not": {"any": [{"via": "ssh"}, {"via": "sudo"}]}}), {
            f"{K} x": "direct", f"sudo {K} x": None, f"ssh h {K} x": None, f"bash -c '{K} x'": "wrapped"})
        self.assert_kinds(rule_of({"command": K, "all": [{"via": "wrapper"}, {"not": {"via": "ssh"}}]}), {
            f"sudo {K} x": "wrapped", f"sudo ssh h '{K} x'": None, f"{K} x": None, f"ssh h {K} x": None})
        self.assert_kinds(rule_of({"any": [{"command": K, "via": "ssh"}, {"command": "ls"}]}), {
            f"ssh h {K} x": "wrapped", f"{K} x": None, "ls": "direct"})

    def test_every_wrapper_crossed_is_in_the_chain(self) -> None:
        self.assert_kinds(rule_of({"command": K, "via": "env"}), {
            f"sudo env {K} x": "wrapped", f"env sudo {K} x": "wrapped", f"bash -c 'sudo env A=1 {K} x'": "wrapped",
            f"sudo {K} x": None})

    def test_a_message_case_does_not_depend_on_which_chain_is_scanned_first(self) -> None:
        rule = policy.Rule.from_json({"match": {"command": K}, "message": "plain", "messages": [
            {"when": {"matches": {"via": "ssh"}}, "text": "remote"}]})
        for command in (f"bash -c '{K} x'; ssh h {K} x", f"ssh h {K} x; bash -c '{K} x'"):
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).details["r"].case, 0)

    def test_the_first_declared_case_that_holds_on_any_unit_wins_whatever_the_names_or_the_order(self) -> None:
        for first, second in (("sudo", "ssh"), ("ssh", "sudo")):
            rule = policy.Rule.from_json({"match": {"command": K}, "message": "plain", "messages": [
                {"when": {"matches": {"via": first}}, "text": first}, {"when": {"matches": {"via": second}}, "text": second}]})
            for command in (f"ssh h '{K} x'; sudo {K} x", f"sudo {K} x; ssh h '{K} x'"):
                with self.subTest(first=first, command=command):
                    self.assertEqual(matching.evaluate(command, {"r": rule}).details["r"].case, 0)

    def test_the_captures_come_from_the_unit_that_satisfied_the_chosen_case(self) -> None:
        match = {"command": K, "has": {"capture": {"kind": "word", "regex": "^[a-z]$"}, "name": "ARG"}}
        rule = policy.Rule.from_json({"match": match, "message": "plain {ARG}", "messages": [
            {"when": {"matches": {"via": "ssh"}}, "text": "remote {ARG}"}]})
        for command in (f"sudo {K} a; ssh h {K} b", f"ssh h {K} b; sudo {K} a"):
            with self.subTest(command=command):
                detail = matching.evaluate(command, {"r": rule}).details["r"]
                self.assertEqual((detail.case, detail.captures), (0, {"ARG": "b"}))

    def test_a_directly_typed_command_has_an_empty_chain(self) -> None:
        self.assert_kinds(rule_of({"command": K, "via": ["wrapper", "runner"]}), {f"{K} x": None})
        self.assert_kinds(rule_of({"command": K, "not": {"via": ["wrapper", "runner"]}}), {f"{K} x": "direct"})

    def test_via_works_in_message_cases_and_next_to_wrappers_false(self) -> None:
        rule = policy.Rule.from_json({"match": {"command": K}, "message": "plain", "messages": [
            {"when": {"matches": {"via": "ssh"}}, "text": "remote"}]})
        for command, case in ((f"ssh h {K} x", 0), (f"sudo {K} x", None)):
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).details["r"].case, case)
        self.assert_kinds(rule_of({"command": K, "not": {"via": "ssh"}}, wrappers=False), {
            f"sudo {K} x": None, f"ssh h {K} x": None, f"bash -c '{K} x'": "wrapped", f"{K} x": "direct"})

    def test_unknown_names_and_empty_lists_are_rejected(self) -> None:
        for value in ("rsync", [], ["ssh", "nope"], True, "sh -c", ""):
            with self.subTest(value=value), self.assertRaises(policy.Invalid):
                rule_of({"command": K, "via": value})


class FlagAtom(AstIsolated):
    def test_a_short_flag_is_found_in_any_cluster_of_letters_and_a_long_one_with_or_without_a_value(self) -> None:
        self.assert_kinds(rule_of({"command": "tail", "flag": "f"}), {
            "tail -f x": "direct", "tail -nf x": "direct", "tail -fq x": "direct", "tail x -f": "direct",
            "/usr/bin/tail -f x": "direct", "FOO=1 tail -f x": "direct", "tail -f x > o": "direct",
            "sudo tail -f x": "wrapped", "tail -F x": None, "tail -n 5 x": None, "tail -n5f x": "direct",
            "tail -fn5 x": "direct", "tail --f x": None, "tail f": None, "tail '-f' x": "direct",
            "tail \"-nf\" x": "direct", "tail -n5 x": None, "tail -5 x": None, "echo -f": None, "tail -f": "direct"})
        self.assert_kinds(rule_of({"command": "tail", "flag": "follow"}), {
            "tail --follow x": "direct", "tail --follow=name x": "direct", "tail --follow='a b' x": "direct",
            "tail --followx x": None, "tail --follo x": None, "tail -follow x": None, "tail -f x": None})

    def test_nothing_after_a_double_dash_word_is_a_flag(self) -> None:
        self.assert_kinds(rule_of({"command": "tail", "flag": ["f", "follow"]}), {
            "tail -f -- x": "direct", "tail -- -f": None, "tail x -- -f": None, "tail -- --follow": None,
            "tail --follow -- -f": "direct"})

    def test_a_list_is_any_of_its_names_and_distinct_options_combine_with_all_any_not(self) -> None:
        self.assert_kinds(rule_of({"command": "tail", "flag": ["f", "F", "follow"]}), {
            "tail -f x": "direct", "tail -n0 -F x": "direct", "tail --follow x": "direct", "tail x": None,
            "tail -n 3 x": None})
        both = rule_of({"command": "x", "all": [{"flag": "a"}, {"flag": "b"}]})
        self.assert_kinds(both, {"x -a -b": "direct", "x -ab": "direct", "x -ba": "direct", "x -a": None})
        self.assert_kinds(rule_of({"command": "x", "not": {"flag": "a"}}), {"x -b": "direct", "x -ba": None})
        self.assert_kinds(rule_of({"command": "x", "any": [{"flag": "a"}, {"flag": "verbose"}]}), {
            "x -a": "direct", "x --verbose": "direct", "x -b": None})

    def test_flag_alone_matches_any_command_that_carries_it(self) -> None:
        self.assert_kinds(rule_of({"flag": "v"}), {"ls -v": "direct", "grep -iv x": "direct", "ls": None,
                                                   "echo -v": "direct", "ls -- -v": None})

    def test_the_cluster_reading_is_coarse_about_values_and_digits(self) -> None:
        self.assert_kinds(rule_of({"command": "ssh", "flag": "o"}), {"ssh -sofoo h": "direct", "ssh -p22 h": None})

    def test_names_with_a_dash_or_of_the_wrong_shape_and_empty_lists_are_rejected(self) -> None:
        for value in ("-f", "--follow", [], "", "1", "a b", "fo o", ["f", "-F"], 3, [3], ["f", ""], "a=b"):
            with self.subTest(value=value), self.assertRaises(policy.Invalid):
                rule_of({"command": "tail", "flag": value})
        with self.assertRaisesRegex(policy.Invalid, "without the dash"):
            rule_of({"flag": "-f"})


class NotThroughWrappers(AstIsolated):
    def test_wrapper_variants_are_skipped_and_every_other_look_through_stays(self) -> None:
        rule = rule_of({"command": K}, wrappers=False)
        self.assert_kinds(rule, {
            f"{K} x": "direct", f"/usr/bin/{K} x": "direct", f"FOO=1 {K} x": "direct", f"a | {K} x": "wrapped",
            f"echo $({K} x)": "wrapped", f"bash -c '{K} x'": "wrapped", f"bash <<EOF\n{K} x\nEOF": "wrapped",
            f"sudo {K} x": None, f"env A=1 {K} x": None, f"xargs {K}": None, f"sudo bash -c '{K} x'": None,
            f"sudo {K} a; {K} b": "direct"})

    def test_a_script_reached_through_a_wrapper_first_is_still_judged_where_it_is_reached_without_one(self) -> None:
        rule = rule_of({"command": K}, wrappers=False)
        self.assert_kinds(rule, {f"sudo bash -c '{K} x'; bash -c 'bash -c \"{K} x\"'": "wrapped"})

    def test_the_wrapped_kind_of_a_pipeline_is_unchanged(self) -> None:
        for wrappers in (True, False):
            with self.subTest(wrappers=wrappers):
                self.assert_kinds(rule_of({"command": K}, wrappers=wrappers), {f"{K} x | cat": "wrapped"})

    def test_the_hook_denies_the_direct_form_and_lets_the_wrapped_one_run(self) -> None:
        self.put(self.gpath, {"rules": {"r": {"match": {"command": K}, "wrappers": False, "message": "No."}}})
        out = self.hook(f"{K} x")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.hook(f"sudo {K} x"))


class RemovedFormat(AstIsolated):
    OLD: ClassVar[dict[str, Any]] = {"match": {"program": [K, "killall"]}, "message": "No kill by name."}

    def test_a_stored_rule_in_the_removed_format_is_named_by_the_hook_status_and_rule_test(self) -> None:
        self.put(self.gpath, {"rules": {"old": self.OLD, "new": {"match": {"command": "strings"}, "message": "No."}}})
        first = self.hook("strings x", session="o1")
        assert first is not None
        self.assertEqual(first["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("rule old is invalid (match.program was removed: write {\"command\": <name or list>}",
                      first["systemMessage"])
        self.assertIsNone(self.hook(f"{K} x", session="o1"))
        self.assertIn("rule old: match.program was removed", self.cli("status", "--problems")[1])
        code, _, err = self.cli("rule", "test", "--id", "old", f"{K} x")
        self.assertEqual(code, 2)
        self.assertIn("match.program was removed", err)

    def test_rule_add_and_test_refuse_the_removed_format_and_bad_atoms(self) -> None:
        for match in ({"program": K}, {"ast": {"pattern": "x $$$"}}, {"regex": "x"}, {"command": "a/b"},
                      {"wrapper": ["ssh"]}, {"assignment": {"name": "A B"}}, {"args": "-9"}):
            rule = json.dumps({"match": match, "message": "m"})
            for argv in (("rule", "add", "r", "--json", rule), ("rule", "test", "--json", rule, "x")):
                with self.subTest(match=match, verb=argv[1]):
                    code, _, err = self.cli(*argv)
                    self.assertEqual(code, 2)
                    self.assertIn("error: ", err)
        self.assertFalse(self.gpath.exists())


class Expansion(AstIsolated):
    def test_atoms_become_plain_ast_grep_rules(self) -> None:
        import rulebuilder

        config = rulebuilder.config_of(rule_of({"not": {"wrapper": True}, "any": [
            {"command": "x", "args": "y"}, {"has": {"assignment": {"name": "A"}}}]}))
        self.assertEqual(keys_of(config) & {"command", "wrapper", "assignment", "args"}, set())
        self.assertEqual(matching.check({"r": rule_of({"command": "x", "has": {"assignment": {}}})}), {})
