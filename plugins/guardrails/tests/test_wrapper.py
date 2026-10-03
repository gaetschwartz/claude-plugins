from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import installer
import telemetry
from helpers import DEV_DATA, HOOKS, LIB, ROOT, Isolated, RealRuntime

SCRIPT = (HOOKS / "guardrails.sh").read_text()
RUNTIME_ID = (LIB / "runtime-id").read_text().strip()
SHELLS = [[path] for path in ("/bin/sh", "/bin/dash") if os.path.exists(path)]
if shutil.which("busybox"):
    SHELLS.append([str(shutil.which("busybox")), "sh"])
NOW = time.time


class Source(Isolated):
    def test_the_wrapper_is_posix_sh_that_never_changes_path(self) -> None:
        self.assertTrue(SCRIPT.startswith("#!/bin/sh\n"))
        self.assertNotIn("PATH=", SCRIPT.replace("for dir in $PATH", ""))
        self.assertNotIn("export", SCRIPT)
        for forbidden in ("dirname", "command -v", "bash", "[["):
            self.assertNotIn(forbidden, SCRIPT)

    def test_the_cli_modules_import_and_run_without_the_ast_grep_library(self) -> None:
        code = ("import sys\nsys.modules['ast_grep_py'] = None\nsys.path.insert(0, sys.argv[1])\n"
                "import guard, cli, engine, matching, rulebuilder\nraise SystemExit(guard.main(['preset', 'list']))")
        result = subprocess.run([sys.executable, "-c", code, str(LIB)], capture_output=True, text=True, check=False,
                                env=dict(os.environ))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn("docs-first", result.stdout)

    def test_it_parses_under_every_available_shell(self) -> None:
        for shell in SHELLS:
            with self.subTest(shell=shell):
                proc = subprocess.run([*shell, "-n", str(HOOKS / "guardrails.sh")], capture_output=True, text=True,
                                      check=False)
                self.assertEqual((proc.returncode, proc.stderr), (0, ""))

Base = Isolated if TYPE_CHECKING else object


class StubbedCases(Base):
    """The script with every fixed interpreter location replaced, run against stub interpreters, under SHELL."""

    SHELL: ClassVar[list[str]]

    def setUp(self) -> None:
        super().setUp()
        self.lib = self.tmp / "plugin" / "lib"
        (self.tmp / "plugin" / "hooks").mkdir(parents=True)
        self.lib.mkdir()
        (self.lib / "runtime-id").write_text(RUNTIME_ID + "\n")
        replaced = (SCRIPT.replace("/opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3",
                                   "/nonexistent/a /nonexistent/b /nonexistent/c").replace("/usr/bin/uname -s", "echo Darwin"))
        self.script = self.tmp / "plugin" / "hooks" / "guardrails.sh"
        self.script.write_text(replaced)
        self.data.mkdir(exist_ok=True)
        self.rt = self.data / "runtime" / RUNTIME_ID

    def stub(self, path: Path, label: str, mode: int = 0o755, body: str = "") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\n{body}echo "{label} $*"\n')
        path.chmod(mode)
        return path

    def ready_runtime(self, data: Path | None = None, body: str = "") -> Path:
        rt = (data or self.data) / "runtime" / RUNTIME_ID
        self.stub(rt / "venv" / "bin" / "python", "READY", body=body)
        (rt / "marker.json").write_text("{}")
        return rt

    def run_script(self, path: str, *args: str, data: str | None = "default", cwd: Path | None = None,
                   extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {"PATH": path, "HOME": str(self.tmp), "CLAUDE_PROJECT_DIR": str(self.proj), **(extra or {})}
        if data == "default":
            env["CLAUDE_PLUGIN_DATA"] = str(self.data)
        elif data is not None:
            env["CLAUDE_PLUGIN_DATA"] = data
        return subprocess.run([*self.SHELL, str(self.script), *args], input="{}", capture_output=True, text=True,
                              check=False, env=env, cwd=cwd or self.proj)

    def host_stub(self) -> str:
        self.stub(self.tmp / "a" / "python3", "HOST")
        return f"{self.tmp}/a"

    def test_a_ready_runtime_runs_the_guard_on_its_own_python_and_nothing_else(self) -> None:
        self.ready_runtime()
        out = self.run_script(self.host_stub()).stdout
        self.assertEqual(out, f"READY -I {self.script.parent}/../lib/guard.py\n")
        cli = self.run_script(self.host_stub(), "cli", "status", "--problems").stdout
        self.assertEqual(cli, f"READY -I {self.script.parent}/../lib/guard.py status --problems\n")

    def test_a_runtime_that_is_not_ready_goes_to_the_bootstrap_on_the_host_python(self) -> None:
        boot = f"HOST -I -S {self.script.parent}/../lib/bootstrap.py hook\n"
        cases = {"no marker": lambda: self.stub(self.rt / "venv" / "bin" / "python", "READY"),
                 "no python": lambda: (self.rt.mkdir(parents=True), (self.rt / "marker.json").write_text("{}")),
                 "marked broken": lambda: (self.ready_runtime(), (self.rt / "broken").touch())}
        for name, setup in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.data / "runtime", ignore_errors=True)
                setup()
                self.assertEqual(self.run_script(self.host_stub()).stdout, boot)
        self.assertTrue(self.run_script(self.host_stub(), data=None).stdout.startswith("HOST -I -S "), "no data dir variable")

    def test_a_relative_data_dir_never_runs_anything_from_it(self) -> None:
        boot = f"HOST -I -S {self.script.parent}/../lib/bootstrap.py hook\n"
        repo = self.proj / ".rt"
        self.ready_runtime(repo)
        for name, value in {"relative": ".rt", "relative from the project root": "./.rt"}.items():
            with self.subTest(name):
                out = self.run_script(self.host_stub(), data=value).stdout
                self.assertEqual(out, boot)
                self.assertNotIn("READY", out)
        self.assertTrue(self.run_script(self.host_stub(), data=str(repo)).stdout.startswith("READY"), "the same dir, absolute")

    def test_a_guard_that_refuses_the_data_dir_falls_through_to_the_bootstrap_in_every_mode(self) -> None:
        self.ready_runtime(body="exit 111\n")
        boot = f"HOST -I -S {self.script.parent}/../lib/bootstrap.py"
        host = self.host_stub()
        self.assertEqual(self.run_script(host).stdout, f"{boot} hook\n")
        self.assertEqual(self.run_script(host, "cli", "status").stdout, f"{boot} run status\n")

    def test_the_cli_passes_the_guards_exit_code_through(self) -> None:
        self.ready_runtime(body="exit 3\n")
        self.assertEqual(self.run_script(self.host_stub(), "cli", "status").returncode, 3)

    def test_the_first_trusted_python3_on_path_is_used_and_each_mode_passes_its_arguments(self) -> None:
        self.stub(self.tmp / "a" / "python3", "a")
        self.stub(self.tmp / "b" / "python3", "b")
        path = f"{self.tmp}/none:{self.tmp}/a:{self.tmp}/b"
        boot = f"{self.script.parent}/../lib/bootstrap.py"
        self.assertEqual(self.run_script(path, "session-start").stdout, f"a -I -S {boot} session-start\n")
        self.assertEqual(self.run_script(path, "cli", "status", "--problems", data=None).stdout,
                         f"a -I -S {boot} run status --problems\n")

    def test_path_entries_inside_the_project_relative_or_world_writable_are_skipped(self) -> None:
        self.stub(self.proj / "bin" / "python3", "evil")
        self.stub(self.proj / "python3", "evil-cwd")
        self.stub(self.tmp / "loose" / "python3", "loose", 0o757)
        self.stub(self.tmp / "ok" / "python3", "ok")
        for path in (f"{self.proj}/bin:{self.tmp}/loose:{self.tmp}/ok", f".:bin:{self.tmp}/ok", f":{self.tmp}/ok"):
            with self.subTest(path=path):
                out = self.run_script(path, "session-start").stdout
                self.assertTrue(out.startswith("ok -I -S "), out)

    def test_a_python_that_cannot_run_is_named_not_ignored(self) -> None:
        cases = {"exits 1": "exit 1\n", "too old": "exit 1\n"}
        for name, body in cases.items():
            broken = self.tmp / name.replace(" ", "-") / "py thon" / "python3"
            broken.parent.mkdir(parents=True)
            broken.write_text("#!/bin/sh\n" + body)
            broken.chmod(0o755)
            for mode in ("hook", "session-start", "cli"):
                with self.subTest(name=name, mode=mode):
                    proc = self.run_script(str(broken.parent), *([] if mode == "hook" else [mode]), data=None)
                    text = proc.stdout + proc.stderr
                    self.assertIn("is broken", text)
                    self.assertIn("python3", text)
                    self.assertIn("NOT enforced", text)
                    self.assertNotIn(str(broken.parent), proc.stdout)
                    self.assertEqual(proc.returncode, 2 if mode == "cli" else 0)
                    if mode == "hook":
                        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"],
                                         json.loads(proc.stdout)["systemMessage"])
        self.stub(self.tmp / "ok" / "python3", "ok")
        both = f"{self.tmp}/exits-1/py thon:{self.tmp}/ok"
        self.assertTrue(self.run_script(both, "session-start").stdout.startswith("ok -I -S "), "a working python after a broken one")

    def test_a_hook_python_that_dies_is_never_silent(self) -> None:
        self.ready_runtime(body="exit 7\n")
        proc = self.run_script(self.host_stub())
        self.assertEqual(proc.returncode, 0)
        self.assertIn("failed to run (exit 7)", json.loads(proc.stdout)["systemMessage"])

    def test_without_any_python_every_mode_says_so_and_never_fails_silently(self) -> None:
        empty = str(self.tmp / "empty")
        hook = self.run_script(empty)
        self.assertEqual(hook.returncode, 0)
        output = json.loads(hook.stdout)
        text = output["systemMessage"]
        self.assertIn("No usable python3", text)
        self.assertIn("NOT enforced", text)
        self.assertEqual(output["hookSpecificOutput"]["additionalContext"], text)
        self.assertIn("No usable python3", json.loads(self.run_script(empty, "session-start").stdout)["systemMessage"])
        cli = self.run_script(empty, "cli", "status")
        self.assertEqual(cli.returncode, 2)
        self.assertIn("No usable python3", cli.stderr)


def make_shell_class(shell: list[str]) -> type:
    name = "Stubbed" + re.sub(r"\W", "", "".join(shell)).title()
    return type(name, (StubbedCases, Isolated), {"SHELL": shell})


for _shell in SHELLS:
    _cls = make_shell_class(_shell)
    globals()[_cls.__name__] = _cls
del _cls


class RealWrapperNotReady(Isolated):
    """The real wrapper and bootstrap on the host's python, with the install held off by a fresh failure stamp."""

    def setUp(self) -> None:
        super().setUp()
        (self.data / "runtime").mkdir(parents=True)
        (self.data / "runtime" / "install.log").write_text("1 " + installer.CONNECT + "\nraw detail\n")

    def run_script(self, *args: str, session: str = "s1") -> subprocess.CompletedProcess[str]:
        payload = json.dumps({"session_id": session, "tool_name": "Bash", "tool_input": {"command": "ls"}})
        return subprocess.run(["sh", str(HOOKS / "guardrails.sh"), *args], input=payload, capture_output=True,
                              text=True, check=False, env=dict(os.environ), cwd=self.proj)

    def test_the_hook_allows_loudly_once_per_session_and_never_silently_fails(self) -> None:
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        output = json.loads(first.stdout)
        self.assertIn("connection to a download host failed", output["systemMessage"])
        self.assertIn("NOT enforced", output["systemMessage"])
        self.assertNotIn("permissionDecision", output["hookSpecificOutput"])
        self.assertEqual(self.run_script().stdout, "")
        self.assertIn("connection to a download host failed", self.run_script(session="s2").stdout)

    def test_session_start_reports_the_failure_and_exits_zero(self) -> None:
        proc = self.run_script("session-start")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("connection to a download host failed", json.loads(proc.stdout)["systemMessage"])

    def test_the_cli_stops_with_one_line_but_engine_status_always_works(self) -> None:
        for argv in (("status",), ("rule", "list")):
            with self.subTest(argv=argv):
                proc = subprocess.run(["sh", str(ROOT / "bin" / "guardrails"), *argv], capture_output=True, text=True,
                                      check=False, env=dict(os.environ), cwd=self.proj)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
                self.assertIn("connection to a download host failed", proc.stderr)
                self.assertEqual(proc.stdout, "")
        proc = subprocess.run(["sh", str(ROOT / "bin" / "guardrails"), "engine", "status"], capture_output=True, text=True,
                              check=False, env=dict(os.environ), cwd=self.proj)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("runtime: NOT ready (backoff)", proc.stdout)
        self.assertIn("next automatic attempt at", proc.stdout)


class RealWrapperReady(RealRuntime):
    def run_script(self, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None
                   ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["sh", str(HOOKS / "guardrails.sh"), *args], input="{}", capture_output=True, text=True,
                              check=False, env=env or dict(os.environ), cwd=cwd or self.proj)

    def test_session_start_is_quiet_and_the_cli_runs_on_the_managed_python_through_the_wrapper(self) -> None:
        proc = self.run_script("session-start")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        for argv, needles in ((("engine", "status"), ("runtime: ready", "pins: uv ", "ast-grep-py 0.45.3", "platform: ",
                                                        "installed: Python 3.13.")), (("preset", "list"), ("process-safety",))):
            proc = subprocess.run(["sh", str(ROOT / "bin" / "guardrails"), *argv], capture_output=True, text=True,
                                  check=False, env=dict(os.environ), cwd=self.proj)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for needle in needles:
                self.assertIn(needle, proc.stdout)

    def deny_rule(self) -> str:
        self.put(self.gpath, {"rules": {"no-pkill": {"match": {"program": "pkill"}, "message": "No pkill.",
                                                      "action": "deny"}}})
        return json.dumps({"session_id": "b1", "cwd": str(self.proj), "tool_name": "Bash",
                           "tool_input": {"command": "sudo pkill x"}})

    def enforced(self, payload: str, env: dict[str, str], cwd: Path) -> bool:
        proc = subprocess.run(["sh", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True, text=True,
                              check=False, env=env, cwd=cwd)
        return proc.returncode == 0 and '"permissionDecision": "deny"' in proc.stdout

    def test_a_data_dir_that_is_not_safe_gets_the_notice_and_the_guard_never_reads_or_writes_in_it(self) -> None:
        payload = self.deny_rule()
        shutil.copy(self.gpath, self.tmp / "state.json")
        loose, grouped, alias = self.tmp / "loose", self.tmp / "grouped", self.tmp / "alias"
        for path, mode in ((loose, 0o777), (grouped, 0o775)):
            path.mkdir()
            (path / "runtime").symlink_to(DEV_DATA / "runtime")
            shutil.copy(self.tmp / "state.json", path / "state.json")
            path.chmod(mode)
        alias.symlink_to(self.data)
        cases = {"world-writable": str(loose), "group-writable": str(grouped), "aliased through a symlink": str(alias),
                 "dot-dot": f"{self.data}/../data", "relative": "data"}
        for name, value in cases.items():
            with self.subTest(name):
                env = {**os.environ, "CLAUDE_PLUGIN_DATA": value}
                proc = subprocess.run(["sh", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True,
                                      text=True, check=False, env=env, cwd=self.tmp)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                output = json.loads(proc.stdout)
                self.assertIn("the plugin data directory is not a safe absolute path", output["systemMessage"])
                self.assertNotIn("permissionDecision", output["hookSpecificOutput"])
        for path in (loose, grouped):
            self.assertEqual(sorted(p.name for p in path.iterdir()), ["runtime", "state.json"])
        self.assertTrue(self.enforced(payload, {**os.environ, "CLAUDE_PLUGIN_DATA": str(self.data)}, self.tmp))

    def test_the_hook_process_has_written_its_telemetry_when_it_exits(self) -> None:
        payload = self.deny_rule()
        for _ in range(3):
            self.assertTrue(self.enforced(payload, dict(os.environ), self.proj))
        rows = {row.rule: row for row in telemetry.read(self.data / telemetry.DB, 0)}
        self.assertGreaterEqual(rows["no-pkill"].deny, 1)
        self.assertGreaterEqual(rows["@hook"].passed, 1)

    def test_a_hostile_python_environment_injects_nothing(self) -> None:
        evil = self.tmp / "evil"
        evil.mkdir()
        pwned = self.tmp / "pwned"
        for name in ("sitecustomize", "usercustomize", "startup"):
            (evil / f"{name}.py").write_text(f"open({str(pwned)!r}, 'a').write({name!r})\n")
        env = {**os.environ, "PYTHONPATH": str(evil), "PYTHONHOME": str(evil), "PYTHONSTARTUP": str(evil / "startup.py"),
               "PYTHONUSERBASE": str(evil), "PYTHONINSPECT": "1", "PYTHONWARNINGS": "error", "PYTHONOPTIMIZE": "2"}
        self.assertTrue(self.enforced(self.deny_rule(), env, self.proj))
        self.assertFalse(pwned.exists())

    def test_the_hook_enforces_wherever_the_project_and_cwd_are(self) -> None:
        payload = self.deny_rule()
        places = {"data dir's parent": self.data.parent, "data dir": self.data, "root": Path("/"),
                  "project": self.proj}
        for name, project in places.items():
            for cwd in (self.data.parent, self.data, Path("/"), self.proj):
                with self.subTest(project=name, cwd=str(cwd)):
                    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
                    self.assertTrue(self.enforced(payload, env, cwd))

    def test_the_hook_enforces_for_a_session_started_in_home_or_dot_claude(self) -> None:
        home = self.tmp / "home"
        data = home / ".claude" / "plugins" / "data" / "guardrails-x"
        data.mkdir(parents=True)
        (data / "runtime").symlink_to(DEV_DATA / "runtime")
        (data / "state.json").write_text(json.dumps({"rules": {"no-pkill": {
            "match": {"program": "pkill"}, "message": "No pkill.", "action": "deny"}}}))
        payload = json.dumps({"session_id": "home", "cwd": str(home), "tool_name": "Bash",
                              "tool_input": {"command": "sudo pkill x"}})
        for project in (home, home / ".claude", home / ".claude" / "plugins", Path("/")):
            for cwd in (home, home / ".claude", home / ".claude" / "plugins"):
                with self.subTest(project=str(project), cwd=str(cwd)):
                    env = {**os.environ, "HOME": str(home), "CLAUDE_PLUGIN_DATA": str(data),
                           "CLAUDE_PROJECT_DIR": str(project)}
                    self.assertTrue(self.enforced(payload, env, cwd))


if __name__ == "__main__":
    unittest.main()
