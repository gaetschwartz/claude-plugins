from __future__ import annotations  # noqa: I001

import os
import unittest
from typing import Any

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
        eff = policy.effective_rules(g, p)["r"]
        self.assertEqual((eff["action"], eff["retry"], eff["modes"]), ("deny", "none", ["re"]))

    def test_project_cannot_relax(self) -> None:
        g = {"rules": {"r": rule(action="deny", retry="none", modes=["re"])}}
        p = {"rules": {"r": {"action": "warn", "retry": "same-command", "enabled": False, "modes": ["re", "x"]}}}
        eff = policy.effective_rules(g, p)["r"]
        self.assertEqual((eff["action"], eff["retry"], eff["enabled"], eff["modes"]), ("deny", "none", True, ["re"]))

    def test_project_can_reenable(self) -> None:
        g = {"rules": {"r": rule(enabled=False)}}
        self.assertTrue(policy.effective_rules(g, {"rules": {"r": {"enabled": True}}})["r"]["enabled"])

    def test_project_replaces_text_and_match(self) -> None:
        g = {"rules": {"r": rule()}}
        eff = policy.effective_rules(g, {"rules": {"r": {"message": "m2", "match": {"program": "nm"}}}})["r"]
        self.assertEqual((eff["message"], eff["match"]), ("m2", {"program": "nm"}))

    def test_project_only_rule_gets_defaults(self) -> None:
        eff = policy.effective_rules({}, {"rules": {"p": rule()}})["p"]
        self.assertEqual((eff["action"], eff["retry"], eff["enabled"], eff["tool"], eff["modes"]),
                         ("deny", "none", True, "Bash", []))

    def test_project_disabled_drops_project_entries_only(self) -> None:
        g = {"rules": {"r": rule(action="warn")}}
        p = {"enabled": False, "rules": {"r": {"action": "deny"}, "p": rule()}}
        eff = policy.effective_rules(g, p)
        self.assertEqual(sorted(eff), ["r"])
        self.assertEqual(eff["r"]["action"], "warn")

    def test_malformed_tables_ignored(self) -> None:
        self.assertEqual(policy.effective_rules({"rules": ["x"]}, {"rules": {"a": "junk"}}), {})


class Modes(unittest.TestCase):
    def test_merge_ands_agent_permission_and_ors_active(self) -> None:
        g = {"modes": {"re": {"description": "g", "agentMayEnable": True},
                       "ops": {"agentMayEnable": False, "active": True}}}
        p = {"modes": {"re": {"agentMayEnable": False, "active": True},
                       "ops": {"agentMayEnable": True},
                       "local": {"agentMayEnable": True}}}
        m = policy.effective_modes(g, p)
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
