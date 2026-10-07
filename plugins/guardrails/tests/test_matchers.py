from __future__ import annotations  # noqa: I001

import json
import time
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
    "pushing": {"matcher": "git", "has": {"regex": "^push$"}},
    "after-cd": {"follows": {"command": "cd", "stopBy": "end"}},
}


def rule(match: Any, **fields: Any) -> dict[str, Any]:
    return {"message": "m", "match": match, **fields}


class Expansion(AstIsolated):
    def kinds(self, spec: dict[str, Any], commands: dict[str, bool], matchers: Any = MATCHERS) -> None:
        parsed = policy.Rule.from_json(spec, matchers)
        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": parsed}).kinds["r"] is not None, expected)

    def test_a_reference_is_an_atom_that_composes_under_the_combinators_and_relations(self) -> None:
        self.kinds(rule({"all": [{"matcher": "git"}, {"not": {"has": {"regex": "^status$"}}}]}),
                   {"git push": True, "git status": False, "ls": False})
        self.kinds(rule({"any": [{"matcher": "pushing"}, {"command": "ls"}]}),
                   {"git push": True, "ls": True, "git log": False})
        self.kinds(rule({"command": "make", "inside": {"matcher": "pushing"}}), {"git push $(make)": False})
        self.kinds(rule({"command": "ls", "follows": {"matcher": "pushing", "stopBy": "end"}}),
                   {"git push && ls": True, "ls && git push": False})

    def test_keys_next_to_a_reference_are_anded_and_a_reference_keeps_redirect_transparency(self) -> None:
        spec = rule({"matcher": "git", "has": {"regex": "^push$"}})
        self.assertEqual(policy.Rule.from_json(spec, MATCHERS).match, policy.Rule.from_json(rule(MATCHERS["pushing"]),
                                                                                           MATCHERS).match)
        self.kinds(rule({"matcher": "after-cd", "command": "git"}), {"cd x && git log": True, "cd x && ls": False})
        self.kinds(rule({"matcher": "after-cd", "command": "git"}), {"cd x && git log >f": True, "git log >f": False})

    def test_message_cases_expand_references_in_matches(self) -> None:
        cases = [{"when": {"matches": {"matcher": "pushing"}}, "text": "pushed"}]
        parsed = policy.Rule.from_json(rule({"matcher": "git"}, messages=cases), MATCHERS)
        self.assertEqual(parsed.messages[0].when, {"matches": {"command": "git", "has": {"regex": "^push$"}}})
        self.assertEqual(matching.evaluate("git push", {"r": parsed}).details["r"].case, 0)
        self.assertIsNone(matching.evaluate("git log", {"r": parsed}).details["r"].case)

    def test_the_rule_names_the_matchers_it_used_including_nested_ones(self) -> None:
        self.assertEqual(policy.Rule.from_json(rule({"matcher": "pushing"}), MATCHERS).matchers, ("git", "pushing"))
        self.assertEqual(policy.Rule.from_json(rule({"command": "ls"}), MATCHERS).matchers, ())

    def test_a_changed_matcher_changes_the_hash_of_the_rules_that_use_it(self) -> None:
        spec = rule({"matcher": "git"})
        before = policy.rule_hash(policy.Rule.from_json(spec, MATCHERS))
        after = policy.rule_hash(policy.Rule.from_json(spec, {**MATCHERS, "git": {"command": "hg"}}))
        self.assertNotEqual(before, after)
        self.assertEqual(before, policy.rule_hash(policy.Rule.from_json(rule({"command": "git"}))))

    def test_missing_names_cycles_and_malformed_fragments_are_refused_with_a_message(self) -> None:
        refused = {"missing": (rule({"matcher": "nope"}), MATCHERS, "refers to no matcher 'nope' (this config defines: "),
                   "cycle": (rule({"matcher": "a"}), {"a": {"matcher": "b"}, "b": {"has": {"matcher": "a"}}},
                             "matcher cycle: a -> b -> a"),
                   "self": (rule({"matcher": "a"}), {"a": {"not": {"matcher": "a"}}}, "matcher cycle: a -> a"),
                   "not an object": (rule({"matcher": "a"}), {"a": "git"}, "matcher 'a' must be a non-empty rule object"),
                   "bad atom": (rule({"matcher": "a"}), {"a": {"command": "a b"}}, "matcher 'a'.command"),
                   "in a case": (rule({"command": "x"}, messages=[{"when": {"matches": {"matcher": "zz"}}, "text": "t"}]),
                                 MATCHERS, "refers to no matcher 'zz'"),
                   "no table": (rule({"matcher": "git"}), None, "refers to no matcher 'git' (this config defines: none)")}
        for name, (spec, matchers, text) in refused.items():
            with self.subTest(name), self.assertRaises(policy.Invalid) as caught:
                policy.Rule.from_json(spec, matchers)
            self.assertIn(text, str(caught.exception))


class Layers(AstIsolated):
    def test_a_layer_resolves_references_from_its_own_matchers_only(self) -> None:
        shared = {"git": {"command": "git"}}
        uses = rule({"matcher": "git"})
        found = policy.effective({"rules": {"m": uses}, "matchers": shared}, {"rules": {"g": uses}},
                                 {"rules": {"p": uses}, "matchers": {"git": {"command": "hg"}}})
        self.assertEqual(sorted(found.rules), ["m", "p"])
        self.assertIn("refers to no matcher 'git'", found.problems["g"])
        self.assertEqual(found.rules["m"].match, {"command": "git"})
        self.assertEqual(found.rules["p"].match, {"command": "hg"})

    def test_a_managed_layer_keeps_its_matchers_and_reports_a_broken_one(self) -> None:
        self.put(self.mpath, {"rules": {"m": rule({"matcher": "git"})}, "matchers": {"git": {"command": "git"},
                                                                                    "bad": {"matcher": "bad"}}})
        self.assertEqual(self.cli("status")[0], 0)
        layer, problems = store.load_managed()
        self.assertEqual(sorted(layer["matchers"]), ["bad", "git"])
        self.assertEqual(sorted(layer["rules"]), ["m"])
        self.assertTrue(any("managed matcher bad is invalid: matcher cycle: bad -> bad" in p for p in problems))
        self.assertEqual(matching.evaluate("git x", {"r": policy.effective(layer, {}, {}).rules["m"]}).kinds["r"], "direct")


class Cli(AstIsolated):
    def add_matcher(self, name: str, fragment: Any, *extra: str) -> tuple[int, str, str]:
        return self.cli("matcher", "add", name, "--json", json.dumps(fragment), *extra)

    def test_matcher_add_rule_add_status_test_and_rm_work_together(self) -> None:
        self.assertEqual(self.add_matcher("git", {"command": "git"})[0], 0)
        code, _, err = self.cli("rule", "add", "no-push", "--json", json.dumps(rule({"matcher": "pushing"})))
        self.assertEqual((code, "refers to no matcher 'pushing'" in err), (2, True))
        self.assertEqual(self.add_matcher("pushing", {"matcher": "git", "has": {"regex": "^push$"}})[0], 0)
        self.assertEqual(self.cli("rule", "add", "no-push", "--json", json.dumps(rule({"matcher": "pushing"})))[0], 0)
        self.assertEqual(sorted(self.get(self.gpath)["matchers"]), ["git", "pushing"])
        self.assertEqual(self.get(self.gpath)["rules"]["no-push"]["match"], {"matcher": "pushing"})

        status = self.cli("status")[1]
        self.assertIn("**Matchers**", status)
        self.assertRegex(status, r"`git\s*` global · used by 1 rule: `no-push`")
        self.assertIn("matchers `git`, `pushing`", status)
        self.assertIn("matchers `git`, `pushing`", self.cli("status", "--rule", "no-push")[1])

        card = self.cli("rule", "test", "--id", "no-push", "git push x", "git log")[1]
        self.assertIn('{"command":"git","has":{"regex":"^push$"}}', card)
        self.assertEqual(self.cli("rule", "test", "--json", json.dumps(rule({"matcher": "pushing"})), "git push")[0], 0)

        code, _, err = self.cli("matcher", "rm", "git")
        self.assertEqual((code, "rule no-push would stop loading" in err), (2, True))
        self.assertEqual(self.cli("matcher", "rm", "nope")[0], 2)
        self.assertEqual(self.cli("rule", "rm", "no-push")[0], 0)
        self.assertEqual(self.cli("matcher", "rm", "pushing")[0], 0)
        self.assertEqual(self.cli("matcher", "rm", "git")[0], 0)
        self.assertEqual(self.get(self.gpath)["matchers"], {})

    def test_matcher_add_refuses_a_cycle_and_a_replacement_that_breaks_a_rule(self) -> None:
        self.add_matcher("a", {"command": "a"})
        code, _, err = self.add_matcher("b", {"has": {"matcher": "b"}})
        self.assertEqual((code, "matcher cycle: b -> b" in err), (2, True))
        self.assertNotIn("b", self.get(self.gpath)["matchers"])
        self.cli("rule", "add", "r", "--json", json.dumps(rule({"matcher": "a"})))
        self.assertEqual(self.add_matcher("a", {"command": "a b"})[0], 2)
        self.assertEqual(self.get(self.gpath)["matchers"]["a"], {"command": "a"})
        self.assertIn("replaced matcher a", self.add_matcher("a", {"command": "c"})[1])

    def test_rule_set_match_resolves_against_the_config_it_changes(self) -> None:
        self.add_matcher("git", {"command": "git"})
        self.cli("rule", "add", "r", "--json", json.dumps(rule({"command": "x"})))
        self.assertEqual(self.set_rule("r", {"match": {"matcher": "git"}})[0], 0)
        self.assertEqual(self.set_rule("r", {"match": {"matcher": "nope"}})[0], 2)
        self.assertIsNotNone(self.hook("git log"))

    def test_the_hook_denies_through_a_matcher_and_a_matcher_edit_changes_the_label(self) -> None:
        self.put(self.gpath, {"rules": {"r": rule({"matcher": "git"})}, "matchers": {"git": {"command": "git"}}})
        first = self.hook("git log", session="a")
        assert first is not None
        label = first["hookSpecificOutput"]["permissionDecisionReason"].split("]")[0]
        self.put(self.gpath, {"rules": {"r": rule({"matcher": "git"})}, "matchers": {"git": {"command": ["git", "hg"]}}})
        second = self.hook("git log", session="b")
        assert second is not None
        self.assertNotEqual(label, second["hookSpecificOutput"]["permissionDecisionReason"].split("]")[0])

    def test_preset_install_brings_the_prefixed_matchers_its_rules_use_and_only_those(self) -> None:
        code, out, _ = self.cli("preset", "install", "shell-hygiene", "--only", "http-wait-exact-status")
        self.assertEqual(code, 0)
        self.assertIn("matcher preset.compares-2xx: added", out)
        self.assertIn("matcher preset.status-2xx: added", out)
        self.assertNotIn("tail-step", out)
        self.assertEqual(sorted(self.get(self.gpath)["matchers"]),
                         ["preset.compares-2xx", "preset.equality", "preset.status-2xx"])
        self.assertIn("matcher preset.status-2xx: unchanged", self.cli("preset", "install", "shell-hygiene", "--only",
                                                                       "http-wait-exact-status")[1])
        self.assertIn("matcher preset.operand: added", self.cli("preset", "install", "modern-cli")[1])
        before = self.get(self.gpath)["matchers"]
        self.assertEqual(self.cli("preset", "install", "docs-first")[0], 0)
        self.assertEqual(self.get(self.gpath)["matchers"], before)
        status = self.cli("status")[1]
        self.assertIn("matchers `preset.compares-2xx`", status)
        self.assertRegex(status, r"`preset.operand\s*` global · used by 2 rules: `cargo-nextest`, `du-dust`")

    def test_a_user_cannot_define_a_preset_prefixed_matcher_but_may_reuse_the_suffix(self) -> None:
        code, _, err = self.add_matcher("preset.mine", {"command": "x"})
        self.assertEqual((code, "reserved for presets" in err), (2, True))
        self.assertEqual(self.add_matcher("status-2xx", {"regex": "^200$"})[0], 0)
        self.cli("preset", "install", "shell-hygiene", "--only", "http-wait-exact-status")
        matchers = self.get(self.gpath)["matchers"]
        self.assertEqual(matchers["status-2xx"], {"regex": "^200$"})
        self.assertIn("preset.status-2xx", matchers)
        self.assertEqual(self.cli("matcher", "rm", "preset.equality")[0], 2)

    def test_a_rule_test_card_shows_the_prefixed_matchers(self) -> None:
        self.cli("preset", "install", "modern-cli", "--only", "du-dust")
        card = self.cli("rule", "test", "--id", "du-dust", "du -sh /tmp")[1]
        self.assertIn("matchers `preset.operand`", card)

    def test_a_reinstall_replaces_a_changed_matcher_so_a_release_can_upgrade_it(self) -> None:
        self.cli("preset", "install", "shell-hygiene", "--only", "http-wait-exact-status")
        shipped = self.get(self.gpath)["matchers"]["preset.status-2xx"]
        self.put(self.gpath, {**self.get(self.gpath), "matchers": {**self.get(self.gpath)["matchers"],
                                                                    "preset.status-2xx": {"regex": "^200$"}}})
        code, out, _ = self.cli("preset", "install", "shell-hygiene", "--only", "http-wait-exact-status")
        self.assertEqual(code, 0)
        self.assertIn("matcher preset.status-2xx: replaced", out)
        self.assertIn("matcher preset.compares-2xx: unchanged", out)
        self.assertEqual(self.get(self.gpath)["matchers"]["preset.status-2xx"], shipped)
        self.assertEqual(self.cli("status", "--problems")[1].strip(), "No problems.")

    def test_an_install_that_would_break_a_rule_of_the_config_changes_nothing(self) -> None:
        self.cli("preset", "install", "shell-hygiene", "--only", "http-wait-exact-status")
        before = self.gpath.read_text()
        with mock.patch.object(cli, "broken_rules", side_effect=[{}, {"mine": "broken"}]):
            code, _, err = self.cli("preset", "install", "shell-hygiene", "--only", "http-wait-exact-status")
        self.assertEqual((code, "rule mine would stop loading" in err), (2, True))
        self.assertEqual(self.gpath.read_text(), before)

    def test_matcher_expansion_is_bounded_while_it_expands(self) -> None:
        chain: dict[str, Any] = {"m0": {"command": "x"}}
        for i in range(1, 30):
            chain[f"m{i}"] = {"all": [{"matcher": f"m{i - 1}"}, {"matcher": f"m{i - 1}"}]}
        started = time.monotonic()
        with self.assertRaises(policy.Invalid) as caught:
            policy.Rule.from_json(rule({"matcher": "m29"}), chain)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("more than 2000 nodes", str(caught.exception))
        self.assertEqual(rulebuilder.matcher_problems(chain), {})
        for name in cli.preset_names():
            doc = cli.load_preset(name)
            for rid, raw in doc["rules"].items():
                inliner = rulebuilder.Inliner(doc.get("matchers", {}))
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
