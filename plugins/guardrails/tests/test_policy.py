from __future__ import annotations  # noqa: I001

import os
import unittest
from typing import Any, ClassVar

from helpers import Isolated

import policy


def rules_of(managed: Any, g: Any, p: Any) -> dict[str, policy.Rule]:
    return policy.effective_rules(managed, g, p)


def rule(**overrides: Any) -> dict[str, Any]:
    return {"match": {"program": "strings"}, "message": "use docs", **overrides}


def shape(eff: policy.Rule) -> tuple[Any, ...]:
    return (str(eff.action), str(eff.retry), eff.enabled, eff.modes)


class Validate(unittest.TestCase):
    def test_minimal_and_full_rules_are_valid_and_malformed_ones_are_not(self) -> None:
        policy.Rule.from_json(rule())
        policy.Rule.from_json(rule(match={"ast": {"kind": "command"}}, requires=["rg"], messageShort="s"))
        bad: list[Any] = [
            "notadict", {"match": {"program": "x"}}, rule(message="  "), rule(match={}), rule(match={"args": "-9"}),
            rule(match={"program": "x", "bogus": 1}), rule(match={"program": 3}), rule(match={"program": ["x", ""]}),
            rule(match={"builtin": "nope"}), rule(match={"program": "x y"}), rule(match={"program": "a/b"}),
            rule(action="block"), rule(retry="always"), rule(modes="reverse-engineering"), rule(requires=[]),
            rule(enabled="false"), rule(messageShort=3),
        ]
        for r in bad:
            with self.subTest(rule=r), self.assertRaises(policy.Invalid):
                policy.Rule.from_json(r)


class Layering(unittest.TestCase):
    def test_a_project_entry_only_tightens_a_global_rule(self) -> None:
        loose = rule(action="warn", retry="same-command", modes=["re", "ops"])
        tight = rule(action="deny", retry="none", modes=["re"])
        cases = [
            ("tighten", loose, {"action": "deny", "retry": "none", "modes": ["re"]}, ("deny", "none", True, ("re",))),
            ("cannot relax", tight, {"action": "warn", "retry": "same-command", "enabled": False, "modes": ["re", "x"]},
             ("deny", "none", True, ("re",))),
            ("re-enable", rule(enabled=False), {"enabled": True}, ("deny", "none", True, ())),
            ("requires ignored", rule(requires=["rg"]), {"requires": ["fd"]}, ("deny", "none", True, ())),
        ]
        for name, base, override, expected in cases:
            with self.subTest(name):
                eff = rules_of({}, {"rules": {"r": base}}, {"rules": {"r": override}})["r"]
                self.assertEqual(shape(eff), expected)
                self.assertEqual(eff.requires, tuple(base.get("requires", ())))

    def test_text_is_replaced_but_match_is_not_and_an_empty_message_falls_back(self) -> None:
        g = {"rules": {"r": rule()}}
        eff = rules_of({}, g, {"rules": {"r": {"message": "m2", "match": {"program": "nm"}}}})["r"]
        self.assertEqual((eff.message, eff.match), ("m2", policy.Match(("strings",))))
        self.assertEqual(rules_of({}, g, {"rules": {"r": {"message": ""}}})["r"].message, "use docs")

    def test_project_only_rules_get_defaults_and_a_disabled_project_drops_its_entries(self) -> None:
        self.assertEqual(shape(rules_of({}, {}, {"rules": {"p": rule()}})["p"]), ("deny", "none", True, ()))
        eff = rules_of({}, {"rules": {"r": rule(action="warn")}}, {"enabled": False, "rules": {"r": {"action": "deny"}, "p": rule()}})
        self.assertEqual({k: str(v.action) for k, v in eff.items()}, {"r": "warn"})
        self.assertEqual(rules_of({}, {"rules": ["x"]}, {"rules": {"a": "junk"}}), {})


class ManagedLayering(unittest.TestCase):
    MANAGED: ClassVar[dict[str, Any]] = {"rules": {"r": rule(action="deny", retry="none", requires=["rg"], modes=["re"])},
                                         "modes": {"re": {"description": "m", "agentMayEnable": False}}}

    def eff(self, g: Any = None, p: Any = None) -> policy.Rule:
        return rules_of(self.MANAGED, g or {}, p or {})["r"]

    def test_each_loosening_is_ignored_on_its_own_and_an_invalid_override_falls_back(self) -> None:
        for field, value in (("action", "warn"), ("retry", "same-command"), ("enabled", False),
                             ("modes", ["re", "extra"]), ("match", {"program": "nm"}), ("requires", ["fd"]), ("message", "")):
            with self.subTest(field=field):
                base = self.eff()
                self.assertEqual(self.eff({"rules": {"r": {field: value}}}), base)
                self.assertEqual(self.eff(None, {"rules": {"r": {field: value}}}), base)

    def test_lower_layers_tighten_but_never_reword_a_managed_rule_and_still_reword_their_own(self) -> None:
        managed = {"rules": {"r": rule(action="warn", retry="same-command", modes=["re", "ops"], enabled=False,
                                       messageShort="short", description="d")}, "modes": {"re": {}, "ops": {}}}
        g = {"rules": {"r": {"action": "deny", "retry": "none", "modes": ["re"], "enabled": True, "message": "g",
                             "description": "gd"}}}
        eff = rules_of(managed, g, {"rules": {"r": {"messageShort": "s"}}})["r"]
        self.assertEqual(shape(eff), ("deny", "none", True, ("re",)))
        self.assertEqual((eff.message, eff.message_short, eff.description), ("use docs", "short", "d"))
        self.assertEqual(rules_of({}, {"rules": {"r": rule()}}, {"rules": {"r": {"message": "p"}}})["r"].message, "p")
        self.assertEqual(sorted(rules_of(self.MANAGED, {"rules": {"g": rule()}}, {"rules": {"p": rule()}})), ["g", "p", "r"])
        self.assertEqual(sorted(rules_of(self.MANAGED, {}, {"enabled": False, "rules": {"p": rule()}})), ["r"])

    def test_managed_rules_keep_only_the_modes_the_managed_file_declares_and_never_gain_any(self) -> None:
        managed = {"rules": {"r": rule(modes=["re", "ghost"])}, "modes": {"re": {}}}
        lower = {"modes": {"ghost": {"active": True}}, "rules": {"r": {"modes": ["re", "ghost"]}}}
        for layers in ((lower, {}), ({}, lower)):
            self.assertEqual(rules_of(managed, *layers)["r"].modes, ("re",))
        self.assertEqual(rules_of({"rules": {"r": rule(modes=["re"])}}, {}, {})["r"].modes, ())
        for lower in ({"rules": {"r": {"modes": ["re"]}}}, {"rules": {"r": {"modes": []}}}):
            for layers in ((lower, {}), ({}, lower)):
                self.assertEqual(rules_of({"rules": {"r": rule()}}, *layers)["r"].modes, ())

    def test_a_global_tightening_carries_through_the_project_fold(self) -> None:
        managed = {"rules": {"r": rule(action="warn")}}
        eff = rules_of(managed, {"rules": {"r": {"action": "deny"}}}, {"rules": {"r": {"action": "warn"}}})["r"]
        self.assertEqual(str(eff.action), "deny")

    def test_origins(self) -> None:
        m, g, p = {"rules": {"a": rule()}}, {"rules": {"a": {}, "b": rule()}}, {"rules": {"b": {}, "c": rule()}}
        self.assertEqual(policy.origins("rules", m, g, p),
                         {"a": ["managed", "global"], "b": ["global", "project"], "c": ["project"]})
        self.assertEqual(policy.origins("modes", m, g, p), {})

    def test_the_managed_layer_names_what_is_wrong_with_the_file(self) -> None:
        state = {"rules": {"bad": {"message": ""}, "ok": rule(modes=["ghost"]), "junk": 1}, "modes": ["x"]}
        layer, problems = policy.managed_layer(state, "/p")
        self.assertEqual(len(problems), 4, problems)
        for needle in ("rules entry 'junk' is not an object", "'modes' must be an object",
                       "managed rule bad is invalid and ignored", "rule ok lists mode 'ghost'"):
            self.assertTrue(any(needle in p for p in problems), needle)
        self.assertEqual(policy.json_modes(layer["rules"]["ok"]), [])
        layer = policy.managed_layer({"rules": {"fine": rule(modes=["m"])}, "modes": {"m": {"active": True}}}, "/p")[0]
        self.assertEqual((policy.json_modes(layer["rules"]["fine"]), layer["modes"]["m"]["active"]), (["m"], True))


class Modes(unittest.TestCase):
    MANAGED: ClassVar[dict[str, Any]] = {"modes": {"re": {"description": "m", "agentMayEnable": False},
                                                   "ops": {"agentMayEnable": True},
                                                   "always": {"active": True, "agentMayEnable": True}}}

    def test_a_managed_mode_cannot_be_loosened_by_a_lower_layer(self) -> None:
        flip, off = {"modes": {"re": {"agentMayEnable": True}}}, {"modes": {"always": {"active": False}}}
        for layers in ((flip, {}), ({}, flip), (flip, flip)):
            self.assertFalse(policy.effective_modes(self.MANAGED, *layers)["re"].agent_may_enable)
        for layers in ((off, {}), ({}, off), (off, off)):
            self.assertTrue(policy.effective_modes(self.MANAGED, *layers)["always"].active)
        self.assertEqual(policy.active_modes(policy.effective_modes(self.MANAGED, off, {}), policy.Session()),
                         {"always": policy.Activation(policy.Actor.USER, "persistently active")})
        forbid = {"modes": {"ops": {"agentMayEnable": False}}}
        self.assertFalse(policy.effective_modes(self.MANAGED, forbid, {})["ops"].agent_may_enable)

    def test_global_adds_and_activates_modes_but_a_project_cannot_switch_a_managed_mode_on(self) -> None:
        g = {"modes": {"mine": {"agentMayEnable": True}, "re": {"active": True}}}
        modes = policy.effective_modes(self.MANAGED, g, {})
        self.assertEqual((modes["mine"].agent_may_enable, modes["re"].active, modes["re"].description), (True, True, "m"))
        p = {"modes": {"re": {"active": True}, "mine": {"active": True}}}
        for g2 in ({}, {"modes": {"re": {"active": False}}}):
            modes = policy.effective_modes(self.MANAGED, g2, p)
            self.assertEqual((modes["re"].active, modes["mine"].active), (False, True))
        self.assertTrue(policy.effective_modes(self.MANAGED, g, {"modes": {"re": {"active": False}}})["re"].active)
        self.assertTrue(policy.effective_modes({}, {"modes": {"m": {}}}, {"modes": {"m": {"active": True}}})["m"].active)

    def test_merge_ands_agent_permission_and_ors_active(self) -> None:
        g = {"modes": {"re": {"description": "g", "agentMayEnable": True}, "ops": {"agentMayEnable": False, "active": True}}}
        p = {"modes": {"re": {"agentMayEnable": False, "active": True}, "ops": {"agentMayEnable": True},
                       "local": {"agentMayEnable": True}}}
        m = policy.effective_modes({}, g, p)
        self.assertEqual(m["re"], policy.Mode("g", False, True))
        self.assertEqual((m["ops"].agent_may_enable, m["ops"].active, m["local"].agent_may_enable), (False, True, True))

    def test_which_modes_are_on_for_a_session(self) -> None:
        modes = {"re": policy.Mode("", True, False), "ops": policy.Mode("", False, True),
                 "inc": policy.Mode("", False, False)}
        session = policy.Session.from_json({"modes": {"re": {"by": "agent", "reason": "user said RE"},
                                                      "inc": {"by": "agent", "reason": "x"}, "ghost": {"by": "user"}}})
        active = policy.active_modes(modes, session)
        self.assertEqual((sorted(active), active["re"].by), (["ops", "re"], "agent"))
        self.assertIn("inc", policy.active_modes(modes, policy.Session.from_json({"modes": {"inc": {"by": "user"}}})))
        self.assertEqual(policy.active_modes(modes, policy.Session.from_json({"modes": "junk"})), {"ops": policy.Activation(
            policy.Actor.USER, "persistently active")})
        locked = policy.effective_modes(self.MANAGED, {"modes": {"re": {"agentMayEnable": True}}}, {})
        agent = policy.Session.from_json({"modes": {"re": {"by": "agent"}, "ops": {"by": "agent"}}})
        self.assertEqual(sorted(policy.active_modes(locked, agent)), ["always", "ops"])


class Rendering(Isolated):
    def make_tool(self, name: str) -> str:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        return str(bin_dir)

    def test_which_placeholder_and_requires_prefer_what_is_installed(self) -> None:
        os.environ["PATH"] = self.make_tool("fdfind")
        self.assertEqual(policy.render("use {which:fd|fdfind} now"), "use fdfind now")
        self.assertTrue(policy.requirements_met(policy.Rule.from_json(rule(requires=["fd", "fdfind"]))))
        self.assertFalse(policy.requirements_met(policy.Rule.from_json(rule(requires=["nope"]))))
        self.assertTrue(policy.requirements_met(policy.Rule.from_json(rule())))
        os.environ["PATH"] = str(self.tmp / "empty")
        self.assertEqual(policy.render("use {which:fd|fdfind}; keep {} and {/}"), "use fd; keep {} and {/}")
