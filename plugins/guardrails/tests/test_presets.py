from __future__ import annotations  # noqa: I001

import os
from typing import Any

from helpers import AstIsolated, plain

import cli
import engine
import matching
import policy


class Presets(AstIsolated):
    def test_all_presets_are_valid_and_self_contained(self) -> None:
        self.assertEqual(cli.preset_names(), ["docs-first", "modern-cli", "process-safety"])
        for name in cli.preset_names():
            preset = cli.load_preset(name)
            self.assertTrue(preset.get("description"))
            for rid, rule in preset["rules"].items():
                with self.subTest(preset=name, rule=rid):
                    policy.Rule.from_json(rule)
                    for mode in policy.json_modes(rule):
                        self.assertIn(mode, preset.get("modes", {}))

    def test_modern_cli_sheets_name_the_installed_binary(self) -> None:
        rules = cli.load_preset("modern-cli")["rules"]
        self.assertEqual(rules["find-fd"]["when"], {"bin": ["fd", "fdfind"]})
        self.assertEqual(rules["grep-rg"]["when"], {"bin": "rg"})
        self.assertEqual(rules["grep-rg"]["match"]["all"][0], {"command": ["grep", "egrep", "fgrep"]})
        fd_texts = [rules["find-fd"]["message"], rules["find-fd"]["messageShort"],
                    *(case["text"] for case in rules["find-fd"]["messages"])]
        for text in fd_texts:
            self.assertIn("{found} -h", text)
        for rid, rule in rules.items():
            for text in (rule["message"], rule["messageShort"], *(c["text"] for c in rule.get("messages", []))):
                with self.subTest(rule=rid):
                    self.assertNotIn("{which:", text)
                    self.assertNotRegex(text.replace("{found}", "").replace("{{", "").replace("}}", ""), "[{}]")

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
        self.cli("mode", "declare", "reverse-engineering")
        self.cli("preset", "install", "docs-first")
        mode = self.get(self.gpath)["modes"]["reverse-engineering"]
        self.assertEqual((mode["agentMayEnable"], bool(mode["description"])), (False, True))

    def test_install_subset_brings_only_referenced_modes(self) -> None:
        self.assertEqual(self.cli("preset", "install", "process-safety", "--only", "kill-9")[0], 0)
        state = self.get(self.gpath)
        self.assertEqual(sorted(state["rules"]), ["kill-9"])
        self.assertEqual(state["modes"], {})

    def test_install_unknown(self) -> None:
        self.assertEqual(self.cli("preset", "install", "nope")[0], 2)
        self.assertEqual(self.cli("preset", "install", "docs-first", "--only", "nope")[0], 2)
        self.assertEqual(self.cli("preset", "show", "../etc/passwd")[0], 2)

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
        self.assertIsNone(self.hook("grep -r x ."))

    def stub_path(self, *names: str) -> str:
        folder = self.tmp / ("bin-" + "-".join(names))
        folder.mkdir()
        for name in names:
            (folder / name).write_text("#!/bin/sh\n")
            (folder / name).chmod(0o755)
        return str(folder)

    def denial(self, command: str) -> str | None:
        out = self.hook(command, session=f"s-{command}-{os.environ['PATH']}")
        if out is None:
            return None
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_modern_cli_catches_and_passes_with_the_binary_it_finds(self) -> None:
        self.cli("preset", "install", "modern-cli")
        catches = {"find . -name x": "find-fd", "sudo find /tmp -type f": "find-fd", "grep -r x .": "grep-rg",
                   "egrep -R y src": "grep-rg", "grep -d recurse z .": "grep-rg", "find . -exec rm {} \\;": "find-fd"}
        passes = ["fd x", "rg x", "grep x file", "grep -- -r file", "echo find", "ls -la"]
        for names, fd_name in ((("fd", "rg"), "fd"), (("fdfind", "rg"), "fdfind")):
            os.environ["PATH"] = self.stub_path(*names)
            for command, rid in catches.items():
                with self.subTest(path=names, command=command):
                    text = self.denial(command)
                    assert text is not None
                    self.assertIn(f"[guardrails:{rid}]", plain(text))
                    if rid == "find-fd":
                        self.assertIn(f"`{fd_name} -h`", text)
                        self.assertNotIn("{found}", text)
                        other = "fdfind" if fd_name == "fd" else "fd "
                        self.assertNotIn(f"`{other}", text)
            for command in passes:
                with self.subTest(path=names, command=command):
                    self.assertIsNone(self.denial(command))
        os.environ["PATH"] = self.stub_path("fdfind")
        self.assertIsNotNone(self.denial("find . -name y"))
        self.assertIsNone(self.denial("grep -r y ."))

    def test_modern_cli_find_exec_gets_its_own_advice(self) -> None:
        self.cli("preset", "install", "modern-cli")
        os.environ["PATH"] = self.stub_path("fd")
        for command in ("find . -name '*.o' -delete", "find . -type f -exec chmod 644 {} \\;", "sudo find / -execdir ls {} +"):
            with self.subTest(command=command):
                text = self.denial(command)
                assert text is not None
                self.assertIn("-x cmd {}   per file", text)
                self.assertNotIn("cheat sheet", text)
        text = self.denial("find . -name x")
        assert text is not None
        self.assertIn("── fd cheat sheet", text)
        self.assertIn("find . -exec cmd {} \\;", text)


def rule_of(preset: str, rid: str) -> policy.Rule:
    return policy.Rule.from_json(cli.load_preset(preset)["rules"][rid])


class Examples(AstIsolated):
    def test_every_rule_of_the_tool_presets_has_catch_and_pass_examples_that_hold(self) -> None:
        for name in ("modern-cli",):
            preset = cli.load_preset(name)
            self.assertEqual(sorted(preset["examples"]), sorted(preset["rules"]))
            for rid, example in preset["examples"].items():
                rule = policy.Rule.from_json(preset["rules"][rid])
                self.assertGreaterEqual(len(example["catch"]), 3)
                self.assertGreaterEqual(len(example["pass"]), 3)
                for kind, expected in (("catch", True), ("pass", False)):
                    for command in example[kind]:
                        with self.subTest(preset=name, rule=rid, kind=kind, command=command):
                            self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"] is not None, expected)

    def says(self, preset: str, rid: str, command: str) -> str:
        rule = rule_of(preset, rid)
        evaluation = matching.evaluate(command, {"r": rule})
        self.assertIsNotNone(evaluation.kinds["r"], command)
        return engine.texts_of(rule, evaluation.details.get("r")).full

    def test_cargo_nextest_cases_and_text(self) -> None:
        plain_text = self.says("modern-cli", "cargo-nextest", "cargo test -p foo my_test")
        self.assertIn("cargo nextest run", plain_text)
        self.assertIn("cargo test --doc", plain_text)
        for command in ("cargo test -- --nocapture", "cargo test -p foo x -- --nocapture --test-threads=1"):
            self.assertIn("--no-capture", self.says("modern-cli", "cargo-nextest", command))
        self.assertNotIn("becomes `cargo nextest run -p foo", plain_text)

    def test_du_dust_cases(self) -> None:
        self.assertIn("dust -d 0 PATH", self.says("modern-cli", "du-dust", "du -sh /tmp"))
        self.assertIn("dust -d N PATH", self.says("modern-cli", "du-dust", "du --max-depth=1 /tmp"))
        self.assertIn("dust -d N PATH", self.says("modern-cli", "du-dust", "du -h -d 1 /tmp"))
        default = self.says("modern-cli", "du-dust", "du -h /tmp")
        self.assertIn("dust -n 20 PATH", default)
        self.assertIn("repeat the same command", default)


class Installed(AstIsolated):
    def denied(self, command: str, tool: str = "Bash", tool_input: Any = None) -> str | None:
        out = self.hook(command, session=f"s-{tool}-{command}-{tool_input}", tool=tool, tool_input=tool_input)
        return None if out is None else out["hookSpecificOutput"]["permissionDecisionReason"]

    def stub_path(self, *names: str) -> str:
        folder = self.tmp / ("bin-" + "-".join(names))
        folder.mkdir()
        for name in names:
            (folder / name).write_text("#!/bin/sh\n")
            (folder / name).chmod(0o755)
        return str(folder)

    def test_nextest_and_dust_rules_need_their_tools(self) -> None:
        self.cli("preset", "install", "modern-cli")
        os.environ["PATH"] = self.stub_path("fd")
        self.assertIsNone(self.denied("cargo test"))
        self.assertIsNone(self.denied("du -sh ."))
        os.environ["PATH"] = self.stub_path("cargo-nextest", "dust")
        self.assertIn("[guardrails:cargo-nextest]", plain(self.denied("cargo test -p x") or ""))
        self.assertIn("[guardrails:du-dust]", plain(self.denied("du -sh .") or ""))
        self.assertIsNone(self.denied("cargo test --doc"))
        self.assertIsNotNone(self.denied("du -sk ."))
        self.assertIsNone(self.denied("du -sk ."))
