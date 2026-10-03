from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from helpers import HOOKS, LIB, ROOT, Isolated, RealRuntime

SCRIPT = (HOOKS / "guardrails.sh").read_text()
RUNTIME_ID = (LIB / "runtime-id").read_text().strip()


class Source(Isolated):
    def test_the_wrapper_is_small_posix_sh_that_never_changes_path(self) -> None:
        self.assertTrue(SCRIPT.startswith("#!/bin/sh\n"))
        self.assertNotIn("PATH=", SCRIPT.replace("for dir in $PATH", ""))
        self.assertNotIn("export", SCRIPT)
        for forbidden in ("dirname", "$(cd", "command -v", "bash"):
            self.assertNotIn(forbidden, SCRIPT)
        self.assertLess(len(SCRIPT.splitlines()), 70)

    def test_linux_brew_is_checked_only_after_uname(self) -> None:
        lines = [ln for ln in SCRIPT.splitlines() if "linuxbrew" in ln]
        self.assertEqual(len(lines), 2)
        self.assertLess(lines[0].index("uname -s"), lines[0].index("linuxbrew"))
        for line in SCRIPT.splitlines():
            if re.search(r"(?<![\w/])/home/", line):
                self.assertIn("linuxbrew", line)

    def test_selection_order(self) -> None:
        marks = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3", "uname -s",
                 "for dir in $PATH")
        body = SCRIPT.split("find_python() {", 1)[1]
        order = [body.index(x) for x in marks]
        self.assertEqual(order, sorted(order))

    def test_the_cli_entry_goes_through_the_same_wrapper(self) -> None:
        text = (ROOT / "bin" / "guardrails").read_text()
        self.assertIn("hooks/guardrails.sh", text)
        self.assertIn(" cli ", text)
        self.assertTrue(os.access(ROOT / "bin" / "guardrails", os.X_OK))


class Stubbed(Isolated):
    """The script with every fixed interpreter location replaced, run against stub interpreters."""

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
        self.rt = self.data / "runtime" / RUNTIME_ID

    def stub(self, path: Path, label: str, mode: int = 0o755) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho "{label} $*"\n')
        path.chmod(mode)
        return path

    def ready_runtime(self) -> None:
        self.stub(self.rt / "venv" / "bin" / "python", "READY")
        (self.rt / "marker.json").write_text("{}")

    def run_script(self, path: str, *args: str, data: bool = True) -> subprocess.CompletedProcess[str]:
        env = {"PATH": path, "HOME": str(self.tmp), "CLAUDE_PROJECT_DIR": str(self.proj)}
        if data:
            env["CLAUDE_PLUGIN_DATA"] = str(self.data)
        return subprocess.run(["/bin/sh", str(self.script), *args], input="{}", capture_output=True, text=True,
                              check=False, env=env, cwd=self.proj)

    def test_a_ready_runtime_runs_the_guard_on_its_own_python_and_nothing_else(self) -> None:
        self.ready_runtime()
        self.stub(self.tmp / "a" / "python3", "HOST")
        out = self.run_script(f"{self.tmp}/a").stdout
        self.assertEqual(out, f"READY -I {self.script.parent}/../lib/guard.py\n")

    def test_a_runtime_that_is_not_ready_goes_to_the_bootstrap_on_the_host_python(self) -> None:
        self.stub(self.tmp / "a" / "python3", "HOST")
        cases = {"no marker": lambda: self.stub(self.rt / "venv" / "bin" / "python", "READY"),
                 "no python": lambda: (self.rt / "marker.json").write_text("{}")}
        for name, setup in cases.items():
            with self.subTest(name):
                setup()
                self.assertEqual(self.run_script(f"{self.tmp}/a").stdout, f"HOST -I -S {self.script.parent}/../lib/bootstrap.py hook\n")
                (self.rt / "marker.json").unlink(missing_ok=True)
                (self.rt / "venv" / "bin" / "python").unlink(missing_ok=True)

    def test_a_runtime_inside_the_project_is_never_executed(self) -> None:
        self.stub(self.tmp / "a" / "python3", "HOST")
        inside = self.proj / "data" / "runtime" / RUNTIME_ID
        self.stub(inside / "venv" / "bin" / "python", "EVIL")
        (inside / "marker.json").write_text("{}")
        env = {"PATH": f"{self.tmp}/a", "HOME": str(self.tmp), "CLAUDE_PROJECT_DIR": str(self.proj),
               "CLAUDE_PLUGIN_DATA": str(self.proj / "data")}
        out = subprocess.run(["/bin/sh", str(self.script)], input="{}", capture_output=True, text=True, check=False,
                             env=env, cwd=self.proj).stdout
        self.assertTrue(out.startswith("HOST -I -S "), out)

    def test_without_the_data_dir_variable_the_bootstrap_decides(self) -> None:
        self.stub(self.tmp / "a" / "python3", "HOST")
        self.assertTrue(self.run_script(f"{self.tmp}/a", data=False).stdout.startswith("HOST -I -S "))

    def test_the_first_trusted_python3_on_path_is_used_and_each_mode_passes_its_arguments(self) -> None:
        self.stub(self.tmp / "a" / "python3", "a")
        self.stub(self.tmp / "b" / "python3", "b")
        path = f"{self.tmp}/none:{self.tmp}/a:{self.tmp}/b"
        boot = f"{self.script.parent}/../lib/bootstrap.py"
        self.assertEqual(self.run_script(path, "session-start").stdout, f"a -I -S {boot} session-start\n")
        self.assertEqual(self.run_script(path, "cli", "status", "--problems").stdout,
                         f"a -I -S {boot} run status --problems\n")

    def test_path_entries_inside_the_project_and_world_writable_pythons_are_skipped(self) -> None:
        self.stub(self.proj / "bin" / "python3", "evil")
        self.stub(self.tmp / "loose" / "python3", "loose", 0o757)
        self.stub(self.tmp / "ok" / "python3", "ok")
        out = self.run_script(f"{self.proj}/bin:{self.tmp}/loose:{self.tmp}/ok", "session-start").stdout
        self.assertTrue(out.startswith("ok -I -S "), out)

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


class RealWrapperNotReady(Isolated):
    """The real wrapper and bootstrap on the host's python, with the install held off by a fresh failure stamp."""

    def setUp(self) -> None:
        super().setUp()
        (self.data / "runtime").mkdir(parents=True)
        (self.data / "runtime" / "failure.json").write_text(json.dumps({"at": time.time(), "reason": "offline"}))

    def run_script(self, *args: str, session: str = "s1") -> subprocess.CompletedProcess[str]:
        payload = json.dumps({"session_id": session, "tool_name": "Bash", "tool_input": {"command": "ls"}})
        return subprocess.run(["sh", str(HOOKS / "guardrails.sh"), *args], input=payload, capture_output=True,
                              text=True, check=False, env=dict(os.environ), cwd=self.proj)

    def test_the_hook_allows_loudly_once_per_session_and_never_silently_fails(self) -> None:
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        output = json.loads(first.stdout)
        self.assertIn("offline", output["systemMessage"])
        self.assertIn("NOT enforced", output["systemMessage"])
        self.assertNotIn("permissionDecision", output["hookSpecificOutput"])
        self.assertEqual(self.run_script().stdout, "")
        self.assertIn("offline", self.run_script(session="s2").stdout)

    def test_session_start_reports_the_failure_and_exits_zero(self) -> None:
        proc = self.run_script("session-start")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("offline", json.loads(proc.stdout)["systemMessage"])

    def test_every_cli_call_ensures_first_and_stops_with_the_reason(self) -> None:
        for argv in (("status",), ("engine", "status", "--bogus")):
            with self.subTest(argv=argv):
                proc = subprocess.run(["sh", str(ROOT / "bin" / "guardrails"), *argv], capture_output=True, text=True,
                                      check=False, env=dict(os.environ), cwd=self.proj)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("offline", proc.stderr)
                self.assertEqual(proc.stdout, "")


class RealWrapperReady(RealRuntime):
    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["sh", str(HOOKS / "guardrails.sh"), *args], input="{}", capture_output=True, text=True,
                              check=False, env=dict(os.environ), cwd=self.proj)

    def test_session_start_is_quiet_when_the_runtime_is_ready(self) -> None:
        proc = self.run_script("session-start")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_the_cli_runs_on_the_managed_python_and_engine_status_reads_the_runtime(self) -> None:
        proc = subprocess.run(["sh", str(ROOT / "bin" / "guardrails"), "engine", "status"], capture_output=True,
                              text=True, check=False, env=dict(os.environ), cwd=self.proj)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for line in ("runtime: ready", "pins: uv ", "ast-grep-py 0.45.3", "platform: ", "installed: Python 3.13."):
            self.assertIn(line, proc.stdout)

    def test_the_cli_works_through_the_wrapper(self) -> None:
        proc = subprocess.run(["sh", str(ROOT / "bin" / "guardrails"), "preset", "list"], capture_output=True, text=True,
                              check=False, env=dict(os.environ), cwd=self.proj)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("process-safety", proc.stdout)


if __name__ == "__main__":
    import unittest

    unittest.main()
