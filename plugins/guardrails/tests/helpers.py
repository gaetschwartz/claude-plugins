"""Shared fixtures: import the hook modules and isolate all state in a temp directory."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))

import astbin
import render  # noqa: F401
import store

DEV_DATA = Path.home() / ".cache" / "guardrails-engine-dev"
SKIP_AST = ("ast-grep is not installed: run `just engine` (or allow network access so the tests can install the "
            "pinned wheel)")
_SHARED: list[str] = []
REAL_WANTED = astbin.wanted
REAL_URLOPEN = urllib.request.urlopen


def no_network(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"a test reached the network: {args[:1]}")


def shared_engine() -> str | None:
    """The engine dir (`<data>/engine`) of a real pinned binary, from `just engine` or installed once per run."""
    if not _SHARED:
        _SHARED.append("")
        try:
            plat = astbin.detect()
        except astbin.Missing:
            return None
        folder = tempfile.mkdtemp(prefix="guardrails-shared-engine-")
        for data in (str(DEV_DATA), folder):
            if astbin.wheel_probe(plat, data).path:
                _SHARED[0] = astbin.engine_root(data)
                break
            if data == folder:
                with contextlib.suppress(astbin.InstallError, OSError), \
                        mock.patch.object(urllib.request, "urlopen", REAL_URLOPEN):
                    astbin.install(data)
                    if astbin.wheel_probe(plat, data).path:
                        _SHARED[0] = astbin.engine_root(data)
    return _SHARED[0] or None


def caught(out: str) -> dict[str, bool]:
    """Command shown in each rule card row, and whether the matcher selects it."""
    rows = re.finditer(r"^- (?P<glyph>[✗✓]) (?:⚠ )?`(?P<cmd>.*?)\s*` ", out, re.MULTILINE)
    return {row["cmd"]: row["glyph"] == "✗" for row in rows}


SCRUBBED = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR",
            "GUARDRAILS_MANAGED_PATH")


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
        self.plugin_root = self.tmp / "plugin-root"
        self.plugin_root.mkdir()
        patch_root = mock.patch.object(astbin, "PLUGIN_ROOT", str(self.plugin_root))
        patch_root.start()
        self.addCleanup(patch_root.stop)
        astbin.take_rejected()
        for guard in (mock.patch.object(astbin, "wanted", lambda state_dir: False),
                      mock.patch.object(urllib.request, "urlopen", no_network)):
            guard.start()
            self.addCleanup(guard.stop)

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

    def set_rule(self, rid: str, fields: Any, *extra: str, agent: bool = False) -> tuple[int, str, str]:
        return self.cli("rule", "set", rid, "--json", json.dumps(fields), *extra, agent=agent)

    def stub_engine(self, body: str) -> None:
        """Replace the engine with a shell script that has this body."""
        path = self.tmp / "stub" / "ast-grep"
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        found = astbin.Engine(str(path), "wheel", astbin.pin())
        patch = mock.patch.object(astbin, "locate", lambda state_dir: found)
        patch.start()
        self.addCleanup(patch.stop)

    def link_engine(self) -> None:
        """Make the real binary findable by a subprocess, which cannot see this process's patches."""
        engine_dir = getattr(self, "engine_dir", None)
        if engine_dir and not (self.data / "engine").exists():
            self.data.mkdir(parents=True, exist_ok=True)
            os.symlink(engine_dir, self.data / "engine")

    def run_guard(self, payload: str) -> subprocess.CompletedProcess[str]:
        self.link_engine()
        return subprocess.run(["bash", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True,
                              text=True, check=False, env=dict(os.environ))


class AstIsolated(Isolated):
    """Isolated state plus the real pinned ast-grep binary behind the wheel path, or a skip."""

    def setUp(self) -> None:
        super().setUp()
        root = shared_engine()
        if root is None:
            if os.environ.get("GUARDRAILS_REQUIRE_AST") == "1":
                self.fail(SKIP_AST)
            self.skipTest(SKIP_AST)
        self.engine_dir = root
        self.use_engine(True)

    def use_engine(self, present: bool) -> None:
        """Point the wheel lookup at the real binary, or at an empty directory (engine missing)."""
        empty = str(self.tmp / "no-engine")
        patch = mock.patch.object(astbin, "engine_root", lambda state_dir: self.engine_dir if present else empty)
        patch.start()
        self.addCleanup(patch.stop)

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        import astrun

        return astrun.call(request, str(self.data))
