from __future__ import annotations  # noqa: I001

import os
import unittest
from typing import Any, ClassVar

from helpers import Isolated

import policy


def rules_of(managed: Any, g: Any, p: Any) -> dict[str, policy.Rule]:
    return policy.effective_rules(managed, g, p)


def rule(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"match": {"program": "strings"}, "message": "use docs"}
    base.update(overrides)
    return base


class Validate(unittest.TestCase):
    def test_minimal_rule_is_valid(self) -> None:
        policy.Rule.from_json(rule())
        policy.Rule.from_json(rule(match={"ast": {"kind": "command"}}, requires=["rg"], messageShort="s"))

    def test_rejects_malformed(self) -> None:
        bad: list[Any] = [
            "notadict",
            {"match": {"program": "x"}},
            rule(message="  "),
            rule(match={}),
            rule(match={"args": "-9"}),
            rule(match={"program": "x", "bogus": 1}),
            rule(match={"program": "x", "mentions": ["a"]}),
            rule(match={"program": 3}),
            rule(match={"program": ["x", ""]}),
            rule(match={"builtin": "nope"}),
            rule(match={"builtin": "nope"}),
            rule(match={"program": "x y"}),
            rule(match={"program": "a/b"}),
            rule(action="block"),
            rule(retry="always"),
            rule(modes="reverse-engineering"),
            rule(requires=[]),
            rule(enabled="false"),
            rule(messageShort=3),
        ]
        for r in bad:
            with self.subTest(rule=r), self.assertRaises(policy.Invalid):
                policy.Rule.from_json(r)


class Layering(unittest.TestCase):
    def test_project_can_tighten(self) -> None:
        g = {"rules": {"r": rule(action="warn", retry="same-command", modes=["re", "ops"])}}
        p = {"rules": {"r": {"action": "deny", "retry": "none", "modes": ["re"]}}}
        eff = rules_of({}, g, p)["r"]
        self.assertEqual((eff.action, eff.retry, eff.modes), ("deny", "none", ("re",)))

    def test_project_cannot_relax(self) -> None:
        g = {"rules": {"r": rule(action="deny", retry="none", modes=["re"])}}
        p = {"rules": {"r": {"action": "warn", "retry": "same-command", "enabled": False, "modes": ["re", "x"]}}}
        eff = rules_of({}, g, p)["r"]
        self.assertEqual((eff.action, eff.retry, eff.enabled, eff.modes), ("deny", "none", True, ("re",)))

    def test_project_can_reenable(self) -> None:
        g = {"rules": {"r": rule(enabled=False)}}
        self.assertTrue(rules_of({}, g, {"rules": {"r": {"enabled": True}}})["r"].enabled)

    def test_project_replaces_text_and_match(self) -> None:
        g = {"rules": {"r": rule()}}
        eff = rules_of({}, g, {"rules": {"r": {"message": "m2", "match": {"program": "nm"}}}})["r"]
        self.assertEqual((eff.message, eff.match), ("m2", policy.Match(("strings",))))

    def test_project_requires_override_ignored(self) -> None:
        g = {"rules": {"r": rule(requires=["rg"])}}
        eff = rules_of({}, g, {"rules": {"r": {"requires": ["fd"]}}})["r"]
        self.assertEqual(eff.requires, ("rg",))

    def test_project_empty_message_falls_back_to_global(self) -> None:
        g = {"rules": {"r": rule(message="global text")}}
        eff = rules_of({}, g, {"rules": {"r": {"message": ""}}})["r"]
        self.assertEqual(eff.message, "global text")

    def test_project_only_rule_gets_defaults(self) -> None:
        eff = rules_of({}, {}, {"rules": {"p": rule()}})["p"]
        self.assertEqual((eff.action, eff.retry, eff.enabled, eff.modes), ("deny", "none", True, ()))

    def test_project_disabled_drops_project_entries_only(self) -> None:
        g = {"rules": {"r": rule(action="warn")}}
        p = {"enabled": False, "rules": {"r": {"action": "deny"}, "p": rule()}}
        eff = rules_of({}, g, p)
        self.assertEqual(sorted(eff), ["r"])
        self.assertEqual(eff["r"].action, "warn")

    def test_malformed_tables_ignored(self) -> None:
        self.assertEqual(rules_of({}, {"rules": ["x"]}, {"rules": {"a": "junk"}}), {})


class ManagedLayering(unittest.TestCase):
    MANAGED: ClassVar[dict[str, Any]] = {"rules": {"r": rule(action="deny", retry="none", requires=["rg"], modes=["re"])},
               "modes": {"re": {"description": "m", "agentMayEnable": False}}}

    def eff(self, g: Any = None, p: Any = None) -> policy.Rule:
        return rules_of(self.MANAGED, g or {}, p or {})["r"]

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
        eff = rules_of(managed, g, {"rules": {"r": {"messageShort": "s"}}})["r"]
        self.assertEqual((eff.action, eff.retry, eff.modes, eff.enabled), ("deny", "none", ("re",), True))
        self.assertEqual((eff.message, eff.message_short, eff.description), ("use docs", "short", "d"))

    def test_non_managed_rules_can_still_be_reworded(self) -> None:
        eff = rules_of({}, {"rules": {"r": rule()}}, {"rules": {"r": {"message": "p"}}})["r"]
        self.assertEqual(eff.message, "p")

    def test_modes_not_declared_in_managed_are_dropped_from_managed_rules(self) -> None:
        managed = {"rules": {"r": rule(modes=["re", "ghost"])}, "modes": {"re": {}}}
        lower = {"modes": {"ghost": {"active": True}}, "rules": {"r": {"modes": ["re", "ghost"]}}}
        for layers in ((lower, {}), ({}, lower)):
            self.assertEqual(rules_of(managed, *layers)["r"].modes, ("re",))
        self.assertEqual(rules_of({"rules": {"r": rule(modes=["re"])}}, {}, {})["r"].modes, ())

    def test_rule_without_modes_can_never_gain_any(self) -> None:
        managed = {"rules": {"r": rule()}}
        for lower in ({"rules": {"r": {"modes": ["re"]}}}, {"rules": {"r": {"modes": []}}}):
            for layers in ((lower, {}), ({}, lower)):
                self.assertEqual(rules_of(managed, *layers)["r"].modes, ())

    def test_lower_layers_keep_their_own_rules(self) -> None:
        eff = rules_of(self.MANAGED, {"rules": {"g": rule()}}, {"rules": {"p": rule()}})
        self.assertEqual(sorted(eff), ["g", "p", "r"])

    def test_invalid_override_falls_back_to_managed(self) -> None:
        self.assertEqual(self.eff({"rules": {"r": {"message": ""}}}).message, "use docs")

    def test_project_switched_off_still_leaves_managed(self) -> None:
        eff = rules_of(self.MANAGED, {}, {"enabled": False, "rules": {"p": rule()}})
        self.assertEqual(sorted(eff), ["r"])

    def test_global_tightening_carries_to_project_fold(self) -> None:
        managed = {"rules": {"r": rule(action="warn")}}
        eff = rules_of(managed, {"rules": {"r": {"action": "deny"}}},
                                     {"rules": {"r": {"action": "warn"}}})["r"]
        self.assertEqual(eff.action, "deny")

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
            self.assertFalse(policy.effective_modes(self.MANAGED, *layers)["re"].agent_may_enable)

    def test_lower_layers_can_forbid_agents(self) -> None:
        g = {"modes": {"ops": {"agentMayEnable": False}}}
        self.assertFalse(policy.effective_modes(self.MANAGED, g, {})["ops"].agent_may_enable)

    def test_active_cannot_be_switched_off(self) -> None:
        off = {"modes": {"always": {"active": False}}}
        for layers in ((off, {}), ({}, off), (off, off)):
            self.assertTrue(policy.effective_modes(self.MANAGED, *layers)["always"].active)

    def test_active_managed_mode_is_always_on(self) -> None:
        modes = policy.effective_modes(self.MANAGED, {"modes": {"always": {"active": False}}}, {})
        self.assertEqual(policy.active_modes(modes, policy.Session()),
                         {"always": policy.Activation(policy.Actor.USER, "persistently active")})

    def test_global_can_add_modes_and_switch_managed_ones_on(self) -> None:
        g = {"modes": {"mine": {"agentMayEnable": True}, "re": {"active": True}}}
        modes = policy.effective_modes(self.MANAGED, g, {})
        self.assertTrue(modes["mine"].agent_may_enable)
        self.assertEqual((modes["re"].active, modes["re"].description), (True, "m"))

    def test_project_cannot_switch_a_managed_mode_on(self) -> None:
        p = {"modes": {"re": {"active": True}, "mine": {"active": True}}}
        for g in ({}, {"modes": {"re": {"active": False}}}):
            modes = policy.effective_modes(self.MANAGED, g, p)
            self.assertFalse(modes["re"].active)
            self.assertTrue(modes["mine"].active)

    def test_project_activation_of_a_global_mode_still_counts(self) -> None:
        modes = policy.effective_modes({}, {"modes": {"m": {}}}, {"modes": {"m": {"active": True}}})
        self.assertTrue(modes["m"].active)

    def test_managed_and_global_activation_survive_a_project_layer(self) -> None:
        p = {"modes": {"re": {"active": False}}}
        g = {"modes": {"re": {"active": True}}}
        self.assertTrue(policy.effective_modes(self.MANAGED, g, p)["re"].active)

    def test_agent_session_activation_respects_managed_lock(self) -> None:
        modes = policy.effective_modes(self.MANAGED, {"modes": {"re": {"agentMayEnable": True}}}, {})
        session = policy.Session.from_json({"modes": {"re": {"by": "agent"}, "ops": {"by": "agent"}}})
        self.assertEqual(sorted(policy.active_modes(modes, session)), ["always", "ops"])


class ManagedLayer(unittest.TestCase):
    def test_modes_a_rule_lists_must_be_declared_in_the_file_and_problems_are_named(self) -> None:
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
    def test_merge_ands_agent_permission_and_ors_active(self) -> None:
        g = {"modes": {"re": {"description": "g", "agentMayEnable": True},
                       "ops": {"agentMayEnable": False, "active": True}}}
        p = {"modes": {"re": {"agentMayEnable": False, "active": True},
                       "ops": {"agentMayEnable": True},
                       "local": {"agentMayEnable": True}}}
        m = policy.effective_modes({}, g, p)
        self.assertEqual(m["re"], policy.Mode("g", False, True))
        self.assertFalse(m["ops"].agent_may_enable)
        self.assertTrue(m["ops"].active)
        self.assertTrue(m["local"].agent_may_enable)

    def test_active_modes_scopes(self) -> None:
        modes = {"re": policy.Mode("", True, False), "ops": policy.Mode("", False, True),
                 "inc": policy.Mode("", False, False)}
        session = policy.Session.from_json({"modes": {"re": {"by": "agent", "reason": "user said RE"},
                                                      "inc": {"by": "agent", "reason": "x"},
                                                      "ghost": {"by": "user"}}})
        active = policy.active_modes(modes, session)
        self.assertEqual(sorted(active), ["ops", "re"])
        self.assertEqual(active["re"].by, "agent")

    def test_user_session_activation_needs_no_agent_permission(self) -> None:
        modes = {"inc": policy.Mode()}
        self.assertIn("inc", policy.active_modes(modes, policy.Session.from_json({"modes": {"inc": {"by": "user"}}})))

    def test_malformed_session_ignored(self) -> None:
        modes = {"re": policy.Mode("", True, False)}
        self.assertEqual(policy.active_modes(modes, policy.Session.from_json({"modes": "junk"})), {})


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
        self.assertTrue(policy.requirements_met(policy.Rule.from_json(rule(requires=["fd", "fdfind"]))))
        self.assertFalse(policy.requirements_met(policy.Rule.from_json(rule(requires=["nope"]))))
        self.assertTrue(policy.requirements_met(policy.Rule.from_json(rule())))
