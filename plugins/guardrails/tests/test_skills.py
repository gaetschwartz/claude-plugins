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
        tools = frontmatter(SKILLS / "explain" / "SKILL.md")[0]["allowed-tools"]
        self.assertIn("Bash(guardrails status *)", tools)
        self.assertIn("Bash(guardrails rule test *)", tools)
        for verb in ("add", "set", "rm", "mode", "preset", "enable", "disable"):
            self.assertNotIn(f"guardrails {verb}", tools)

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
