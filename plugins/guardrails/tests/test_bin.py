from __future__ import annotations

import os
import subprocess

from helpers import ROOT, Isolated

BIN = ROOT / "bin" / "guardrails"


class BareCommand(Isolated):
    def run_bin(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(BIN), *argv], capture_output=True, text=True, check=False, env=dict(os.environ))

    def test_preset_list(self) -> None:
        result = self.run_bin("preset", "list")
        self.assertEqual(result.returncode, 0)
        self.assertIn("docs-first", result.stdout)

    def test_help(self) -> None:
        result = self.run_bin("--help")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("usage: guardrails"))
