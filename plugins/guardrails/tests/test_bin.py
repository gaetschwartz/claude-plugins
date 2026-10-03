from __future__ import annotations

import os
import subprocess
import sys

from helpers import ROOT, AstIsolated

BIN = ROOT / "bin" / "guardrails"


class BareCommand(AstIsolated):
    def run_bin(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(BIN), *argv], capture_output=True, text=True, check=False, env=dict(os.environ))

    def test_the_cli_modules_import_and_run_without_the_ast_grep_library(self) -> None:
        code = ("import sys\nsys.modules['ast_grep_py'] = None\nsys.path.insert(0, sys.argv[1])\n"
                "import guard, cli, engine, matching, rulebuilder\nraise SystemExit(guard.main(['preset', 'list']))")
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "lib")], capture_output=True, text=True,
                                check=False, env=dict(os.environ))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("docs-first", result.stdout)
