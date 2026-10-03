from __future__ import annotations

import re
import unittest
from pathlib import Path

from helpers import ROOT

SKILLS = ROOT / "skills"
EXPECTED = {"status", "explain", "new", "edit", "mode", "setup"}
ALLOWED_FIELDS = {"name", "description", "when_to_use", "argument-hint", "arguments", "disable-model-invocation",
                  "user-invocable", "allowed-tools", "disallowed-tools", "model", "effort", "context", "agent",
                  "background", "hooks", "paths", "shell", "metadata", "license", "compatibility"}
TEXT_SUFFIXES = {".md", ".json", ".py", ".sh", ".toml", ".txt"}


def frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} does not start with frontmatter"
    head, _, body = text[4:].partition("\n---\n")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z_-]*):\s*(.*)$", line)
        assert match, f"{path}: unparseable frontmatter line {line!r}"
        assert match.group(1) not in fields, f"{path}: duplicate field {match.group(1)}"
        fields[match.group(1)] = match.group(2).strip()
    return fields, body


def skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


class SkillFiles(unittest.TestCase):
    def test_setup_ensures_the_runtime_before_anything_else_and_stops_on_failure(self) -> None:
        fields, body = frontmatter(SKILLS / "setup" / "SKILL.md")
        self.assertIn("Bash(guardrails engine ensure)", fields["allowed-tools"])
        first = body.index("## First step: the runtime")
        self.assertLess(first, body.index("## Interview"))
        step = body[first:body.index("## Current state")]
        for needle in ("guardrails engine ensure", "Report its result", "precise reason", "stop"):
            self.assertIn(needle, step)

    def test_no_text_tells_anyone_to_install_the_engine_by_hand(self) -> None:
        stale = ("engine install", "engine verify", "npm ci", "package.json", "SARIF", "GUARDRAILS ENGINE MISSING",
                 "astbin", "astrun", "astcli", "astworker", "astrules", "engine-manifest")
        for path in [ROOT / "README.md", *ROOT.glob("references/**/*.md"), *ROOT.glob("skills/*/SKILL.md"),
                     *ROOT.glob("lib/*.py"), ROOT / "hooks" / "guardrails.sh"]:
            text = path.read_text()
            for needle in stale:
                self.assertNotIn(needle, text, f"{path.name} still mentions {needle!r}")

    def test_the_six_skills_exist(self) -> None:
        self.assertEqual({p.parent.name for p in skill_files()}, EXPECTED)
        self.assertEqual({p.name for p in SKILLS.iterdir()}, EXPECTED)

    def test_frontmatter_is_valid(self) -> None:
        for path in skill_files():
            with self.subTest(skill=path.parent.name):
                fields, _ = frontmatter(path)
                self.assertEqual(fields["name"], path.parent.name)
                self.assertTrue(fields.get("description"))
                self.assertTrue(fields.get("argument-hint"), "every skill has an argument-hint")
                self.assertEqual(set(fields) - ALLOWED_FIELDS, set())

    def test_forked_skills(self) -> None:
        for name, model in (("status", "haiku"), ("explain", "sonnet")):
            with self.subTest(skill=name):
                fields, _ = frontmatter(SKILLS / name / "SKILL.md")
                self.assertEqual((fields["context"], fields["model"]), ("fork", model))
        for name in EXPECTED - {"status", "explain"}:
            with self.subTest(skill=name):
                self.assertNotIn("context", frontmatter(SKILLS / name / "SKILL.md")[0])

    def test_explain_is_read_only(self) -> None:
        fields, body = frontmatter(SKILLS / "explain" / "SKILL.md")
        patterns = re.findall(r"[A-Za-z]+\([^)]*\)", fields["allowed-tools"])
        self.assertEqual(patterns, ["Bash(guardrails status *)", "Bash(guardrails rule test *)",
                                    "Bash(guardrails rule ast *)", "Bash(cat ${CLAUDE_PLUGIN_ROOT}/references/*)"])
        self.assertEqual(set(fields["disallowed-tools"].split()), {"Edit", "Write", "NotebookEdit"})
        for forbidden in (r"guardrails rule (add|set|rm)", r"guardrails (enable|disable)", r"preset install",
                          r"guardrails mode (on|off|declare|undeclare)", r"sudo (guardrails|python)"):
            self.assertIsNone(re.search(forbidden, body), forbidden)

    def test_no_read_permission_patterns(self) -> None:
        for path in skill_files():
            with self.subTest(skill=path.parent.name):
                self.assertNotIn("Read(", frontmatter(path)[0].get("allowed-tools", ""))

    def test_forked_skills_inject_their_references(self) -> None:
        wanted = {"status": set(), "explain": {"matching.md", "presentation.md"}}
        for name, files in wanted.items():
            fields, body = frontmatter(SKILLS / name / "SKILL.md")
            injected = set(re.findall(r"^!`cat \$\{CLAUDE_PLUGIN_ROOT\}/references/([a-z-]+\.md)`$", body, re.MULTILINE))
            with self.subTest(skill=name):
                self.assertEqual(injected, files)
                self.assertEqual("Bash(cat ${CLAUDE_PLUGIN_ROOT}/references/*)" in fields["allowed-tools"], bool(files))

    def test_rules_are_passed_on_stdin_without_the_write_tool(self) -> None:
        for name in ("new", "edit"):
            with self.subTest(skill=name):
                fields, body = frontmatter(SKILLS / name / "SKILL.md")
                self.assertIn("--json -", body)
                self.assertIn("<<'EOF'", body)
                self.assertNotIn("'\\''", body)
                self.assertNotIn("Write tool", body)
                self.assertNotIn("Write", fields["allowed-tools"])

    def test_allowed_tools_are_pinned_and_never_preapprove_changes(self) -> None:
        pinned = {"new": ["Bash(guardrails status *)", "Bash(guardrails rule test *)", "Bash(guardrails rule ast *)",
                          "Bash(guardrails preset list *)", "AskUserQuestion"],
                  "edit": ["Bash(guardrails status *)", "Bash(guardrails rule test *)", "Bash(guardrails rule ast *)",
                           "AskUserQuestion"],
                  "explain": ["Bash(guardrails status *)", "Bash(guardrails rule test *)",
                              "Bash(guardrails rule ast *)", "Bash(cat ${CLAUDE_PLUGIN_ROOT}/references/*)"],
                  "status": ["Bash(guardrails status *)"]}
        for name, tools in pinned.items():
            with self.subTest(skill=name):
                declared = frontmatter(SKILLS / name / "SKILL.md")[0]["allowed-tools"]
                self.assertEqual(re.findall(r"Bash\([^)]*\)|AskUserQuestion", declared), tools)

    def test_display_skills_render_with_the_cli_and_paste_verbatim(self) -> None:
        for name in ("new", "edit", "explain", "status"):
            with self.subTest(skill=name):
                text = (SKILLS / name / "SKILL.md").read_text()
                self.assertIn("VERBATIM", text)
                self.assertIsNone(re.search(r"\bpad(ded|ding)?\b", text, re.IGNORECASE))
        self.assertIn("rule test", (SKILLS / "new" / "SKILL.md").read_text())
        self.assertIn("status", (SKILLS / "status" / "SKILL.md").read_text())
        for name in ("new", "edit", "explain"):
            self.assertIn("never write a script", (SKILLS / name / "SKILL.md").read_text().lower())

    def test_new_builds_examples_with_sources_and_expectations(self) -> None:
        text = (SKILLS / "new" / "SKILL.md").read_text()
        for needle in ("\"examples\"", '"expect"', "you chose", "--id-name"):
            self.assertIn(needle, text)
        self.assertIn("status --rule", text)

    def test_skills_teach_the_matcher_ladder_and_the_tree_command(self) -> None:
        for name in ("new", "edit"):
            text = (SKILLS / name / "SKILL.md").read_text()
            with self.subTest(skill=name):
                self.assertIn("guardrails rule ast", text)
                self.assertRegex(text, r"(?s)`program`.{0,80}`program` \+ `args`.{0,80}`builtin`.{0,80}`ast`.{0,200}`regex`")

    def test_presentation_reference_states_the_paste_rule(self) -> None:
        text = (ROOT / "references" / "presentation.md").read_text()
        self.assertIn("VERBATIM", text)
        self.assertIn("Never write a script", text)

    def test_flags_used_in_skills_exist_in_the_cli(self) -> None:
        import argparse

        import cli

        known: set[str] = set()

        def walk(parser: argparse.ArgumentParser) -> None:
            for action in parser._actions:
                known.update(action.option_strings)
                if isinstance(action, argparse._SubParsersAction):
                    for sub in action.choices.values():
                        walk(sub)

        walk(cli.build_parser())
        for path in [*skill_files(), ROOT / "references" / "presentation.md", ROOT / "README.md"]:
            for line in path.read_text().splitlines():
                if "guardrails " not in line and "guardrails\n" not in line:
                    continue
                for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", line):
                    with self.subTest(file=path.name, flag=flag):
                        self.assertIn(flag, known | {"--help", "--ignore-scripts"})

    def test_referenced_files_exist(self) -> None:
        pattern = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)")
        for path in skill_files():
            fields, body = frontmatter(path)
            text = body + fields.get("allowed-tools", "")
            self.assertNotIn("${CLAUDE_SKILL_DIR}", text)
            for ref in pattern.findall(text):
                ref = ref.rstrip(".")
                ref = ref.removesuffix("/**")
                with self.subTest(skill=path.parent.name, ref=ref):
                    self.assertTrue((ROOT / ref.rstrip("/")).exists())

    def test_rule_writing_skills_point_to_the_guide_and_cookbook(self) -> None:
        for name in ("new", "edit", "explain"):
            body = frontmatter(SKILLS / name / "SKILL.md")[1]
            with self.subTest(skill=name):
                self.assertIn("${CLAUDE_PLUGIN_ROOT}/references/writing-rules.md", body)
                self.assertIn("${CLAUDE_PLUGIN_ROOT}/references/ast/index.md", body)

    def test_pitfalls_in_new_match_the_guide(self) -> None:
        new = frontmatter(SKILLS / "new" / "SKILL.md")[1]
        short = re.findall(r"^- `([a-z-]+)`: ", new.partition("## How to write the rule")[2].partition("\n## ")[0],
                           re.MULTILINE)
        guide = (ROOT / "references" / "writing-rules.md").read_text()
        full = re.findall(r"^### ([a-z-]+)$", guide.partition("## The six pitfalls")[2].partition("\n## ")[0],
                          re.MULTILINE)
        self.assertEqual(len(short), 6)
        self.assertEqual(short, full)

    def test_references_are_used_and_not_duplicated(self) -> None:
        used = set()
        for path in skill_files():
            used.update(re.findall(r"references/([a-z-]+\.md)", frontmatter(path)[1]))
        self.assertEqual(used, {p.name for p in (ROOT / "references").glob("*.md")})

    def test_no_stale_skill_references(self) -> None:
        stale = "guardrails:" + "rules"
        known = {f"guardrails:{name}" for name in EXPECTED}
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES or "__pycache__" in path.parts:
                continue
            text = path.read_text()
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertNotIn(stale, text)
                if path.suffix == ".md":
                    self.assertEqual(set(re.findall(r"guardrails:[a-z]+", text)) - known, set())


if __name__ == "__main__":
    unittest.main()
