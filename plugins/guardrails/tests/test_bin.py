from __future__ import annotations

import json
import os
import stat
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

    def test_managed_scope_authoring_then_hook_denies(self) -> None:
        rule = '{"match": {"program": "pkill"}, "message": "No pkill."}'
        result = self.run_bin("rule", "add", "no-pkill", "--json", rule, "--scope", "managed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("always enforced", result.stdout)
        self.assertEqual(stat.S_IMODE(self.mpath.stat().st_mode), 0o644)
        self.put(self.ppath, {"rules": {"no-pkill": {"enabled": False}}})
        payload = json.dumps({"session_id": "b", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "pkill node"}})
        proc = self.run_guard(payload)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        status = self.run_bin("status")
        self.assertIn("`no-pkill` deny · managed+project · always enforced", status.stdout)

    def test_managed_scope_unwritable_exits_2_with_sudo_hint(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        self.mpath.parent.mkdir()
        self.mpath.parent.chmod(0o555)
        self.addCleanup(self.mpath.parent.chmod, 0o755)
        result = self.run_bin("mode", "declare", "incident", "--scope", "managed")
        self.assertEqual(result.returncode, 2)
        self.assertIn("sudo", result.stderr)
