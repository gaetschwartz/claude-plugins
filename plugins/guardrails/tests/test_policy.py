from __future__ import annotations  # noqa: I001

import os
import unittest
from typing import Any, ClassVar

from helpers import Isolated

import policy
from shellwords import simple_commands


def rule(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"match": {"program": "strings"}, "message": "use docs"}
    base.update(overrides)
    return base


def matches(r: dict[str, Any], command: str) -> bool:
    try:
        cmds = simple_commands(command)
    except ValueError:
        cmds = None
    return policy.rule_matches(policy.with_defaults(r), command, cmds)


class Validate(unittest.TestCase):
    def test_minimal_rule_is_valid(self) -> None:
        policy.validate_rule(rule())
        policy.validate_rule(rule(match={"builtin": "grep-recursive"}, requires=["rg"], messageShort="s"))

    def test_rejects_malformed(self) -> None:
        bad: list[Any] = [
            "notadict",
            {"match": {"program": "x"}},
            rule(message="  "),
            rule(match={}),
            rule(match={"args": "-9"}),
            rule(match={"program": "x", "bogus": 1}),
            rule(match={"program": 3}),
            rule(match={"program": ["x", ""]}),
            rule(match={"builtin": "nope"}),
            rule(match={"regex": "("}),
            rule(match={"program": "x", "args": "["}),
            rule(action="block"),
            rule(retry="always"),
            rule(modes="reverse-engineering"),
            rule(requires=[]),
            rule(enabled="false"),
            rule(messageShort=3),
            rule(tool=5),
        ]
        for r in bad:
            with self.subTest(rule=r), self.assertRaises(policy.Invalid):
                policy.validate_rule(r)


class Layering(unittest.TestCase):
    def test_project_can_tighten(self) -> None:
        g = {"rules": {"r": rule(action="warn", retry="same-command", modes=["re", "ops"])}}
        p = {"rules": {"r": {"action": "deny", "retry": "none", "modes": ["re"]}}}
        eff = policy.effective_rules({}, g, p)["r"]
        self.assertEqual((eff["action"], eff["retry"], eff["modes"]), ("deny", "none", ["re"]))

    def test_project_cannot_relax(self) -> None:
        g = {"rules": {"r": rule(action="deny", retry="none", modes=["re"])}}
        p = {"rules": {"r": {"action": "warn", "retry": "same-command", "enabled": False, "modes": ["re", "x"]}}}
        eff = policy.effective_rules({}, g, p)["r"]
        self.assertEqual((eff["action"], eff["retry"], eff["enabled"], eff["modes"]), ("deny", "none", True, ["re"]))

    def test_project_can_reenable(self) -> None:
        g = {"rules": {"r": rule(enabled=False)}}
        self.assertTrue(policy.effective_rules({}, g, {"rules": {"r": {"enabled": True}}})["r"]["enabled"])

    def test_project_replaces_text_and_match(self) -> None:
        g = {"rules": {"r": rule()}}
        eff = policy.effective_rules({}, g, {"rules": {"r": {"message": "m2", "match": {"program": "nm"}}}})["r"]
        self.assertEqual((eff["message"], eff["match"]), ("m2", {"program": "strings"}))

    def test_project_match_override_ignored(self) -> None:
        g = {"rules": {"r": rule()}}
        eff = policy.effective_rules({}, g, {"rules": {"r": {"match": {"program": "nm"}}}})["r"]
        self.assertEqual(eff["match"], {"program": "strings"})

    def test_project_requires_override_ignored(self) -> None:
        g = {"rules": {"r": rule(requires=["rg"])}}
        eff = policy.effective_rules({}, g, {"rules": {"r": {"requires": ["fd"]}}})["r"]
        self.assertEqual(eff["requires"], ["rg"])

    def test_project_empty_message_falls_back_to_global(self) -> None:
        g = {"rules": {"r": rule(message="global text")}}
        eff = policy.effective_rules({}, g, {"rules": {"r": {"message": ""}}})["r"]
        self.assertEqual(eff["message"], "global text")

    def test_project_only_rule_gets_defaults(self) -> None:
        eff = policy.effective_rules({}, {}, {"rules": {"p": rule()}})["p"]
        self.assertEqual((eff["action"], eff["retry"], eff["enabled"], eff["tool"], eff["modes"]),
                         ("deny", "none", True, "Bash", []))

    def test_project_disabled_drops_project_entries_only(self) -> None:
        g = {"rules": {"r": rule(action="warn")}}
        p = {"enabled": False, "rules": {"r": {"action": "deny"}, "p": rule()}}
        eff = policy.effective_rules({}, g, p)
        self.assertEqual(sorted(eff), ["r"])
        self.assertEqual(eff["r"]["action"], "warn")

    def test_malformed_tables_ignored(self) -> None:
        self.assertEqual(policy.effective_rules({}, {"rules": ["x"]}, {"rules": {"a": "junk"}}), {})


class ManagedLayering(unittest.TestCase):
    MANAGED: ClassVar[dict[str, Any]] = {"rules": {"r": rule(action="deny", retry="none", requires=["rg"], modes=["re"])},
               "modes": {"re": {"description": "m", "agentMayEnable": False}}}

    def eff(self, g: Any = None, p: Any = None) -> dict[str, Any]:
        return policy.effective_rules(self.MANAGED, g or {}, p or {})["r"]

    def test_lower_layers_cannot_loosen(self) -> None:
        loosening = {"action": "warn", "retry": "same-command", "enabled": False, "modes": ["re", "extra"],
                     "match": {"program": "nm"}, "requires": ["fd"]}
        for layer in ("global", "project", "both"):
            with self.subTest(layer=layer):
                g = {"rules": {"r": dict(loosening)}} if layer in ("global", "both") else {}
                p = {"rules": {"r": dict(loosening)}} if layer in ("project", "both") else {}
                eff = self.eff(g, p)
                self.assertEqual((eff["action"], eff["retry"], eff["enabled"], eff["modes"]),
                                 ("deny", "none", True, ["re"]))
                self.assertEqual((eff["match"], eff["requires"]), ({"program": "strings"}, ["rg"]))

    def test_each_loosening_is_ignored_on_its_own(self) -> None:
        for field, value in (("action", "warn"), ("retry", "same-command"), ("enabled", False),
                             ("modes", ["re", "extra"]), ("match", {"program": "nm"}), ("requires", ["fd"])):
            with self.subTest(field=field):
                base = self.eff()
                self.assertEqual(self.eff({"rules": {"r": {field: value}}}), base)
                self.assertEqual(self.eff(None, {"rules": {"r": {field: value}}}), base)

    def test_lower_layers_can_tighten_but_not_reword(self) -> None:
        managed = {"rules": {"r": rule(action="warn", retry="same-command", modes=["re", "ops"], enabled=False,
                                       messageShort="short", description="d")},
                   "modes": {"re": {}, "ops": {}}}
        g = {"rules": {"r": {"action": "deny", "retry": "none", "modes": ["re"], "enabled": True, "message": "g",
                             "description": "gd"}}}
        eff = policy.effective_rules(managed, g, {"rules": {"r": {"messageShort": "s"}}})["r"]
        self.assertEqual((eff["action"], eff["retry"], eff["modes"], eff["enabled"]), ("deny", "none", ["re"], True))
        self.assertEqual((eff["message"], eff["messageShort"], eff["description"]), ("use docs", "short", "d"))

    def test_non_managed_rules_can_still_be_reworded(self) -> None:
        eff = policy.effective_rules({}, {"rules": {"r": rule()}}, {"rules": {"r": {"message": "p"}}})["r"]
        self.assertEqual(eff["message"], "p")

    def test_modes_not_declared_in_managed_are_dropped_from_managed_rules(self) -> None:
        managed = {"rules": {"r": rule(modes=["re", "ghost"])}, "modes": {"re": {}}}
        lower = {"modes": {"ghost": {"active": True}}, "rules": {"r": {"modes": ["re", "ghost"]}}}
        for layers in ((lower, {}), ({}, lower)):
            self.assertEqual(policy.effective_rules(managed, *layers)["r"]["modes"], ["re"])
        self.assertEqual(policy.effective_rules({"rules": {"r": rule(modes=["re"])}}, {}, {})["r"]["modes"], [])

    def test_rule_without_modes_can_never_gain_any(self) -> None:
        managed = {"rules": {"r": rule()}}
        for lower in ({"rules": {"r": {"modes": ["re"]}}}, {"rules": {"r": {"modes": []}}}):
            for layers in ((lower, {}), ({}, lower)):
                self.assertEqual(policy.effective_rules(managed, *layers)["r"]["modes"], [])

    def test_lower_layers_keep_their_own_rules(self) -> None:
        eff = policy.effective_rules(self.MANAGED, {"rules": {"g": rule()}}, {"rules": {"p": rule()}})
        self.assertEqual(sorted(eff), ["g", "p", "r"])

    def test_invalid_override_falls_back_to_managed(self) -> None:
        self.assertEqual(self.eff({"rules": {"r": {"message": ""}}})["message"], "use docs")

    def test_project_switched_off_still_leaves_managed(self) -> None:
        eff = policy.effective_rules(self.MANAGED, {}, {"enabled": False, "rules": {"p": rule()}})
        self.assertEqual(sorted(eff), ["r"])

    def test_global_tightening_carries_to_project_fold(self) -> None:
        managed = {"rules": {"r": rule(action="warn")}}
        eff = policy.effective_rules(managed, {"rules": {"r": {"action": "deny"}}},
                                     {"rules": {"r": {"action": "warn"}}})["r"]
        self.assertEqual(eff["action"], "deny")

    def test_origins(self) -> None:
        m, g, p = {"rules": {"a": rule()}}, {"rules": {"a": {}, "b": rule()}}, {"rules": {"b": {}, "c": rule()}}
        self.assertEqual(policy.origins("rules", m, g, p),
                         {"a": ["managed", "global"], "b": ["global", "project"], "c": ["project"]})
        self.assertEqual(policy.origins("modes", m, g, p), {})


class ManagedModes(unittest.TestCase):
    MANAGED: ClassVar[dict[str, Any]] = {"modes": {"re": {"description": "m", "agentMayEnable": False},
                         "ops": {"agentMayEnable": True},
                         "always": {"active": True, "agentMayEnable": True}}}

    def test_agent_permission_cannot_be_flipped_on(self) -> None:
        flip = {"modes": {"re": {"agentMayEnable": True}}}
        for layers in ((flip, {}), ({}, flip), (flip, flip)):
            self.assertFalse(policy.effective_modes(self.MANAGED, *layers)["re"]["agentMayEnable"])

    def test_lower_layers_can_forbid_agents(self) -> None:
        g = {"modes": {"ops": {"agentMayEnable": False}}}
        self.assertFalse(policy.effective_modes(self.MANAGED, g, {})["ops"]["agentMayEnable"])

    def test_active_cannot_be_switched_off(self) -> None:
        off = {"modes": {"always": {"active": False}}}
        for layers in ((off, {}), ({}, off), (off, off)):
            self.assertTrue(policy.effective_modes(self.MANAGED, *layers)["always"]["active"])

    def test_active_managed_mode_is_always_on(self) -> None:
        modes = policy.effective_modes(self.MANAGED, {"modes": {"always": {"active": False}}}, {})
        self.assertEqual(policy.active_modes(modes, {"modes": {}}), {"always": {"by": "user",
                                                                             "reason": "persistently active"}})

    def test_global_can_add_modes_and_switch_managed_ones_on(self) -> None:
        g = {"modes": {"mine": {"agentMayEnable": True}, "re": {"active": True}}}
        modes = policy.effective_modes(self.MANAGED, g, {})
        self.assertTrue(modes["mine"]["agentMayEnable"])
        self.assertEqual((modes["re"]["active"], modes["re"]["description"]), (True, "m"))

    def test_project_cannot_switch_a_managed_mode_on(self) -> None:
        p = {"modes": {"re": {"active": True}, "mine": {"active": True}}}
        for g in ({}, {"modes": {"re": {"active": False}}}):
            modes = policy.effective_modes(self.MANAGED, g, p)
            self.assertFalse(modes["re"]["active"])
            self.assertTrue(modes["mine"]["active"])

    def test_project_activation_of_a_global_mode_still_counts(self) -> None:
        modes = policy.effective_modes({}, {"modes": {"m": {}}}, {"modes": {"m": {"active": True}}})
        self.assertTrue(modes["m"]["active"])

    def test_managed_and_global_activation_survive_a_project_layer(self) -> None:
        p = {"modes": {"re": {"active": False}}}
        g = {"modes": {"re": {"active": True}}}
        self.assertTrue(policy.effective_modes(self.MANAGED, g, p)["re"]["active"])

    def test_agent_session_activation_respects_managed_lock(self) -> None:
        modes = policy.effective_modes(self.MANAGED, {"modes": {"re": {"agentMayEnable": True}}}, {})
        session = {"modes": {"re": {"by": "agent"}, "ops": {"by": "agent"}}}
        self.assertEqual(sorted(policy.active_modes(modes, session)), ["always", "ops"])


class ManagedLayer(unittest.TestCase):
    R = rule()

    def test_no_sources_is_empty(self) -> None:
        self.assertEqual(policy.managed_layer([]), ({"rules": {}, "modes": {}}, []))

    def test_first_source_wins_and_later_ones_only_tighten_or_add(self) -> None:
        first = {"rules": {"r": rule(action="warn", retry="same-command", modes=["m"]), "keep": rule()},
                 "modes": {"m": {"agentMayEnable": False}}}
        second = {"rules": {"r": {**rule(), "message": "second", "action": "deny", "retry": "none", "modes": [],
                                  "enabled": False}, "new": rule()},
                  "modes": {"m": {"agentMayEnable": True, "active": True}, "n": {}}}
        layer, problems = policy.managed_layer([("a", first), ("b", second)])
        self.assertEqual(problems, [])
        r = layer["rules"]["r"]
        self.assertEqual((r["action"], r["retry"], r["modes"], r["enabled"], r["message"]),
                         ("deny", "none", [], True, "use docs"))
        self.assertEqual(sorted(layer["rules"]), ["keep", "new", "r"])
        self.assertEqual((layer["modes"]["m"]["agentMayEnable"], layer["modes"]["m"]["active"]), (False, False))
        self.assertIn("n", layer["modes"])

    def test_problems(self) -> None:
        state = {"rules": {"bad": {"message": ""}, "ok": rule(modes=["ghost"]), "junk": 1}, "modes": ["x"]}
        problems = policy.managed_layer([("/p", state)])[1]
        self.assertEqual(len(problems), 4, problems)
        self.assertTrue(any("rules entry 'junk' is not an object" in p for p in problems))
        self.assertTrue(any("'modes' must be an object" in p for p in problems))
        self.assertTrue(any("managed rule bad is invalid and ignored" in p for p in problems))
        self.assertTrue(any("rule ok lists mode 'ghost'" in p for p in problems))

    def test_later_source_cannot_switch_on_a_mode_an_earlier_one_declares(self) -> None:
        first = {"rules": {"r": rule(modes=["m"])}, "modes": {"m": {}}}
        second = {"modes": {"m": {"active": True}}}
        layer, problems = policy.managed_layer([("a", first), ("b", second)])
        self.assertEqual(problems, [])
        self.assertFalse(layer["modes"]["m"]["active"])
        rules = policy.effective_rules(layer, {}, {})
        modes = policy.effective_modes(layer, {}, {})
        self.assertEqual(policy.active_modes(modes, {}), {})
        self.assertEqual(policy.modes_of(rules["r"]), ["m"])

    def test_a_mode_first_declared_by_a_source_keeps_its_own_active(self) -> None:
        layer, _ = policy.managed_layer([("a", {}), ("b", {"modes": {"m": {"active": True}}})])
        self.assertTrue(layer["modes"]["m"]["active"])

    def test_later_source_cannot_add_modes_to_an_earlier_rule(self) -> None:
        first = {"rules": {"r": rule()}, "modes": {"m": {}}}
        second = {"rules": {"r": rule(modes=["m"])}, "modes": {"m": {}}}
        layer, _ = policy.managed_layer([("a", first), ("b", second)])
        self.assertEqual(policy.modes_of(layer["rules"]["r"]), [])

    def test_later_source_cannot_make_an_undeclared_mode_suspend_an_earlier_rule(self) -> None:
        first = {"rules": {"r": rule(modes=["m"])}}
        second = {"modes": {"m": {"active": True}}}
        layer, problems = policy.managed_layer([("a", first), ("b", second)])
        self.assertTrue(any("rule r lists mode 'm'" in p for p in problems))
        self.assertEqual(policy.modes_of(layer["rules"]["r"]), [])

    def test_non_dict_state_is_tolerated(self) -> None:
        self.assertEqual(policy.managed_layer([("/p", [])])[0], {"rules": {}, "modes": {}})


class Modes(unittest.TestCase):
    def test_merge_ands_agent_permission_and_ors_active(self) -> None:
        g = {"modes": {"re": {"description": "g", "agentMayEnable": True},
                       "ops": {"agentMayEnable": False, "active": True}}}
        p = {"modes": {"re": {"agentMayEnable": False, "active": True},
                       "ops": {"agentMayEnable": True},
                       "local": {"agentMayEnable": True}}}
        m = policy.effective_modes({}, g, p)
        self.assertEqual(m["re"], {"description": "g", "agentMayEnable": False, "active": True})
        self.assertFalse(m["ops"]["agentMayEnable"])
        self.assertTrue(m["ops"]["active"])
        self.assertTrue(m["local"]["agentMayEnable"])

    def test_active_modes_scopes(self) -> None:
        modes = {"re": {"description": "", "agentMayEnable": True, "active": False},
                 "ops": {"description": "", "agentMayEnable": False, "active": True},
                 "inc": {"description": "", "agentMayEnable": False, "active": False}}
        session = {"modes": {"re": {"by": "agent", "reason": "user said RE"},
                             "inc": {"by": "agent", "reason": "x"},
                             "ghost": {"by": "user"}}}
        active = policy.active_modes(modes, session)
        self.assertEqual(sorted(active), ["ops", "re"])
        self.assertEqual(active["re"]["by"], "agent")

    def test_user_session_activation_needs_no_agent_permission(self) -> None:
        modes = {"inc": {"description": "", "agentMayEnable": False, "active": False}}
        self.assertIn("inc", policy.active_modes(modes, {"modes": {"inc": {"by": "user"}}}))

    def test_malformed_session_ignored(self) -> None:
        modes = {"re": {"description": "", "agentMayEnable": True, "active": False}}
        self.assertEqual(policy.active_modes(modes, {"modes": "junk"}), {})


class Matching(unittest.TestCase):
    def test_program(self) -> None:
        r = rule()
        for c in ["strings /bin/ls", "sudo strings x | head", "/usr/bin/strings a", "bash -c 'strings a'",
                  "x=$(strings a)", "cd /tmp && strings a"]:
            with self.subTest(command=c):
                self.assertTrue(matches(r, c))
        for c in ["grep strings file", "echo strings", "man strings", "command -v strings", "which strings",
                  "cat <<'EOF' > notes.md\nstrings are fun\nEOF", "rg strings"]:
            with self.subTest(command=c):
                self.assertFalse(matches(r, c))

    def test_program_list_and_args(self) -> None:
        r = rule(match={"program": ["kill"], "args": r"(^|\s)-(9|KILL)(\s|$)"})
        self.assertTrue(matches(r, "kill -9 123"))
        self.assertFalse(matches(r, "kill 123"))
        self.assertFalse(matches(r, "echo kill -9"))

    def test_regex_on_raw_text(self) -> None:
        r = rule(match={"regex": r"git\s+push\s+--force"})
        self.assertTrue(matches(r, "git push --force origin"))
        self.assertFalse(matches(r, "git push origin"))

    def test_builtin_grep_recursive(self) -> None:
        r = rule(match={"builtin": "grep-recursive"})
        for c in ["grep -rn foo .", "ps | grep -R x", "grep -d recurse x .", "egrep -r x ."]:
            with self.subTest(command=c):
                self.assertTrue(matches(r, c))
        for c in ["grep -e r file", "grep foo file", "git grep -r foo"]:
            with self.subTest(command=c):
                self.assertFalse(matches(r, c))

    def test_unbalanced_quotes_fallback(self) -> None:
        self.assertTrue(matches(rule(), "echo 'x; strings /bin/ls"))
        self.assertFalse(matches(rule(match={"builtin": "grep-recursive"}), "echo 'x; grep -r foo ."))
        self.assertTrue(matches(rule(match={"program": "kill", "args": "-9"}), "echo 'x; kill -9 1"))
        self.assertFalse(matches(rule(match={"program": "kill", "args": "-9"}), "echo 'x; kill 1"))


class Rendering(Isolated):
    def make_tool(self, name: str) -> str:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        return str(bin_dir)

    def test_which_placeholder_prefers_installed(self) -> None:
        os.environ["PATH"] = self.make_tool("fdfind")
        self.assertEqual(policy.render("use {which:fd|fdfind} now"), "use fdfind now")
        os.environ["PATH"] = str(self.tmp / "empty")
        self.assertEqual(policy.render("use {which:fd|fdfind}; keep {} and {/}"), "use fd; keep {} and {/}")

    def test_requires(self) -> None:
        os.environ["PATH"] = self.make_tool("fdfind")
        self.assertTrue(policy.requirements_met(rule(requires=["fd", "fdfind"])))
        self.assertFalse(policy.requirements_met(rule(requires=["nope"])))
        self.assertTrue(policy.requirements_met(rule()))
