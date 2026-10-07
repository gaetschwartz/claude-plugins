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

import bootstrap
import render  # noqa: F401
import store
import telemetry

GREP_RECURSIVE = json.loads((ROOT / "presets" / "modern-cli.json").read_text())["rules"]["grep-rg"]["match"]
MAINTAINER = json.loads((ROOT / "tests" / "maintainer_rules.json").read_text())


def real_rules() -> dict[str, Any]:
    """The maintainer's own eight rules plus every preset rule, as a state file's `rules` table."""
    rules = dict(MAINTAINER["rules"])
    for path in sorted((ROOT / "presets").glob("*.json")):
        rules.update({f"{path.stem}-{rid}": rule for rid, rule in json.loads(path.read_text())["rules"].items()})
    return rules


def real_matchers() -> dict[str, Any]:
    """The matchers every preset defines, as a config's `matchers` table."""
    return {name: fragment for path in sorted((ROOT / "presets").glob("*.json"))
            for name, fragment in json.loads(path.read_text()).get("matchers", {}).items()}


DEV_DATA = Path.home() / ".cache" / "guardrails-runtime-dev"
SKIP_RUNTIME = "the managed runtime could not be installed ({reason}); tests that run the real hook are skipped"
_RUNTIME: list[bootstrap.Outcome] = []
REAL_URLOPEN = urllib.request.urlopen
SKIP_AST = ("ast-grep-py is not importable: run the suite with `just test` (uv provides the pinned library)")


def no_network(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"a test reached the network: {args[:1]}")


def caught(out: str) -> dict[str, bool]:
    """Command shown in each rule card row, and whether the matcher selects it."""
    rows = re.finditer(r"^- (?P<glyph>[✗✓]) (?:⚠ )?`(?P<cmd>.*?)\s*` ", out, re.MULTILINE)
    return {row["cmd"]: row["glyph"] == "✗" for row in rows}


def plain(text: str) -> str:
    """Denial text without the rule hash after each id."""
    return re.sub(r"#[0-9a-f]{8}(?=[\],]| \()", "", text)


SCRUBBED = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_DATA", "CLAUDE_PROJECT_DIR", "XDG_CONFIG_HOME")


class Isolated(unittest.TestCase):
    """State under <tmp>/data, global config under <tmp>/xdg, HOME at <tmp>/home, the managed file at <tmp>/managed,
    project root <tmp>/proj, no agent markers."""

    silent_telemetry = True

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.data = self.tmp / "data"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        self.data.mkdir()
        self.mpath = self.tmp / "managed" / "guardrails.json"
        if self.silent_telemetry:
            quiet = mock.patch.object(telemetry.Recorder, "start")
            quiet.start()
            self.addCleanup(quiet.stop)
        patch_managed = mock.patch.object(store, "MANAGED_PATH", self.mpath)
        patch_managed.start()
        self.addCleanup(patch_managed.stop)
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        self.home = self.tmp / "home"
        self.home.mkdir()
        env.update(CLAUDE_PLUGIN_DATA=str(self.data), CLAUDE_PROJECT_DIR=str(self.proj),
                   XDG_CONFIG_HOME=str(self.tmp / "xdg"), HOME=str(self.home))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        guard = mock.patch.object(urllib.request, "urlopen", no_network)
        guard.start()
        self.addCleanup(guard.stop)

    @property
    def gpath(self) -> Path:
        return self.tmp / "xdg" / "dev.gaetans.guardrails" / "claude-plugin" / "config.json"

    @property
    def ppath(self) -> Path:
        return self.proj / ".claude" / "guardrails.json"

    @property
    def spath(self) -> Path:
        return self.data / "state.json"

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

    def run_guard(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["sh", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True, text=True,
                              check=False, env=dict(os.environ))


class AstIsolated(Isolated):
    """Isolated state plus the in-process ast-grep library, or a skip."""

    def setUp(self) -> None:
        super().setUp()
        try:
            import ast_grep_py  # noqa: F401
        except ImportError:
            if os.environ.get("GUARDRAILS_REQUIRE_AST") == "1":
                self.fail(SKIP_AST)
            self.skipTest(SKIP_AST)

    def assert_kinds(self, rule: Any, commands: dict[str, str | None]) -> None:
        import matching

        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"], expected)

    def break_engine(self, reason: str = "it misparsed a test command") -> None:
        """Make the library fail on every command and its self-test fail, as a broken install would."""
        import scanner

        for patch in (mock.patch.object(scanner, "self_test", lambda: reason),
                      mock.patch.object(scanner.Scanner, "run", side_effect=RuntimeError(reason))):
            patch.start()
            self.addCleanup(patch.stop)


def shared_runtime() -> bootstrap.Outcome:
    """Install the real runtime once per run into a cache dir that survives between runs."""
    if not _RUNTIME:
        with mock.patch.object(urllib.request, "urlopen", REAL_URLOPEN):
            _RUNTIME.append(bootstrap.ensure(DEV_DATA, retry_now=True))
    return _RUNTIME[0]


class RealRuntime(Isolated):
    """Isolated state plus the real managed runtime (Python, ast-grep-py) behind the real sh wrapper, or a skip."""

    def setUp(self) -> None:
        super().setUp()
        outcome = shared_runtime()
        if outcome.state not in ("ready", "installed"):
            if os.environ.get("GUARDRAILS_REQUIRE_AST") == "1":
                self.fail(SKIP_RUNTIME.format(reason=outcome.detail or outcome.state))
            self.skipTest(SKIP_RUNTIME.format(reason=outcome.detail or outcome.state))
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "runtime").symlink_to(DEV_DATA / "runtime")
