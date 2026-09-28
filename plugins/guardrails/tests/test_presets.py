from __future__ import annotations

import json
import os

import cli
import policy
from helpers import Isolated


class Presets(Isolated):
    def test_all_presets_are_valid_and_self_contained(self) -> None:
        self.assertEqual(cli.preset_names(), ["docs-first", "modern-cli", "process-safety"])
        for name in cli.preset_names():
            preset = cli.load_preset(name)
            self.assertTrue(preset.get("description"))
            for rid, rule in preset["rules"].items():
                with self.subTest(preset=name, rule=rid):
                    policy.validate_rule(rule)
                    for mode in policy.modes_of(rule):
                        self.assertIn(mode, preset.get("modes", {}))

    def test_modern_cli_sheet_is_fully_substituted(self) -> None:
        rules = cli.load_preset("modern-cli")["rules"]
        self.assertEqual(rules["find-fd"]["message"], rules["grep-rg"]["message"])
        self.assertEqual(rules["find-fd"]["messageShort"], rules["grep-rg"]["messageShort"])
        sheet = rules["find-fd"]["message"]
        for leftover in ("{FD}", "{MARKER}", "FIND_OK", "GREP_OK", "shown once per context"):
            self.assertNotIn(leftover, sheet)
        self.assertIn("{which:fd|fdfind} -e py", sheet)
        self.assertEqual(rules["find-fd"]["requires"], ["fd", "fdfind"])
        self.assertEqual(rules["grep-rg"]["match"], {"builtin": "grep-recursive"})

    def test_list_and_show(self) -> None:
        code, out, _ = self.cli("preset", "list")
        self.assertEqual(code, 0)
        for name in ("docs-first", "modern-cli", "process-safety"):
            self.assertIn(f"{name}: ", out)
        code, out, _ = self.cli("preset", "show", "docs-first")
        self.assertEqual(code, 0)
        self.assertIn("no-strings", json.loads(out)["rules"])

    def test_install_global_and_idempotent(self) -> None:
        code, out, _ = self.cli("preset", "install", "docs-first")
        self.assertEqual(code, 0)
        self.assertIn("rule no-strings: added", out)
        self.assertIn("mode reverse-engineering: added (agent may enable: yes)", out)
        state = self.get(self.gpath)
        self.assertEqual(sorted(state["rules"]), ["binary-spelunking", "no-strings"])
        self.assertEqual(state["rules"]["no-strings"]["setBy"]["reason"], "preset docs-first")
        self.assertTrue(state["modes"]["reverse-engineering"]["agentMayEnable"])
        out = self.cli("preset", "install", "docs-first")[1]
        self.assertIn("rule no-strings: unchanged", out)
        self.assertIn("mode reverse-engineering: kept existing declaration", out)

    def test_install_subset_brings_only_referenced_modes(self) -> None:
        self.assertEqual(self.cli("preset", "install", "process-safety", "--only", "kill-9")[0], 0)
        state = self.get(self.gpath)
        self.assertEqual(sorted(state["rules"]), ["kill-9"])
        self.assertEqual(state["modes"], {})

    def test_install_unknown(self) -> None:
        self.assertEqual(self.cli("preset", "install", "nope")[0], 2)
        self.assertEqual(self.cli("preset", "install", "docs-first", "--only", "nope")[0], 2)
        self.assertEqual(self.cli("preset", "show", "../etc/passwd")[0], 2)

    def test_install_keeps_existing_mode_choice(self) -> None:
        self.cli("preset", "install", "docs-first")
        self.cli("mode", "declare", "reverse-engineering")
        self.cli("preset", "install", "docs-first")
        mode = self.get(self.gpath)["modes"]["reverse-engineering"]
        self.assertFalse(mode["agentMayEnable"])
        self.assertTrue(mode["description"])

    def test_install_requires_user(self) -> None:
        self.assertEqual(self.cli("preset", "install", "docs-first", agent=True)[0], 3)
        self.assertEqual(self.cli("preset", "install", "docs-first", "--as-user", agent=True)[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["no-strings"]["setBy"]["by"], "agent")

    def test_install_project(self) -> None:
        self.assertEqual(self.cli("preset", "install", "process-safety", "--project")[0], 0)
        self.assertIn("no-pkill", self.get(self.ppath)["rules"])

    def test_docs_first_end_to_end(self) -> None:
        self.cli("preset", "install", "docs-first")
        out = self.hook("strings /bin/ls")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.hook("strings /bin/ls"))
        warn = self.hook("nm -g libfoo.dylib")
        assert warn is not None
        self.assertIn("additionalContext", warn["hookSpecificOutput"])

    def test_modern_cli_needs_tools(self) -> None:
        self.cli("preset", "install", "modern-cli")
        os.environ["PATH"] = str(self.tmp / "empty")
        self.assertIsNone(self.hook("find . -name x"))
