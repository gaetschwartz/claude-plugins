from __future__ import annotations  # noqa: I001

import json
from typing import Any

from helpers import AstIsolated, Isolated, plain

import engine
import matching
import messages
import policy
import redirects
import rulebuilder

NAME = "name"
LAST_STAGE = {"kind": "command", "nthChild": {"position": 1, "reverse": True}}
PIPELINE_LAST = {"kind": "pipeline", "has": {"capture": LAST_STAGE, "name": "LAST", "field": NAME}}


def capture(sub: dict[str, Any], name: str = "X", **extra: Any) -> dict[str, Any]:
    return {"capture": sub, "name": name, **extra}


def binding(name: str) -> dict[str, Any]:
    return {"pattern": f"${name}"}


def field_binding(name: str, field: str = NAME) -> dict[str, Any]:
    child = {"field": field, "pattern": f"${name}"}
    return {"any": [{"has": child}, {"kind": redirects.WRAPPER, "has": {"field": "body", "has": child}}]}


def raw_rule(match: dict[str, Any], message: str = "m", **extra: Any) -> dict[str, Any]:
    return {"match": match, "message": message, **extra}


class Engine(AstIsolated):
    def says(self, match: dict[str, Any], message: str, command: str, **extra: Any) -> str:
        rule = policy.Rule.from_json(raw_rule(match, message, **extra))
        evaluation = matching.evaluate(command, {"r": rule})
        self.assertIsNotNone(evaluation.kinds["r"], command)
        return engine.texts_of(rule, evaluation.details.get("r")).full


class Expansion(Engine):
    def test_a_capture_is_its_sub_rule_and_a_bare_metavariable_on_the_same_node(self) -> None:
        self.assertEqual(rulebuilder.expanded(capture({"kind": "word"}, "LAST")),
                         {"all": [{"kind": "word"}, binding("LAST")]})

    def test_a_field_capture_binds_the_child_or_the_body_of_a_redirect_wrapper(self) -> None:
        self.assertEqual(rulebuilder.expanded(capture({"kind": "command"}, "N", field=NAME)),
                         {"all": [{"kind": "command"}, field_binding("N")]})

    def test_captures_nest_and_sit_in_any_position(self) -> None:
        nested = rulebuilder.expanded(capture({"has": capture({"kind": "word"}, "B")}, "A"))
        self.assertEqual(nested, {"all": [{"has": {"all": [{"kind": "word"}, binding("B")]}}, binding("A")]})
        for key in ("has", "inside", "follows", "precedes", "not"):
            with self.subTest(key=key):
                got = rulebuilder.expanded({"kind": "word", key: capture({"kind": "word"}, "B")})
                self.assertEqual(got[key], {"all": [{"kind": "word"}, binding("B")]})
        for key in ("any", "all"):
            with self.subTest(key=key):
                got = rulebuilder.expanded({key: [capture({"kind": "word"}, "B"), {"kind": "command"}]})
                self.assertEqual(got[key][0], {"all": [{"kind": "word"}, binding("B")]})
        got = rulebuilder.expanded({"statement": capture({"kind": "word"}, "S")})
        self.assertEqual(got, {"all": [redirects.statement({"all": [{"kind": "word"}, binding("S")]})]})

    def test_a_capture_around_a_command_with_siblings_still_sees_through_a_redirect(self) -> None:
        plain_form = rulebuilder.expanded({"command": "pgrep", "precedes": {"kind": "word"}})
        around = rulebuilder.expanded(capture({"command": "pgrep", "precedes": {"kind": "word"}}, "P"))
        self.assertEqual(around, {"all": [plain_form, binding("P")]})
        self.assertIn(redirects.WRAPPER, str(around))
        under_has = rulebuilder.expanded({"has": capture({"command": "pgrep", "precedes": {"kind": "word"}}, "P")})
        self.assertIn(redirects.WRAPPER, str(under_has))

    def test_positions_inside_a_capture_next_to_a_command_count_as_positions(self) -> None:
        beside = {"command": "x", "precedes": {"kind": "word"}}
        placed = rulebuilder.expanded({**beside, "all": [capture({"inside": {"kind": "list"}})]})
        self.assertIn(redirects.WRAPPER, str(placed))
        with self.assertRaises(policy.Invalid):
            rulebuilder.expanded({**beside, "all": [capture({"inside": {"kind": "list"}, "regex": "a"})]})

    def test_every_expanded_shape_binds_what_it_says_when_run(self) -> None:
        flag, path = {"kind": "word", "regex": "^-"}, {"kind": "word", "regex": "^/"}
        for match, message, expected in (
            (capture({"command": "du"}, "A"), "{A}", "du -sh /x"),
            (capture({"command": "du"}, "A", field=NAME), "{A}", "du"),
            (capture({"command": "du", "has": capture(flag, "B")}, "A"), "{A}|{B}", "du -sh /x|-sh"),
            ({"command": "du", "has": capture(path, "B")}, "{B}", "/x"),
            ({**path, "inside": capture({"command": "du"}, "B")}, "{B}", "du -sh /x"),
            ({**path, "follows": capture(flag, "B")}, "{B}", "-sh"),
            ({**flag, "precedes": capture(path, "B")}, "{B}", "/x"),
            ({"any": [capture({"command": "du"}, "B"), {"command": "df"}]}, "{B}", "du -sh /x"),
            ({"all": [capture({"command": "du"}, "B"), {"has": flag}]}, "{B}", "du -sh /x"),
            ({"statement": capture({"command": "du"}, "S")}, "{S}", "du -sh /x"),
        ):
            with self.subTest(match=match):
                self.assertEqual(self.says(match, message, "du -sh /x"), expected)

    def test_a_capture_around_a_command_with_siblings_binds_it_when_run_with_and_without_a_redirect(self) -> None:
        match = {"kind": "program", "has": capture({"command": "make", "precedes": {"command": "tail"}}, "P")}
        self.assertEqual(self.says(match, "{P}", "make\ntail"), "make")
        self.assertEqual(self.says(match, "{P}", "make >o\ntail"), "make >o")
        around = capture({"command": "make", "precedes": {"command": "tail"}}, "P", field=NAME)
        self.assertEqual(self.says(around, "{P}", "make 2>&1 >o\ntail"), "make")


class Binding(Engine):
    def bound(self, match: dict[str, Any]) -> set[str]:
        return set(rulebuilder.bound_names(match) or ())

    def test_the_names_a_match_binds_are_its_captures_and_its_metavariables(self) -> None:
        match = {"all": [capture({"kind": "word"}, "A"), {"pattern": "git push $REMOTE $$$REST"},
                         {"pattern": {"context": "echo $CTX", "selector": "command"}}]}
        self.assertEqual(self.bound(match), {"A", "REMOTE", "REST", "CTX"})

    def test_only_what_ast_grep_binds_counts(self) -> None:
        for pattern, names in (("echo '$X'", set()), ('echo "$X"', {"X"}), ("echo ${Y}", set()), ("echo $_N", set()),
                               ("git push $$$REST", {"REST"}), ("cat <<EOF\n$X\nEOF", set()), ("echo $$A", set()),
                               ("echo a $A $B", {"A", "B"}), ("echo $lower", set()), ("echo $(ls $Z)", {"Z"})):
            with self.subTest(pattern=pattern):
                self.assertEqual(self.bound({"pattern": pattern}), names)

    def test_the_engine_binds_what_the_parsed_pattern_names(self) -> None:
        self.assertEqual(self.says({"pattern": 'echo "$X"'}, "[{X}]", 'echo "hi there"'), "[hi there]")
        self.assertEqual(self.says({"pattern": "git push $$$REST"}, "[{REST}]", "git push -f origin"), "[-f origin]")

    def test_a_pattern_under_not_binds_nothing(self) -> None:
        self.assertEqual(self.bound({"command": "ls", "not": {"pattern": "ls $F"}}), set())
        self.assertEqual(self.bound({"command": "ls", "not": {"any": [{"pattern": "ls $F"}]}, "pattern": "ls $G"}),
                         {"G"})

    def test_a_placeholder_that_only_a_literal_or_a_negated_pattern_names_is_refused(self) -> None:
        for match, name in (({"pattern": "echo '$X'"}, "X"), ({"command": "ls", "not": {"pattern": "ls $F"}}, "F"),
                            ({"pattern": "cat <<EOF\n$X\nEOF"}, "X"), ({"pattern": "echo ${Y}"}, "Y")):
            with self.subTest(match=match), self.assertRaises(policy.Invalid) as raised:
                policy.Rule.from_json(raw_rule(match, f"{{{name}}}"))
            self.assertIn(f"placeholder {{{name}}} is never bound", str(raised.exception))
        policy.Rule.from_json(raw_rule({"pattern": 'echo "$X"'}, "{X}"))


class Validation(Isolated):
    def invalid(self, node: Any) -> str:
        with self.assertRaises(policy.Invalid) as raised:
            rulebuilder.checked(node)
        return str(raised.exception)

    def test_the_name_is_required_and_an_upper_case_identifier(self) -> None:
        for bad in ({"capture": {"kind": "word"}}, capture({"kind": "word"}, "last"), capture({"kind": "word"}, "1A"),
                    capture({"kind": "word"}, "A-B"), capture({"kind": "word"}, "_A"), capture({"kind": "word"}, ""),
                    {"capture": {"kind": "word"}, "name": 3}):
            with self.subTest(bad=bad):
                self.assertIn("match.name", self.invalid(bad))

    def test_found_is_reserved(self) -> None:
        self.assertIn("reserved", self.invalid(capture({"kind": "word"}, "found")))

    def test_only_capture_name_and_field_are_allowed(self) -> None:
        self.assertIn("match.regex", self.invalid({**capture({"kind": "word"}), "regex": "x"}))
        self.assertIn("match.all[1].stopBy", self.invalid({"all": [{"kind": "word"}, {**capture({"kind": "word"}),
                                                                                       "stopBy": "end"}]}))

    def test_the_field_is_a_lower_case_identifier(self) -> None:
        for bad in ("", "Name", "a-b", 3, None):
            with self.subTest(field=bad):
                self.assertIn("match.field", self.invalid(capture({"kind": "word"}, field=bad)))

    def test_the_sub_rule_is_a_non_empty_object(self) -> None:
        for bad in ({}, "x", [], None):
            with self.subTest(sub=bad):
                self.assertIn("match.capture", self.invalid({"capture": bad, "name": "X"}))

    def test_an_error_inside_the_sub_rule_names_its_path(self) -> None:
        self.assertIn("match.has.capture.bogus", self.invalid({"has": capture({"bogus": 1})}))

    def test_a_capture_under_not_is_an_error_at_any_depth(self) -> None:
        for bad in ({"not": capture({"kind": "word"})}, {"not": {"has": capture({"kind": "word"})}},
                    {"not": {"any": [{"kind": "x"}, capture({"kind": "word"})]}},
                    {"has": {"not": {"all": [capture({"kind": "word"})]}}}):
            with self.subTest(bad=bad):
                self.assertIn("under 'not'", self.invalid(bad))
        self.assertIn("match.not", self.invalid({"not": capture({"kind": "word"})}))

    def test_a_name_twice_on_one_path_is_an_error(self) -> None:
        for bad in ({"all": [capture({"kind": "word"}), capture({"kind": "word"})]},
                    capture({"has": capture({"kind": "word"})}),
                    {"has": capture({"kind": "word"}), "inside": capture({"kind": "word"})},
                    {"all": [capture({"kind": "word"}), {"any": [capture({"kind": "word"}), {"kind": "command"}]}]}):
            with self.subTest(bad=bad):
                self.assertIn("bound twice", self.invalid(bad))

    def test_the_same_name_in_different_any_branches_is_allowed(self) -> None:
        rulebuilder.checked({"any": [capture({"command": "git"}, field=NAME), capture({"command": "svn"}, field=NAME)]})
        rulebuilder.checked({"all": [{"any": [capture({"kind": "a"}), capture({"kind": "b"})]},
                                     {"any": [capture({"kind": "c"}, "Y"), capture({"kind": "d"}, "Y")]}]})

    def test_a_capture_in_a_message_case_matches_is_refused(self) -> None:
        raw = raw_rule({"command": "x"}, messages=[{"when": {"matches": capture({"kind": "word"})}, "text": "t"}])
        with self.assertRaises(policy.Invalid) as raised:
            policy.Rule.from_json(raw)
        self.assertIn("binds nothing", str(raised.exception))

    def test_every_placeholder_must_be_bound_by_the_match(self) -> None:
        match = capture({"command": "x"}, "LAST", field=NAME)
        policy.Rule.from_json(raw_rule(match, "{LAST} {found}", when={"bin": "x"}))
        policy.Rule.from_json(raw_rule({"pattern": "rm $DIR"}, "{DIR}"))
        policy.Rule.from_json(raw_rule({"pattern": "rm $$$ARGS"}, "{ARGS}"))
        for fields in ({"message": "{OTHER}"}, {"message": "m", "messageShort": "{OTHER}"},
                       {"message": "m", "messages": [{"when": {"wrapped": True}, "text": "{OTHER}"}]},
                       {"message": "m", "messages": [{"when": {"wrapped": True}, "text": "t", "messageShort": "{OTHER}"}]}):
            with self.subTest(fields=fields), self.assertRaises(policy.Invalid) as raised:
                policy.Rule.from_json({"match": match, **fields})
            self.assertIn("placeholder {OTHER} is never bound by the match", str(raised.exception))

    def test_a_placeholder_bound_in_only_some_any_branches_is_accepted(self) -> None:
        policy.Rule.from_json(raw_rule({"any": [capture({"command": "du"}, "P"), {"command": "df"}]}, "{P}"))

    def test_an_override_text_with_an_unbound_placeholder_leaves_the_base_rule_unchanged(self) -> None:
        base = {"rules": {"r": raw_rule({"command": "x"}, "m")}}
        self.assertEqual(policy.effective_rules({}, base, {"rules": {"r": {"message": "{NOPE}"}}})["r"].message, "m")


class Rendering(Engine):
    def test_the_last_stage_of_a_pipeline_of_two_three_and_four(self) -> None:
        for command, last in (("a | b", "b"), ("a | b | c", "c"), ("a | b | c | d", "d"), ("a | b | c > o 2>&1", "c"),
                              ("a | b -x >log", "b"), ("(a | b | c)", "c"), ("x && a | b | c", "c")):
            with self.subTest(command=command):
                self.assertEqual(self.says(PIPELINE_LAST, "[{LAST}]", command), f"[{last}]")

    def test_a_capture_without_a_field_binds_the_matched_node(self) -> None:
        match = capture({"command": "du"}, "WHOLE")
        self.assertEqual(self.says(match, "{WHOLE}", "du -sh /tmp"), "du -sh /tmp")
        self.assertEqual(self.says(match, "{WHOLE}", "du -sh /tmp > out"), "du -sh /tmp")

    def test_a_capture_under_has_and_in_a_nested_any(self) -> None:
        under_has = {"command": "du", "has": capture({"kind": "word", "regex": "^[^-]"}, "PATH")}
        self.assertEqual(self.says(under_has, "{PATH}", "du -sh /tmp/x"), "/tmp/x")
        nested = {"command": ["git", "svn"], "has": {"any": [{"all": [{"any": [capture({"regex": "^push$"}, "VERB"),
                                                                              capture({"regex": "^commit$"}, "VERB")]}]},
                                                             {"regex": "^never-seen$"}]}}
        self.assertEqual(self.says(nested, "{VERB}", "git commit -m x"), "commit")
        self.assertEqual(self.says(nested, "{VERB}", "svn push"), "push")

    def test_the_same_name_in_two_any_branches_binds_the_branch_that_matched(self) -> None:
        match = {"any": [capture({"command": "git"}, field=NAME), capture({"command": "svn"}, field=NAME)]}
        self.assertEqual(self.says(match, "{X}", "svn up"), "svn")
        self.assertEqual(self.says(match, "{X}", "git pull"), "git")

    def test_a_name_bound_in_another_branch_renders_empty(self) -> None:
        match = {"any": [{"command": "du", "has": capture({"kind": "word", "regex": "^[^-]"}, "PATH")}, {"command": "df"}]}
        self.assertEqual(self.says(match, "[{PATH}]", "df -h"), "[]")
        self.assertEqual(self.says(match, "[{PATH}]", "du /srv"), "[/srv]")

    def test_a_capture_on_a_command_that_is_also_matched_as_a_redirect_wrapper(self) -> None:
        match = {"kind": "program", "has": capture({"command": "make", "precedes": {"command": "tail"}}, "STEP")}
        self.assertEqual(self.says(match, "{STEP}", "make\ntail"), "make")
        self.assertEqual(self.says(match, "{STEP}", "make >o\ntail"), "make >o")
        field = {"command": "tail", "follows": capture({"command": "make"}, "STEP", field=NAME)}
        self.assertEqual(self.says(field, "{STEP}", "make 2>&1 >o\ntail"), "make")
        self.assertEqual(self.says(capture({"command": "make", "precedes": {"command": "tail"}}, "STEP"), "{STEP}",
                                   "make >o\ntail"), "make")

    def test_a_capture_inside_a_statement(self) -> None:
        match = {"statement": capture({"command": "tail"}, "T", field=NAME)}
        self.assertEqual(self.says(match, "{T}", "make | tail > o"), "tail")
        self.assertEqual(self.says(match, "{T}", "tail"), "tail")

    def test_a_long_capture_is_capped_and_a_multi_line_one_is_flattened(self) -> None:
        match = capture({"kind": "program"}, "ALL")
        long_text = self.says(match, "{ALL}", "echo " + "x" * 300)
        self.assertEqual(long_text, "echo " + "x" * (messages.MAX_CAPTURE_CHARS - 5) + "…")
        self.assertEqual(self.says(match, "[{ALL}]", "make \\\n  --jobs 4\n   tail  \n"), "[make \\ --jobs 4 tail]")
        self.assertNotIn("\n", self.says(match, "{ALL}", "a\n\n\nb"))

    def test_a_message_case_can_pick_the_text_that_uses_the_capture(self) -> None:
        match = {"any": [{"command": "du", "has": capture({"kind": "word", "regex": "^[^-]"}, "PATH")}, {"command": "du"}]}
        cases = [{"when": {"matches": {"has": {"kind": "word", "regex": "^[^-]"}}}, "text": "dust {PATH}"}]
        self.assertEqual(self.says(match, "dust", "du -sh /tmp", messages=cases), "dust /tmp")
        self.assertEqual(self.says(match, "dust", "du -sh", messages=cases), "dust")


class Hook(AstIsolated):
    def reason(self, command: str, session: str = "s") -> str:
        out = self.hook(command, session=session)
        assert out is not None, command
        return plain(out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_captures_cross_the_fork_boundary_for_has_and_nested_any(self) -> None:
        self.put(self.gpath, {"rules": {"last": raw_rule(PIPELINE_LAST, "last is {LAST}.")}})
        for i, (command, last) in enumerate((("a | b", "b"), ("a | b | c", "c"), ("a | b | c | d", "d"))):
            with self.subTest(command=command):
                self.assertIn(f"last is {last}.", self.reason(command, f"s{i}"))
        nested = {"any": [{"command": "du", "has": {"any": [capture({"kind": "word", "regex": "^[^-]"}, "PATH"),
                                                              {"regex": "^never-seen$"}]}}, {"command": "df"}]}
        self.put(self.gpath, {"rules": {"disk": raw_rule(nested, "path=[{PATH}]")}})
        self.assertIn("path=[/srv]", self.reason("du -sh /srv", "t1"))
        self.assertIn("path=[]", self.reason("df -h", "t2"))

    def test_rule_test_shows_the_text_with_the_capture_filled_in(self) -> None:
        code, out, err = self.cli("rule", "test", "--json", json.dumps(raw_rule(PIPELINE_LAST, "last is {LAST}.")),
                                  "make | tail -3")
        self.assertEqual(code, 0, err)
        self.assertIn("make | tail -3", out)
