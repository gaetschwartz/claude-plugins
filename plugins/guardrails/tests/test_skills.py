from __future__ import annotations

import argparse
import re
import unittest

from helpers import ROOT

SKILLS = ROOT / "skills"
EXPECTED = {"status", "explain", "new", "edit", "mode", "setup"}
TEXT_SUFFIXES = {".md", ".json", ".py", ".sh", ".toml", ".txt"}
READ_ONLY_TOOLS = ["Bash(guardrails status *)", "Bash(guardrails rule test *)", "Bash(guardrails rule ast *)"]
CAT_REFERENCES = "Bash(cat ${CLAUDE_PLUGIN_ROOT}/references/*)"
STALE = ("engine install", "engine verify", "npm ci", "package.json", "SARIF", "GUARDRAILS ENGINE MISSING", "astbin",
         "astrun", "astcli", "astworker", "astrules", "engine-manifest", "guardrails:" + "rules", "GUARDRAILS_MANAGED_PATH",
         "--path", "guardrails wrapper", "match.builtin", '"builtin":')


def skill(name: str) -> tuple[dict[str, str], str]:
    """The frontmatter fields and body of a skill (`claude plugin validate` checks the format itself)."""
    head, _, body = (SKILLS / name / "SKILL.md").read_text()[4:].partition("\n---\n")
    return dict(line.split(":", 1) for line in head.splitlines()), body


def tools(name: str) -> list[str]:
    return re.findall(r"Bash\([^)]*\)|AskUserQuestion", skill(name)[0]["allowed-tools"])


class SkillFiles(unittest.TestCase):
    def test_the_six_skills_exist_and_are_named_after_their_directory(self) -> None:
        self.assertEqual({p.name for p in SKILLS.iterdir()}, EXPECTED)
        for name in EXPECTED:
            self.assertEqual(skill(name)[0]["name"].strip(), name)

    def test_only_status_and_explain_fork_and_each_injects_exactly_what_it_may_read(self) -> None:
        for name in EXPECTED:
            fields, body = skill(name)
            files = {"status": set(), "explain": {"matching.md", "presentation.md"}}.get(name)
            injected = set(re.findall(r"^!`cat \$\{CLAUDE_PLUGIN_ROOT\}/references/([a-z-]+\.md)`$", body, re.MULTILINE))
            with self.subTest(skill=name):
                self.assertEqual(fields.get("context", "").strip(), "fork" if files is not None else "")
                self.assertEqual(injected, files or set())
                self.assertEqual(CAT_REFERENCES in fields["allowed-tools"], bool(files))

    def test_allowed_tools_are_pinned_read_only_and_never_preapprove_changes(self) -> None:
        pinned = {"new": [*READ_ONLY_TOOLS, "Bash(guardrails preset list *)", "AskUserQuestion"],
                  "edit": [*READ_ONLY_TOOLS, "AskUserQuestion"], "explain": [*READ_ONLY_TOOLS, CAT_REFERENCES],
                  "status": ["Bash(guardrails status *)"]}
        for name, expected in pinned.items():
            with self.subTest(skill=name):
                self.assertEqual(tools(name), expected)
        fields, body = skill("explain")
        self.assertEqual(set(fields["disallowed-tools"].split()), {"Edit", "Write", "NotebookEdit"})
        self.assertIsNone(re.search(r"guardrails (rule (add|set|rm)|enable|disable|mode (on|off|declare|undeclare))|"
                                    r"preset install|sudo (guardrails|python)", body))
        for name in ("new", "edit"):
            self.assertIn("--json -", skill(name)[1])
            self.assertNotIn("Write", skill(name)[0]["allowed-tools"])

    def test_flags_used_in_the_docs_exist_in_the_cli(self) -> None:
        import cli

        known: set[str] = set()

        def walk(parser: argparse.ArgumentParser) -> None:
            for action in parser._actions:
                known.update(action.option_strings)
                if isinstance(action, argparse._SubParsersAction):
                    for sub in action.choices.values():
                        walk(sub)

        walk(cli.build_parser())
        for path in [*SKILLS.glob("*/SKILL.md"), ROOT / "references" / "presentation.md", ROOT / "README.md"]:
            for line in (ln for ln in path.read_text().splitlines() if "guardrails " in ln):
                for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", line):
                    with self.subTest(file=path.name, flag=flag):
                        self.assertIn(flag, known | {"--help", "--ignore-scripts"})

    def test_referenced_files_exist_and_every_reference_is_used(self) -> None:
        used: set[str] = set()
        for name in EXPECTED:
            fields, body = skill(name)
            text = body + fields.get("allowed-tools", "")
            self.assertNotIn("${CLAUDE_SKILL_DIR}", text)
            used.update(re.findall(r"references/([a-z-]+\.md)", body))
            for ref in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)", text):
                with self.subTest(skill=name, ref=ref):
                    self.assertTrue((ROOT / ref.rstrip(".").removesuffix("/**").rstrip("/")).exists())
        self.assertEqual(used, {p.name for p in (ROOT / "references").glob("*.md")})

    def test_no_text_names_a_removed_install_step_or_skill(self) -> None:
        known = {f"guardrails:{name}" for name in EXPECTED}
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES or "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            text = path.read_text()
            with self.subTest(file=str(path.relative_to(ROOT))):
                if "tests" not in path.parts:
                    self.assertEqual([needle for needle in STALE if needle in text], [])
                if path.suffix == ".md":
                    self.assertEqual(set(re.findall(r"guardrails:[a-z]+", text)) - known, set())


if __name__ == "__main__":
    unittest.main()
