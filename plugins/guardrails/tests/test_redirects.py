from __future__ import annotations  # noqa: I001

import json
from typing import Any, ClassVar

from helpers import AstIsolated

import matching
import policy
import rulebuilder

STATUS = {"regex": "^(&&|\\|\\|)$"}
LIST = {"kind": "list"}


def rule_of(match: dict[str, Any]) -> policy.Rule:
    return policy.Rule.from_json({"match": match, "message": "m", "wrappers": False})


class Hits(AstIsolated):
    def assert_hits(self, match: dict[str, Any], expected: dict[str, bool]) -> None:
        rule = rule_of(match)
        for command, hit in expected.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"] is not None, hit)


class CommandSiblings(Hits):
    PGREP: ClassVar[dict[str, Any]] = {"command": "pgrep", "inside": LIST, "precedes": STATUS}

    def test_a_redirect_around_the_command_does_not_hide_it_from_its_siblings(self) -> None:
        self.assert_hits(self.PGREP, {
            "pgrep -f X || echo gone": True, "pgrep -x X && echo up": True,
            "pgrep -f X >/dev/null || echo gone": True, "pgrep -x X 2>/dev/null && echo up": True,
            "pgrep -f X >/dev/null 2>&1 || echo gone": True, "pgrep -f X &>/dev/null && echo up": True,
            "pgrep -f X 2>&1 >/dev/null || echo gone": True, "pgrep -f X > out.txt && cat out.txt": True,
            "pgrep -f X < in || echo gone": True,
            "pgrep -fl X >/dev/null 2>&1; echo done": False, "pgrep -f X > out.txt; cat out.txt": False,
            "pgrep -f X; echo done": False, "echo a && pgrep -f X >/dev/null": False, "pgrep -f X": False,
            "pgrep -f X >/dev/null": False, "echo pgrep >/dev/null || echo gone": False,
        })

    def test_follows_is_judged_from_the_wrapper_too(self) -> None:
        after_cd = {"command": "git", "follows": {"regex": "^(&&|\\|\\|)$"}, "inside": LIST}
        self.assert_hits(after_cd, {
            "cd x && git pull": True, "cd x && git pull >/dev/null": True, "git pull >/dev/null": False, "git pull; git push": False,
        })

    def test_a_list_of_any_shape_with_or_without_a_redirect(self) -> None:
        self.assert_hits(self.PGREP, {
            "a && pgrep x >/dev/null || b": False, "a; pgrep x >/dev/null; b": False,
            "pgrep x >/dev/null && b && c": True, "for i in 1 2; do pgrep x >/dev/null || break; done": True,
            "{ pgrep x >/dev/null || echo gone; }": True, "(pgrep x 2>/dev/null || echo gone)": True,
        })

    def test_relations_on_the_target_look_through_a_redirect_as_well(self) -> None:
        fed = {"command": "xargs", "args": r"\bkill\b", "inside": {"kind": "pipeline"},
               "follows": {"command": ["ps", "lsof"], "stopBy": "end"}}
        self.assert_hits(fed, {
            "lsof -ti :3000 | xargs kill": True, "lsof -ti :3000 2>/dev/null | xargs kill": True,
            "ps aux 2>&1 | head | xargs kill": True, "lsof -ti :3000 | xargs kill >/dev/null": True,
            "lsof -ti :3000 | xargs kill 2>/dev/null | cat": True, "echo 1 2>/dev/null | xargs kill": False,
            "lsof -ti :3000 >/dev/null; xargs kill": False,
        })

    def test_a_pipeline_stage_with_a_redirect_is_a_wrapper_child_of_the_pipeline(self) -> None:
        shell = {"command": ["sh", "bash"], "inside": {"kind": "pipeline"},
                 "follows": {"command": ["curl", "wget"], "stopBy": "end"}}
        self.assert_hits(shell, {
            "curl x | sh": True, "curl x 2>/dev/null | sh": True, "curl x | sh >/dev/null": True,
            "curl x | tee f | sh -s 2>/dev/null": True, "curl x -o f 2>/dev/null; sh f": False,
            "echo x 2>/dev/null | sh": False,
        })

    def test_nested_under_has_inside_any_all_and_not(self) -> None:
        self.assert_hits({"kind": "list", "has": {"command": "pgrep", "precedes": STATUS}}, {
            "pgrep x >/dev/null || y": True, "pgrep x || y": True, "pgrep x; y": False})
        self.assert_hits({"command": "echo", "inside": {"kind": "list", "stopBy": "end",
                                                        "has": {"command": "pgrep", "precedes": STATUS}}}, {
            "pgrep x 2>/dev/null || echo gone": True, "pgrep x; echo gone": False})
        self.assert_hits({"any": [{"command": "pgrep", "precedes": STATUS}, {"command": "kill"}]}, {
            "pgrep x >/dev/null && y": True, "kill 1": True, "pgrep x >/dev/null; y": False})
        self.assert_hits({"all": [{"command": "pgrep"}, {"precedes": STATUS}]}, {"pgrep x || y": True})
        self.assert_hits({"command": "pgrep", "not": {"inside": {"kind": "list"}}, "precedes": STATUS}, {
            "pgrep x || y": False})
        self.assert_hits({"command": "pgrep", "inside": LIST, "precedes": {"command": "echo"}, "not": {
            "follows": {"command": "cd", "stopBy": "end"}}}, {
            "pgrep x >/dev/null; echo a": False, "pgrep x >/dev/null && echo a": False})

    def test_a_not_of_relations_follows_the_statement(self) -> None:
        rule = {"command": "git", "inside": LIST, "precedes": STATUS,
                "not": {"follows": {"command": "cd", "stopBy": "end"}}}
        self.assert_hits(rule, {
            "git pull >/dev/null && x": True, "git pull && x": True, "cd d && git pull && x": False,
            "cd d && git pull >/dev/null && x": False, "cd d >/dev/null && git pull >/dev/null && x": False})

    def test_a_sibling_target_in_a_list_of_alternatives_or_negated(self) -> None:
        self.assert_hits({"kind": "command", "regex": "^echo", "follows": {"any": [
            {"command": "ps"}, {"command": "pgrep"}], "stopBy": "end"}}, {
            "pgrep x >/dev/null; echo a": True, "ps >/dev/null; echo a": True, "ls; echo a": False})
        self.assert_hits({"kind": "command", "regex": "^echo", "not": {"follows": {"command": "pgrep", "stopBy": "end"}}}, {
            "ls; echo a": True, "pgrep x >/dev/null; echo a": False, "pgrep x; echo a": False})

    def test_mixed_conditions_under_not_next_to_relations_are_refused(self) -> None:
        mixed = {"command": "git", "precedes": STATUS, "not": {"any": [{"follows": {"command": "cd"}},
                                                                       {"has": {"regex": "^-f$"}}]}}
        with self.assertRaisesRegex(policy.Invalid, "mixes relations"):
            rulebuilder.expanded(mixed)

    def test_without_sibling_relations_the_command_is_unchanged(self) -> None:
        plain = rulebuilder.expanded({"command": "pgrep", "inside": LIST})
        self.assertNotIn("redirected_statement", str(plain))
        self.assertIn("redirected_statement", str(rulebuilder.expanded({"command": "pgrep", "precedes": STATUS})))


class Statement(Hits):
    PIPELINE: ClassVar[dict[str, Any]] = {"statement": {"kind": "pipeline"}, "precedes": STATUS}

    def test_a_node_and_its_wrapper_are_one_statement(self) -> None:
        self.assert_hits(self.PIPELINE, {
            "a | b && c": True, "a | b >/dev/null && c": True, "a | b 2>&1 || c": True, "a | b; c": False,
            "a | b >/dev/null; c": False, "a && b | c": False, "a | b": False, "a && c | d >f": False,
        })

    def test_a_command_statement(self) -> None:
        self.assert_hits({"statement": {"command": "pgrep"}, "precedes": STATUS}, {
            "pgrep x || y": True, "pgrep x >/dev/null || y": True, "pgrep x; y": False, "pgrep x >f; y": False})

    def test_the_statement_is_the_wrapper_so_has_sees_its_redirects(self) -> None:
        loud = {"statement": {"command": "cargo"}, "has": {"redirect": {"fd": 2, "to": "/dev/null"}}}
        self.assert_hits(loud, {
            "cargo build 2>/dev/null": True, "cargo build >/dev/null 2>/dev/null": True, "cargo build >/dev/null": False,
            "cargo build": False})

    def test_a_group_or_a_loop_can_be_the_statement(self) -> None:
        self.assert_hits({"statement": {"kind": "compound_statement"}, "has": {"redirect": {"fd": 1}}}, {
            "{ a; b; } >f": True, "{ a; b; }": False})
        self.assert_hits({"statement": {"kind": "while_statement"}, "has": {"redirect": {}}}, {
            "while a; do b; done >f": True, "while a; do b; done": False})

    def test_an_empty_or_malformed_statement_is_refused(self) -> None:
        for bad in ({}, "pgrep", ["x"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(policy.Invalid, "statement"):
                rulebuilder.expanded({"statement": bad})


class Redirect(Hits):
    def hit(self, spec: dict[str, Any], expected: dict[str, bool]) -> None:
        self.assert_hits({"statement": {"kind": "command"}, "has": {"redirect": spec}}, expected)

    def test_the_target_in_any_spelling(self) -> None:
        self.hit({"to": "/dev/null"}, {
            "a >/dev/null": True, "a > /dev/null": True, 'a >"/dev/null"': True, "a >'/dev/null'": True,
            "a 2>/dev/null": True, "a &>/dev/null": True, "a >/dev/nullx": False, "a >out": False, "a": False})

    def test_a_regex_target(self) -> None:
        self.hit({"to": {"regex": r"^/(dev|tmp)/"}}, {"a >/tmp/x": True, "a 2>/dev/null": True, "a >out": False})

    def test_the_stream(self) -> None:
        self.hit({"fd": 1}, {"a >f": True, "a 1>f": True, "a >>f": True, "a &>f": True, "a >&f": True, "a >&2": True,
                             "a 2>f": False, "a <f": False, "a 2>&1": False})
        self.hit({"fd": 2}, {"a 2>f": True, "a 2>>f": True, "a 2>&1": True, "a &>f": True, "a >&f": True,
                             "a >f": False, "a 1>f": False, "a >&2": False, "a 3>f": False})
        self.hit({"fd": "&"}, {"a &>f": True, "a &>>f": True, "a >&f": True, "a >f": False, "a 2>f": False,
                               "a >&2": False, "a 2>&1": False})
        self.hit({"fd": 0}, {"a <f": True, "a 0<f": True, "a <&3": True, "a >f": False})
        self.hit({"fd": 3}, {"a 3>f": True, "a 3>&1": True, "a >f": False})

    def test_the_operator(self) -> None:
        for op, yes, no in ((">", "a >f", "a >>f"), (">>", "a 2>>f", "a >f"), ("<", "a <f", "a >f"),
                            (">&", "a 2>&1", "a >f"), ("&>", "a &>f", "a >f"), ("&>>", "a &>>f", "a &>f"),
                            (">|", "a >|f", "a >f"), (">&-", "a >&-", "a >&1")):
            with self.subTest(op=op):
                self.hit({"op": op}, {yes: True, no: False})

    def test_fields_combine(self) -> None:
        self.hit({"fd": 2, "op": ">&", "to": "1"}, {"a 2>&1": True, "a 2>&3": False, "a >&1": False, "a 2>f": False})
        self.hit({"fd": 2, "to": "/dev/null"}, {"a 2>/dev/null": True, "a &>/dev/null": True, "a >/dev/null": False,
                                                 "a 2>f": False})
        self.hit({"op": ">>", "to": {"regex": r"\.log$"}}, {"a >>x.log": True, "a >x.log": False, "a >>x.txt": False})

    def test_any_redirect(self) -> None:
        self.assert_hits({"statement": {"command": "a"}, "has": {"redirect": {}}}, {
            "a >f": True, "a <f": True, "a 2>&1": True, "a <<EOF\nx\nEOF": True, "a": False})

    def test_what_the_atom_matches_is_the_redirect_node(self) -> None:
        self.assert_hits({"redirect": {"fd": 2, "to": "/dev/null"}}, {"a 2>/dev/null": True, "a >/dev/null": False})

    def test_bad_specs_name_the_key_and_the_value(self) -> None:
        for spec, text in (
                ({"fd": 11.5}, "redirect.fd"), ({"fd": "x"}, "redirect.fd"), ({"fd": True}, "redirect.fd"),
                ({"fd": -1}, "redirect.fd"), ({"op": "=>"}, "redirect.op"), ({"op": 1}, "redirect.op"),
                ({"to": ""}, "redirect.to"), ({"to": 3}, "redirect.to"), ({"to": {"rx": "x"}}, "redirect.to"),
                ({"to": {"regex": 1}}, "redirect.to"), ({"path": "x"}, "'path'"), ("x", "redirect"), ([], "redirect")):
            with self.subTest(spec=spec), self.assertRaisesRegex(policy.Invalid, text):
                rulebuilder.expanded({"redirect": spec})


class Discards(Hits):
    SHAPES: ClassVar[dict[str, tuple[bool, bool]]] = {
        "a >/dev/null": (True, False),
        "a 1>/dev/null": (True, False),
        "a >>/dev/null": (True, False),
        "a >|/dev/null": (True, False),
        "a 2>/dev/null": (False, True),
        "a 2>>/dev/null": (False, True),
        "a &>/dev/null": (True, True),
        "a &>>/dev/null": (True, True),
        "a >&/dev/null": (True, True),
        'a >"/dev/null"': (True, False),
        "a >/dev/null 2>&1": (True, True),
        "a 2>&1 >/dev/null": (True, False),
        "a 2>&1 >>/dev/null": (True, False),
        "a >/dev/null 2>/dev/null": (True, True),
        "a 2>/dev/null >/dev/null": (True, True),
        "a >/dev/null 2>&1 >out": (False, True),
        "a >/dev/null >out": (False, False),
        "a >out >/dev/null": (True, False),
        "a 2>err >/dev/null 2>&1": (True, True),
        "a 2>/dev/null 1>&2": (True, True),
        "a 2>/dev/null >&2": (True, True),
        "a 2>/dev/null 1>&2 2>&1": (True, True),
        "a >/dev/null 2>&1 2>err": (True, False),
        "a 2>/dev/null 2>&1": (False, False),
        "a >/dev/null 2>&-": (True, False),
        "a >&- >/dev/null": (True, False),
        "a 3>&1 >/dev/null": (True, False),
        "a >/dev/null 2>&1 <in": (True, True),
        "a 2>&1": (False, False),
        "a >&2": (False, False),
        "a >out": (False, False),
        "a >/dev/nullx": (False, False),
        "a 2>/dev/null >out": (False, True),
        "a": (False, False),
        "a | b": (False, False),
    }

    def test_each_stream_follows_the_redirects_in_the_order_the_shell_applies_them(self) -> None:
        for stream, index in (("stdout", 0), ("stderr", 1)):
            self.assert_hits({"discards": stream}, {cmd: want[index] for cmd, want in self.SHAPES.items()})

    def test_all_needs_both_streams(self) -> None:
        self.assert_hits({"discards": "all"}, {cmd: all(want) for cmd, want in self.SHAPES.items()})

    def test_it_is_a_property_of_the_wrapper_in_any_position(self) -> None:
        loud = {"statement": {"command": "cargo"}, "discards": "all"}
        self.assert_hits(loud, {
            "cargo build >/dev/null 2>&1": True, "cargo build >/dev/null 2>&1 && echo ok": True,
            "x; cargo build &>/dev/null; y": True, "cargo build 2>&1 >/dev/null": False, "cargo build": False,
            "cargo build >/dev/null": False})
        self.assert_hits({"statement": {"kind": "pipeline"}, "discards": "stdout"}, {
            "a | b >/dev/null": True, "a | b 2>&1 >/dev/null": True, "a | b 2>/dev/null": False, "a | b": False})

    def test_the_rule_compiles_to_static_ast_grep(self) -> None:
        from ast_grep_py import SgRoot

        config = rulebuilder.built({"discards": "all"})
        json.dumps(config)
        found = SgRoot("a >/dev/null 2>&1", "bash").root().find({"rule": config})
        self.assertIsNotNone(found)
        self.assertEqual(found.kind() if found else None, "redirected_statement")

    def test_bad_values_are_refused(self) -> None:
        for bad in ("both", "out", True, ["all"], None, 1):
            with self.subTest(bad=bad), self.assertRaisesRegex(policy.Invalid, "discards"):
                rulebuilder.expanded({"discards": bad})


class Reports(Hits):
    def test_the_hit_node_of_a_wrapped_command_is_the_command(self) -> None:
        rule = policy.Rule.from_json({"match": {"command": "pgrep", "inside": LIST, "precedes": STATUS}, "message": "m",
                                      "messages": [{"text": "wrapped body", "when": {"matches": {"command": "pgrep"}}}]})
        ev = matching.evaluate("pgrep -f X >/dev/null || echo gone", {"r": rule})
        self.assertIsNotNone(ev.kinds["r"])
        self.assertEqual(ev.details["r"].case, 0)

    def test_the_new_atoms_are_valid_in_message_cases(self) -> None:
        rule = policy.Rule.from_json({"match": {"command": "cargo"}, "message": "m", "messages": [
            {"text": "quiet", "when": {"matches": {"statement": {"command": "cargo"}, "discards": "all"}}}]})
        self.assertEqual(matching.check({"r": rule}), {})
