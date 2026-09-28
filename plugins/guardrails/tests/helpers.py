"""Shared fixtures: import the hook modules and isolate all state in a temp directory."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))

SCRUBBED = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR")


class Isolated(unittest.TestCase):
    """Global state under <tmp>/data, project root <tmp>/proj, and no agent markers in the environment."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.data = self.tmp / "data"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        env.update(CLAUDE_PLUGIN_DATA=str(self.data), CLAUDE_PROJECT_DIR=str(self.proj))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def gpath(self) -> Path:
        return self.data / "state.json"

    @property
    def ppath(self) -> Path:
        return self.proj / ".claude" / "plugins" / "data" / "guardrails-gaetans-claude-plugins" / "state.json"

    def put(self, path: Path, state: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state if isinstance(state, str) else json.dumps(state))

    def get(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text())

    def hook(self, command: str, session: str = "s1") -> dict[str, Any] | None:
        import engine

        payload = {"session_id": session, "cwd": str(self.proj), "tool_name": "Bash",
                   "tool_input": {"command": command}}
        out = io.StringIO()
        engine.run_hook(io.StringIO(json.dumps(payload)), out)
        return json.loads(out.getvalue()) if out.getvalue() else None

    def cli(self, *argv: str, agent: bool = False) -> tuple[int, str, str]:
        import cli as cli_module

        if agent:
            os.environ["CLAUDECODE"] = "1"
        else:
            os.environ.pop("CLAUDECODE", None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_module.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_guard(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True,
                              text=True, check=False, env=dict(os.environ))
