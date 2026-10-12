from __future__ import annotations  # noqa: I001

import json
import time
from contextlib import AbstractContextManager
from typing import Any
from unittest import mock

from helpers import AstIsolated

import cli
import matching
import policy
import rulebuilder
import store

MATCHERS: dict[str, Any] = {
    "git": {"command": "git"},
    "pushing": {"use": "@git", "has": {"regex": "^push$"}},
    "after-cd": {"follows": {"command": "cd", "stopBy": "end"}},
}


def rule(match: Any, **fields: Any) -> dict[str, Any]:
    return {"message": "m", "match": match, **fields}


def shipped(**fragments: Any) -> AbstractContextManager[Any]:
    return mock.patch.object(rulebuilder, "builtin_matchers",
                             return_value={name: rulebuilder.Builtin("test", body) for name, body in fragments.items()})


class Expansion(AstIsolated):
    def kinds(self, spec: dict[str, Any], commands: dict[str, bool], matchers: Any = MATCHERS) -> None:
        parsed = policy.Rule.from_json(spec, matchers)
        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": parsed}).kinds["r"] is not None, expected)

    def test_a_reference_is_an_atom_that_composes_under_the_combinators_and_relations(self) -> None:
        self.kinds(rule({"all": [{"use": "@git"}, {"not": {"has": {"regex": "^status$"}}}]}),
                   {"git push": True, "git status": False, "ls": False})
        self.kinds(rule({"any": [{"use": "@pushing"}, {"command": "ls"}]}),
                   {"git push": True, "ls": True, "git log": False})
        self.kinds(rule({"command": "make", "inside": {"use": "@pushing"}}), {"git push $(make)": False})
        self.kinds(rule({"command": "ls", "follows": {"use": "@pushing", "stopBy": "end"}}),
                   {"git push && ls": True, "ls && git push": False})

    def test_keys_next_to_a_reference_are_anded_and_a_reference_keeps_redirect_transparency(self) -> None:
        spec = rule({"use": "@git", "has": {"regex": "^push$"}})
        self.assertEqual(policy.Rule.from_json(spec, MATCHERS).match, policy.Rule.from_json(rule(MATCHERS["pushing"]),
                                                                                           MATCHERS).match)
        self.kinds(rule({"use": "@after-cd", "command": "git"}), {"cd x && git log": True, "cd x && ls": False})
        self.kinds(rule({"use": "@after-cd", "command": "git"}), {"cd x && git log >f": True, "git log >f": False})

    def test_message_cases_expand_references_in_matches(self) -> None:
        cases = [{"when": {"matches": {"use": "@pushing"}}, "text": "pushed"}]
        parsed = policy.Rule.from_json(rule({"use": "@git"}, messages=cases), MATCHERS)
        self.assertEqual(parsed.messages[0].when, {"matches": {"command": "git", "has": {"regex": "^push$"}}})
        self.assertEqual(matching.evaluate("git push", {"r": parsed}).details["r"].case, 0)
        self.assertIsNone(matching.evaluate("git log", {"r": parsed}).details["r"].case)

    def test_the_rule_names_the_references_it_used_and_the_ones_it_writes(self) -> None:
        parsed = policy.Rule.from_json(rule({"use": "@pushing"}), MATCHERS)
        self.assertEqual((parsed.matchers, parsed.written), (("@git", "@pushing"), ("@pushing",)))
        self.assertEqual(policy.Rule.from_json(rule({"command": "ls"}), MATCHERS).matchers, ())

    def test_a_changed_matcher_changes_the_hash_of_the_rules_that_use_it(self) -> None:
        spec = rule({"use": "@git"})
        before = policy.rule_hash(policy.Rule.from_json(spec, MATCHERS))
        after = policy.rule_hash(policy.Rule.from_json(spec, {**MATCHERS, "git": {"command": "hg"}}))
        self.assertNotEqual(before, after)
        self.assertEqual(before, policy.rule_hash(policy.Rule.from_json(rule({"command": "git"}))))

    def test_a_changed_built_in_matcher_changes_the_hash_of_the_rules_that_use_it(self) -> None:
        spec = rule({"use": "@preset/vcs"})
        with shipped(vcs={"command": "git"}):
            before = policy.rule_hash(policy.Rule.from_json(spec))
            same = policy.rule_hash(policy.Rule.from_json(rule({"command": "git"})))
        with shipped(vcs={"command": "hg"}):
            after = policy.rule_hash(policy.Rule.from_json(spec))
        self.assertEqual(before, same)
        self.assertNotEqual(before, after)

    def test_missing_names_cycles_and_malformed_fragments_are_refused_with_a_message(self) -> None:
        refused = {"missing": (rule({"use": "@nope"}), MATCHERS, "refers to no matcher '@nope' (this config defines: "),
                   "cycle": (rule({"use": "@a"}), {"a": {"use": "@b"}, "b": {"has": {"use": "@a"}}},
                             "matcher cycle: @a -> @b -> @a"),
                   "self": (rule({"use": "@a"}), {"a": {"not": {"use": "@a"}}}, "matcher cycle: @a -> @a"),
                   "not an object": (rule({"use": "@a"}), {"a": "git"}, "matcher @a must be a non-empty rule object"),
                   "bad atom": (rule({"use": "@a"}), {"a": {"command": "a b"}}, "@a.command"),
                   "in a case": (rule({"command": "x"}, messages=[{"when": {"matches": {"use": "@zz"}}, "text": "t"}]),
                                 MATCHERS, "refers to no matcher '@zz'"),
                   "no table": (rule({"use": "@git"}), None, "refers to no matcher '@git' (this config defines: none)"),
                   "no @": (rule({"use": "git"}), MATCHERS, "references start with @"),
                   "not a string": (rule({"use": ["@git"]}), MATCHERS, "must be a reference"),
                   "both keys": (rule({"use": "@git", "matcher": "git"}), MATCHERS, "has both 'use' and 'matcher'"),
                   "unknown built-in": (rule({"use": "@preset/nope"}), MATCHERS,
                                        "this guardrails version does not ship @preset/nope; update the plugin"),
                   "layer name under the built-in scope": (rule({"use": "@preset/git"}), MATCHERS,
                                                           "does not ship @preset/git"),
                   "alias with @": (rule({"matcher": "@preset/vcs"}), MATCHERS, "takes a bare name"),
                   "old grammar": (rule({"use": "@Old_Name"}), {"Old_Name": {"command": "x"}, "ok": {"command": "y"}},
                                   "matcher @Old_Name is ignored because its name does not match")}
        for name, (spec, matchers, text) in refused.items():
            with self.subTest(name), self.assertRaises(policy.Invalid) as caught:
                policy.Rule.from_json(spec, matchers)
            self.assertIn(text, str(caught.exception))

    def test_errors_in_a_matcher_body_read_cleanly(self) -> None:
        for body, text in (({"use": "bad2"}, "'@m.use': references start with @, write \"@bad2\""),
                           ({"has": {"use": "bad2"}}, "'@m.has.use': references start with @"),
                           ({"has": {"use": "@nope"}}, "@m.has refers to no matcher '@nope' (this config defines: none)")):
            with self.subTest(body), self.assertRaises(policy.Invalid) as caught:
                policy.Rule.from_json(rule({"use": "@m"}), {"m": body})
            self.assertIn(text, str(caught.exception))

    def test_a_built_in_resolves_from_any_layer_and_a_user_matcher_may_wrap_it(self) -> None:
        with shipped(vcs={"command": ["git", "hg"]}):
            wrapped = {"vcs-push": {"use": "@preset/vcs", "has": {"regex": "^push$"}}}
            self.kinds(rule({"use": "@preset/vcs"}), {"git x": True, "hg x": True, "ls": False}, None)
            self.kinds(rule({"use": "@vcs-push"}), {"git push": True, "git log": False}, wrapped)
            found = policy.effective({"rules": {"m": rule({"use": "@preset/vcs"})}},
                                     {"rules": {"g": rule({"use": "@preset/vcs"})}},
                                     {"rules": {"p": rule({"use": "@preset/vcs"})}})
        self.assertEqual((sorted(found.rules), found.problems), (["g", "m", "p"], {}))
        self.assertEqual(found.rules["p"].matchers, ("@preset/vcs",))

    def test_a_built_in_refers_only_to_built_ins(self) -> None:
        with shipped(a={"use": "@preset/b"}, b={"command": "b"}, c={"use": "@local"}, d={"use": "@preset/d"}):
            self.assertEqual(policy.Rule.from_json(rule({"use": "@preset/a"})).match, {"command": "b"})
            for name, text in (("c", "a built-in matcher can only refer to built-in ones"), ("d", "matcher cycle")):
                with self.subTest(name), self.assertRaises(policy.Invalid) as caught:
                    policy.Rule.from_json(rule({"use": f"@preset/{name}"}), {"local": {"command": "x"}, "d": {"command": "y"}})
                self.assertIn(text, str(caught.exception))

    def test_the_deprecated_matcher_key_is_an_alias_that_the_rule_reports(self) -> None:
        with shipped(vcs={"command": "git"}):
            old = policy.Rule.from_json(rule({"all": [{"matcher": "preset.vcs"}, {"matcher": "local"}]}),
                                        {"local": {"has": {"regex": "^x$"}}})
            new = policy.Rule.from_json(rule({"all": [{"use": "@preset/vcs"}, {"use": "@local"}]}),
                                        {"local": {"has": {"regex": "^x$"}}})
        self.assertEqual((old.matchers, old.deprecated_use), (("@local", "@preset/vcs"), True))
        self.assertEqual((old.match, policy.rule_hash(old)), (new.match, policy.rule_hash(new)))
        self.assertFalse(new.deprecated_use)

    def test_the_budget_refuses_a_chain_that_doubles_at_each_level_quickly(self) -> None:
        chain: dict[str, Any] = {"m0": {"command": "x"}}
        for i in range(1, 23):
            chain[f"m{i}"] = {"all": [{"use": f"@m{i - 1}"}, {"use": f"@m{i - 1}"}]}
        started = time.monotonic()
        with self.assertRaises(policy.Invalid) as caught:
            policy.Rule.from_json(rule({"use": "@m22"}), chain)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("more than 2000 nodes", str(caught.exception))
        self.assertEqual(rulebuilder.matcher_problems(chain), {})


class Layers(AstIsolated):
    def test_a_layer_resolves_references_from_its_own_matchers_only(self) -> None:
        shared = {"git": {"command": "git"}}
        uses = rule({"use": "@git"})
        found = policy.effective({"rules": {"m": uses}, "matchers": shared}, {"rules": {"g": uses}},
                                 {"rules": {"p": uses}, "matchers": {"git": {"command": "hg"}}})
        self.assertEqual(sorted(found.rules), ["m", "p"])
        self.assertIn("refers to no matcher '@git'", found.problems["g"])
        self.assertEqual(found.rules["m"].match, {"command": "git"})
        self.assertEqual(found.rules["p"].match, {"command": "hg"})

    def test_a_layer_cannot_define_a_name_that_claims_a_built_in(self) -> None:
        mine = {"preset.vcs": {"command": "x"}, "preset/vcs": {"command": "x"}, "@vcs": {"command": "x"},
                "Upper": {"command": "x"}, "snake_case": {"command": "x"}, "fine-1": {"command": "x"}}
        inliner = rulebuilder.Inliner(mine)
        self.assertEqual(sorted(inliner.matchers), ["fine-1"])
        for name in mine:
            self.assertEqual(bool(policy.MATCHER_NAME.fullmatch(name)), name == "fine-1")
        with self.assertRaises(policy.Invalid) as caught, shipped(vcs={"command": "git"}):
            policy.Rule.from_json(rule({"use": "@preset.vcs"}), mine)
        self.assertIn("matcher @preset.vcs is ignored because its name does not match [a-z0-9][a-z0-9-]*",
                      str(caught.exception))
        with shipped(vcs={"command": "git"}):
            self.assertEqual(policy.Rule.from_json(rule({"use": "@preset/vcs"}), mine).match, {"command": "git"})

    def test_a_managed_layer_keeps_its_matchers_and_reports_a_broken_one(self) -> None:
        self.put(self.mpath, {"rules": {"m": rule({"use": "@git"})}, "matchers": {"git": {"command": "git"},
                                                                                  "bad": {"use": "@bad"},
                                                                                  "Bad_Name": {"command": "x"}}})
        self.assertEqual(self.cli("status")[0], 0)
        layer, problems = store.load_managed()
        self.assertEqual(sorted(layer["matchers"]), ["Bad_Name", "bad", "git"])
        self.assertEqual(sorted(layer["rules"]), ["m"])
        self.assertTrue(any("managed matcher @bad is invalid: matcher cycle: @bad -> @bad" in p for p in problems))
        self.assertTrue(any("managed matcher @Bad_Name is ignored" in p for p in problems))
        self.assertEqual(matching.evaluate("git x", {"r": policy.effective(layer, {}, {}).rules["m"]}).kinds["r"], "direct")

    def test_a_built_in_is_usable_from_the_project_and_the_managed_layer(self) -> None:
        use = rule({"use": "@preset/operand"}, action="warn")
        self.put(self.mpath, {"rules": {"m": use}})
        self.put(self.ppath, {"rules": {"p": use}})
        self.assertNotIn("matcher", self.cli("status", "--problems")[1])
        status = self.cli("status")[1]
        self.assertRegex(status, r"`@preset/operand\s*` built-in · used by 2 rules: `m`, `p`")


class Cli(AstIsolated):
    def add_matcher(self, name: str, fragment: Any, *extra: str) -> tuple[int, str, str]:
        return self.cli("matcher", "add", name, "--json", json.dumps(fragment), *extra)

    def test_matcher_add_rule_add_status_test_and_rm_work_together(self) -> None:
        self.assertEqual(self.add_matcher("git", {"command": "git"})[0], 0)
        code, _, err = self.cli("rule", "add", "no-push", "--json", json.dumps(rule({"use": "@pushing"})))
        self.assertEqual((code, "refers to no matcher '@pushing'" in err), (2, True))
        self.assertEqual(self.add_matcher("pushing", {"use": "@git", "has": {"regex": "^push$"}})[0], 0)
        self.assertEqual(self.cli("rule", "add", "no-push", "--json", json.dumps(rule({"use": "@pushing"})))[0], 0)
        self.assertEqual(sorted(self.get(self.gpath)["matchers"]), ["git", "pushing"])
        self.assertEqual(self.get(self.gpath)["rules"]["no-push"]["match"], {"use": "@pushing"})

        status = self.cli("status")[1]
        self.assertIn("**Matchers**", status)
        self.assertRegex(status, r"`@git\s*` global · used by 1 rule: `no-push`")
        self.assertIn("uses `@pushing`", status)
        self.assertIn("uses `@pushing`", self.cli("status", "--rule", "no-push")[1])

        card = self.cli("rule", "test", "--id", "no-push", "git push x", "git log")[1]
        self.assertIn('{"command":"git","has":{"regex":"^push$"}}', card)
        self.assertEqual(self.cli("rule", "test", "--json", json.dumps(rule({"use": "@pushing"})), "git push")[0], 0)

        code, _, err = self.cli("matcher", "rm", "git")
        self.assertEqual((code, "rule no-push would stop loading" in err), (2, True))
        self.assertEqual(self.cli("matcher", "rm", "nope")[0], 2)
        self.assertEqual(self.cli("rule", "rm", "no-push")[0], 0)
        self.assertEqual(self.cli("matcher", "rm", "pushing")[0], 0)
        self.assertEqual(self.cli("matcher", "rm", "git")[0], 0)
        self.assertEqual(self.get(self.gpath)["matchers"], {})

    def test_matcher_add_refuses_a_cycle_and_a_replacement_that_breaks_a_rule(self) -> None:
        self.add_matcher("a", {"command": "a"})
        code, _, err = self.add_matcher("b", {"has": {"use": "@b"}})
        self.assertEqual((code, "matcher cycle: @b -> @b" in err), (2, True))
        self.assertNotIn("b", self.get(self.gpath)["matchers"])
        self.cli("rule", "add", "r", "--json", json.dumps(rule({"use": "@a"})))
        self.assertEqual(self.add_matcher("a", {"command": "a b"})[0], 2)
        self.assertEqual(self.get(self.gpath)["matchers"]["a"], {"command": "a"})
        self.assertIn("replaced matcher a", self.add_matcher("a", {"command": "c"})[1])

    def test_matcher_add_refuses_names_outside_the_grammar(self) -> None:
        for name in ("preset.mine", "preset/mine", "@mine", "Mine", "my_matcher", "a b"):
            with self.subTest(name):
                code, _, err = self.add_matcher(name, {"command": "x"})
                self.assertEqual((code, "must match [a-z0-9][a-z0-9-]*" in err), (2, True))
        self.assertEqual(self.add_matcher("m1-x", {"command": "x"})[0], 0)

    def test_rule_set_match_resolves_against_the_config_it_changes(self) -> None:
        self.add_matcher("git", {"command": "git"})
        self.cli("rule", "add", "r", "--json", json.dumps(rule({"command": "x"})))
        self.assertEqual(self.set_rule("r", {"match": {"use": "@git"}})[0], 0)
        self.assertEqual(self.set_rule("r", {"match": {"use": "@nope"}})[0], 2)
        self.assertIsNotNone(self.hook("git log"))

    def test_the_hook_denies_through_a_matcher_and_a_matcher_edit_changes_the_label(self) -> None:
        self.put(self.gpath, {"rules": {"r": rule({"use": "@git"})}, "matchers": {"git": {"command": "git"}}})
        first = self.hook("git log", session="a")
        assert first is not None
        label = first["hookSpecificOutput"]["permissionDecisionReason"].split("]")[0]
        self.put(self.gpath, {"rules": {"r": rule({"use": "@git"})}, "matchers": {"git": {"command": ["git", "hg"]}}})
        second = self.hook("git log", session="b")
        assert second is not None
        self.assertNotEqual(label, second["hookSpecificOutput"]["permissionDecisionReason"].split("]")[0])

    def test_preset_install_copies_rules_that_use_built_ins_and_no_matchers(self) -> None:
        code, out, _ = self.cli("preset", "install", "shell-hygiene", "--only", "http-wait-exact-status")
        self.assertEqual(code, 0)
        self.assertNotIn("matcher", out)
        state = self.get(self.gpath)
        self.assertNotIn("matchers", state)
        self.assertIn("@preset/", json.dumps(state["rules"]))
        self.assertEqual(self.cli("preset", "install", "modern-cli")[0], 0)
        self.assertNotIn("matchers", self.get(self.gpath))
        status = self.cli("status")[1]
        self.assertIn("uses `@preset/compares-2xx`", status)
        self.assertRegex(status, r"`@preset/operand\s*` built-in · used by 2 rules: `cargo-nextest`, `du-dust`")
        self.assertEqual(self.cli("status", "--problems")[1].strip(), "No problems.")

    def test_legacy_preset_copies_are_ignored_reported_and_removable(self) -> None:
        self.put(self.gpath, {"rules": {"old": rule({"command": "du", "has": {"matcher": "preset.operand"}}),
                                        "mine": rule({"use": "@keep"})},
                              "matchers": {"preset.operand": {"command": "never-used"}, "keep": {"command": "git"}}})
        self.assertEqual(self.cli("rule", "test", "--id", "old", "du /x", "git")[0], 0)
        self.assertIsNotNone(self.hook("du -sh /x"))
        self.assertIsNotNone(self.hook("git log"))
        problems = self.cli("status", "--problems")[1]
        self.assertIn("matchers @preset.operand are legacy copies of built-in ones and are ignored", problems)
        self.assertIn("rules old write {\"matcher\": ...}", problems)
        self.assertNotIn("mine", problems)
        status = self.cli("status")[1]
        self.assertNotIn("preset.operand", status.split("**Matchers**")[1].split("**Problems**")[0])
        code, out, _ = self.cli("matcher", "rm", "preset.operand")
        self.assertEqual((code, "removed matcher preset.operand" in out), (0, True))
        self.assertEqual(sorted(self.get(self.gpath)["matchers"]), ["keep"])

    def test_preset_install_removes_legacy_copies_and_rewrites_the_rules(self) -> None:
        self.put(self.gpath, {"rules": {"du-dust": rule({"matcher": "preset.operand"})},
                              "matchers": {"preset.operand": {"command": "x"}, "preset.gone": {"command": "y"},
                                           "keep": {"command": "git"}}})
        code, out, _ = self.cli("preset", "install", "modern-cli", "--only", "du-dust")
        self.assertEqual(code, 0)
        self.assertIn("removed legacy matcher preset.operand", out)
        self.assertIn("removed legacy matcher preset.gone", out)
        self.assertIn("rule du-dust: replaced", out)
        state = self.get(self.gpath)
        self.assertEqual(state["matchers"], {"keep": {"command": "git"}})
        self.assertNotIn('"matcher"', json.dumps(state["rules"]))
        self.assertEqual(self.cli("status", "--problems")[1].strip(), "No problems.")

    def test_a_failed_install_changes_nothing(self) -> None:
        self.put(self.gpath, {"matchers": {"preset.operand": {"command": "x"}}})
        before = self.gpath.read_text()
        code, _, _ = self.cli("preset", "install", "modern-cli", "--only", "nope")
        self.assertEqual(code, 2)
        with mock.patch.object(cli, "table", side_effect=store.StoreError("corrupt")):
            code, _, _ = self.cli("preset", "install", "modern-cli")
        self.assertEqual(code, 2)
        self.assertEqual(self.gpath.read_text(), before)

    def test_a_rule_test_card_shows_the_built_ins_a_rule_uses(self) -> None:
        self.cli("preset", "install", "modern-cli", "--only", "du-dust")
        card = self.cli("rule", "test", "--id", "du-dust", "du -sh /tmp")[1]
        self.assertIn("`@preset/operand`", card)

    def test_status_lists_only_the_built_ins_that_enabled_rules_use(self) -> None:
        self.cli("preset", "install", "modern-cli", "--only", "du-dust,find-fd")
        self.assertRegex(self.cli("status")[1].split("**Matchers**")[1], r"`@preset/operand\s*` built-in")
        self.set_rule("du-dust", {"enabled": False})
        listing = self.cli("status")[1].split("**Matchers**")[1]
        self.assertNotIn("operand", listing)
        self.assertRegex(listing, r"`@preset/local-tool\s*` built-in · used by 1 rule: `find-fd`")

    def test_matcher_expansion_is_bounded_for_the_shipped_rules(self) -> None:
        for name in cli.preset_names():
            for rid, raw in cli.load_preset(name)["rules"].items():
                inliner = rulebuilder.Inliner({})
                inliner.rule(raw["match"])
                self.assertLess(inliner.nodes, rulebuilder.MAX_EXPANDED_NODES // 3, f"{name}/{rid}")

    def test_a_matchers_table_of_the_wrong_shape_is_reported(self) -> None:
        self.put(self.gpath, {"matchers": []})
        self.assertIn("global config: 'matchers' must be an object", self.cli("status", "--problems")[1])
        self.put(self.mpath, {"matchers": "x"})
        self.assertIn("managed file", self.cli("status", "--problems")[1])

    def test_an_agent_cannot_change_matchers_without_the_users_say_so(self) -> None:
        code, _, err = self.cli("matcher", "add", "x", "--json", '{"command": "x"}', agent=True)
        self.assertEqual((code, "matcher add changes guardrails configuration" in err), (3, True))
        self.assertEqual(self.cli("matcher", "rm", "x", agent=True)[0], 3)


class Hardening(AstIsolated):
    def test_an_unknown_built_in_invalidates_only_the_rules_using_it(self) -> None:
        good = rule({"command": "du", "has": {"use": "@preset/operand"}})
        self.put(self.gpath, {"rules": {"good": good, "stale": rule({"use": "@preset/nope"})}})
        reply = self.hook("du -sh /x")
        assert reply is not None
        self.assertEqual(reply["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("guardrails:good#", reply["hookSpecificOutput"]["permissionDecisionReason"])
        problems = self.cli("status", "--problems")[1]
        self.assertIn("rule stale: match: this guardrails version does not ship @preset/nope; update the plugin", problems)
        self.assertNotIn("rule good", problems)

    def test_the_deprecated_spelling_is_blamed_on_the_layer_that_wrote_the_match(self) -> None:
        old = rule({"matcher": "preset.operand", "command": "du"})
        self.put(self.mpath, {"rules": {"m": old}})
        self.put(self.gpath, {"rules": {"m": {"action": "deny"}, "find-fd": old, "mine": old}})
        problems = self.cli("status", "--problems")[1]
        self.assertIn("managed rules m write", problems)
        self.assertIn("global rules find-fd write", problems)
        self.assertIn("`guardrails preset install modern-cli` rewrites them", problems)
        self.assertRegex(problems, r"global rules mine write [^;\n]*\n")
        self.assertNotRegex(problems, r"global rules [^\n]*\bm\b[^\n]* write")

    def test_a_malformed_or_unreadable_preset_file_is_a_problem_not_a_crash(self) -> None:
        for name, text, why in (("bad.json", "{", "cannot be read"), ("list.json", "[]", "cannot be read"),
                                ("shape.json", '{"matchers": []}', "not an object")):
            preset_dir = self.tmp / name.removesuffix(".json")
            preset_dir.mkdir()
            (preset_dir / name).write_text(text)
            with self.subTest(name), mock.patch.object(rulebuilder, "PRESETS_DIR", preset_dir):
                with self.assertRaises(policy.Invalid) as caught:
                    rulebuilder.builtin_matchers.__wrapped__()
                self.assertIn(why, str(caught.exception))

    def test_a_built_in_name_defined_by_two_preset_files_is_refused(self) -> None:
        for name in ("a", "b"):
            (self.tmp / f"{name}.json").write_text('{"matchers": {"same": {"command": "x"}}}')
        with mock.patch.object(rulebuilder, "PRESETS_DIR", self.tmp), self.assertRaises(policy.Invalid) as caught:
            rulebuilder.builtin_matchers.__wrapped__()
        self.assertIn("built-in matcher same is defined by both a and b", str(caught.exception))

    def test_matcher_add_does_not_list_the_candidate_as_defined(self) -> None:
        code, _, err = self.cli("matcher", "add", "bad", "--json", '{"has": {"use": "@missing"}}')
        self.assertEqual(code, 2)
        self.assertIn("(this config defines: none)", err)
