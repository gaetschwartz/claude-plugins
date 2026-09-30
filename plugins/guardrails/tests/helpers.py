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

import render  # noqa: F401
import store

AST_PIN = "0.45.3"


def ast_mode() -> str | None:
    """How the AST matcher can run here: "inprocess" (ast_grep_py importable), "uv" (uv run works), or None."""
    global _AST_MODE
    if _AST_MODE is None:
        try:
            import ast_grep_py  # noqa: F401  # ty: ignore[unresolved-import]

            _AST_MODE = "inprocess"
        except ImportError:
            import astrun

            saved = os.environ.pop(astrun.INPROCESS_ENV, None)
            try:
                astrun.call({"op": "ping"})
                _AST_MODE = "uv"
            except astrun.Unavailable:
                _AST_MODE = ""
            finally:
                if saved is not None:
                    os.environ[astrun.INPROCESS_ENV] = saved
    return _AST_MODE or None


_AST_MODE: str | None = None

SKIP_AST = (f"ast-grep-py is unavailable: run the tests with `uv run --with ast-grep-py=={AST_PIN} python -m "
            "unittest discover -s tests`, or with network access once so uv can cache it")

SCRUBBED = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR",
            "GUARDRAILS_MANAGED_PATH", "GUARDRAILS_UV", "GUARDRAILS_AST_INPROCESS", "GUARDRAILS_PARITY")


class Isolated(unittest.TestCase):
    """Global state under <tmp>/data, managed override under <tmp>/managed, stand-in platform default <tmp>/sysdefault, project root <tmp>/proj, no agent markers."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.data = self.tmp / "data"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        self.mpath = self.tmp / "managed" / "guardrails.json"
        self.dpath = self.tmp / "sysdefault" / "guardrails.json"
        patch_default = mock.patch.object(store, "default_managed_path", return_value=str(self.dpath))
        patch_default.start()
        self.addCleanup(patch_default.stop)
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        env.update(CLAUDE_PLUGIN_DATA=str(self.data), CLAUDE_PROJECT_DIR=str(self.proj),
                   GUARDRAILS_MANAGED_PATH=str(self.mpath))
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

    def hook(self, command: str, session: str = "s1", tool: str = "Bash",
             tool_input: Any = None) -> dict[str, Any] | None:
        import engine

        payload = {"session_id": session, "cwd": str(self.proj), "tool_name": tool,
                   "tool_input": {"command": command} if tool_input is None else tool_input}
        out = io.StringIO()
        self.hook_err = io.StringIO()
        with contextlib.redirect_stderr(self.hook_err):
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


class AstIsolated(Isolated):
    """Isolated state plus a working AST matcher (in-process when importable, else through uv), or a skip."""

    def setUp(self) -> None:
        super().setUp()
        mode = ast_mode()
        if mode is None:
            if os.environ.get("GUARDRAILS_REQUIRE_AST") == "1":
                self.fail(SKIP_AST)
            self.skipTest(SKIP_AST)
        if mode == "inprocess":
            os.environ["GUARDRAILS_AST_INPROCESS"] = "1"
