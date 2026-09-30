from __future__ import annotations  # noqa: I001

import io
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from helpers import HOOKS, LIB, AstIsolated, Isolated, ast_mode

import astrun
import engine
import matching
import policy
import wrappers
from test_corpus import ALLOW, DENY

K = "pk" + "ill"
BY_NAME: dict[str, Any] = {"any": [{"pattern": f"{K} $$$"}, {"pattern": "killall $$$"}]}
PROGRAM_RULE = policy.with_defaults({"match": {"program": [K, "killall"]}, "message": "m"})
AST_RULE = policy.with_defaults({"match": {"ast": BY_NAME}, "message": "m"})


def deny_text(out: dict[str, Any] | None) -> str:
    assert out is not None
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def is_denied(out: dict[str, Any] | None) -> bool:
    return out is not None and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


FORMS = [
    f"xargs -I{{}} {K} {{}}", f"xargs -I{{}} sh -c '{K} {{}}'", f"env - {K} x", f'env "A=1 B" {K} x',
    f"bash -c -- '{K} x'", f"sudo -nu bob {K} x", f"sudo -Eu bob {K} x", f"sudo -iu bob {K} x", f"xargs -i {K} {{}}",
    f"cat <<EOF\n\t$({K} x)\nEOF", f"cat <<EOF\n`{K} x`\nEOF", f"cat <<-EOF\n\t$({K} x)\n\tEOF",
    f"echo $(cat <<EOF\n$({K} x)\nEOF\n)", f'echo "$(nm $({K} z))"', f"sudo -u bob -- {K} x",
]


class PlainForms(Isolated):
    def test_the_plain_lexer_sees_every_form(self) -> None:
        for command in FORMS:
            with self.subTest(command=command):
                self.assertIsNotNone(matching.evaluate(command, {"r": PROGRAM_RULE}).kinds["r"])

    def test_quoted_heredocs_stay_data(self) -> None:
        for command in (f"cat <<'EOF'\n$({K} x)\nEOF", f"cat <<\"EOF\"\n`{K} x`\nEOF"):
            self.assertIsNone(matching.evaluate(command, {"r": PROGRAM_RULE}).kinds["r"], command)


class AstForms(AstIsolated):
    def test_the_ast_path_sees_every_form(self) -> None:
        for command in FORMS:
            with self.subTest(command=command):
                self.assertIsNotNone(matching.evaluate(command, {"r": AST_RULE}).kinds["r"])

    def test_quoted_heredocs_stay_data(self) -> None:
        for command in (f"cat <<'EOF'\n$({K} x)\nEOF", f"echo '{K} x'"):
            self.assertIsNone(matching.evaluate(command, {"r": AST_RULE}).kinds["r"], command)


class Layering(Isolated):
    def managed_rules(self) -> None:
        self.put(self.mpath, {"rules": {"no-kill": {"match": {"program": K}, "message": "No kill by name."},
                                        "no-strings": {"match": {"program": "strings", "args": "^-n"},
                                                       "message": "No strings."}}})

    def test_a_project_cannot_swallow_a_command_with_a_wrapper_flag(self) -> None:
        self.managed_rules()
        self.assertTrue(is_denied(self.hook(f"sudo -E {K} x", "a")))
        self.put(self.ppath, {"wrappers": {"sudo": {"flagsWithValue": ["-E"], "noCommandFlags": ["-E"]},
                                           "nohup": {"skip": 2}}})
        for n, command in enumerate([f"sudo -E {K} x", f"nohup {K} x", f"env -i {K} x"]):
            out = self.hook(command, f"b{n}")
            self.assertTrue(is_denied(out), command)

    def test_the_ignored_entry_is_reported_once_per_session(self) -> None:
        self.managed_rules()
        self.put(self.ppath, {"wrappers": {"sudo": {"flagsWithValue": ["-E"]}}})
        first = self.hook(f"sudo -E {K} x", "once")
        assert first is not None
        self.assertIn("wrapper 'sudo' is built in", first["systemMessage"])
        second = self.hook(f"sudo -E {K} y", "once")
        assert second is not None
        self.assertNotIn("systemMessage", second)

    def test_a_project_cannot_make_a_command_vanish_by_declaring_it_a_wrapper(self) -> None:
        self.managed_rules()
        self.put(self.ppath, {"wrappers": {K: {}, "strings": {"flagsWithValue": ["-n"]}}})
        for n, command in enumerate([f"{K} x", f"true && {K} x", "strings -n 4 /bin/ls", f"{K} -f y | head"]):
            self.assertTrue(is_denied(self.hook(command, f"v{n}")), command)

    def test_a_global_layer_is_held_to_the_same_rule(self) -> None:
        self.managed_rules()
        self.put(self.gpath, {"wrappers": {"sudo": {"flagsWithValue": ["-E"]}, K: {"skip": 3}}})
        self.assertTrue(is_denied(self.hook(f"sudo -E {K} x", "g1")))
        self.assertTrue(is_denied(self.hook(f"{K} x", "g2")))

    def test_the_cli_refuses_to_redefine_a_builtin(self) -> None:
        code, _, err = self.cli("wrapper", "add", "sudo", "--json", '{"flagsWithValue": ["-E"]}')
        self.assertEqual(code, 2)
        self.assertIn("built in", err)
        self.assertFalse(self.gpath.exists())

    def test_status_reports_ignored_wrapper_entries(self) -> None:
        self.put(self.ppath, {"wrappers": {"sudo": {"skip": 1}}})
        self.assertIn("wrapper 'sudo' is built in", self.cli("status")[1])


NAMES = [*wrappers.DEFAULTS, K, "strings", "grep", "find", "kill", "fresh", "nm", "mywrap"]
FLAGS = ["-E", "-x", "-n", "-u", "-9", "-r", "-s", "-c", "-v", "-i"]
CORPUS = [*DENY, *ALLOW[:20], f"sudo -E {K} x", f"nohup {K} x", f"env -i {K} x", f"command -p {K} x",
          f"timeout 5 {K} a", f"sudo -u bob -- {K} x", f"bash -c '{K} x'", f"x=$({K} y)", f"a | {K} b",
          "strings -n 4 /bin/ls", "grep -r foo .", "sudo grep -rn foo ."]


def random_entry(rng: random.Random) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if rng.random() < 0.7:
        entry["flagsWithValue"] = rng.sample(FLAGS, rng.randint(1, 4))
    if rng.random() < 0.4:
        entry["shellString"] = rng.choice(["-c", "rest"])
    if rng.random() < 0.4:
        entry["skip"] = rng.randint(0, 3)
    if rng.random() < 0.3:
        entry["assignments"] = True
    if rng.random() < 0.4:
        entry["noCommandFlags"] = rng.sample(FLAGS, rng.randint(1, 2))
    return entry


def random_state(rng: random.Random) -> dict[str, Any]:
    return {"wrappers": {rng.choice(NAMES): random_entry(rng) for _ in range(rng.randint(1, 6))}}


class NeverReducesDetection(Isolated):
    RULES: ClassVar[dict[str, policy.Rule]] = {"p": policy.with_defaults({"match": {"program": [K, "killall"]}, "message": "m"}),
             "s": policy.with_defaults({"match": {"program": "strings", "args": "^-n"}, "message": "m"}),
             "g": policy.with_defaults({"match": {"builtin": "grep-recursive"}, "message": "m"})}

    def check(self, rules: dict[str, policy.Rule], iterations: int, seed: int) -> None:
        rng = random.Random(seed)
        base = {c: matching.evaluate(c, rules).kinds for c in CORPUS}
        for _ in range(iterations):
            managed, _ = policy.managed_layer([("/m", random_state(rng))])
            table = policy.effective_wrappers(managed, random_state(rng), random_state(rng))
            for command in CORPUS:
                after = matching.evaluate(command, rules, table).kinds
                for rid, before in base[command].items():
                    if before is not None:
                        self.assertIsNotNone(after[rid], f"{rid} stopped matching {command!r} with {table}")

    def test_plain_rules(self) -> None:
        self.check(self.RULES, 120, 3)


class NeverReducesDetectionAst(AstIsolated):
    def test_ast_rules(self) -> None:
        rules = {"a": AST_RULE, "p": PROGRAM_RULE}
        rng = random.Random(9)
        base = {c: matching.evaluate(c, rules).kinds for c in CORPUS[:40]}
        for _ in range(12):
            table = policy.effective_wrappers({}, random_state(rng), random_state(rng))
            for command in CORPUS[:40]:
                after = matching.evaluate(command, rules, table).kinds
                for rid, before in base[command].items():
                    if before is not None:
                        self.assertIsNotNone(after[rid], f"{rid} stopped matching {command!r}")


class Isolation(Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.log = self.tmp / "uv.log"
        fake = self.tmp / "fake-uv"
        fake.write_text(f"#!/bin/sh\n{{ pwd; env | sort; echo \"ARGS $*\"; }} >> {self.log}\nexit 0\n")
        fake.chmod(0o755)
        os.environ["GUARDRAILS_UV"] = str(fake)
        self.put(self.gpath, {"rules": {"a": {"match": {"ast": BY_NAME}, "message": "m"}}})

    def test_uv_runs_in_the_data_dir_with_a_scrubbed_environment_and_no_config(self) -> None:
        hostile = {"UV_FIND_LINKS": "/evil", "UV_NO_INDEX": "1", "UV_INDEX_URL": "http://evil", "UV_PYTHON": "/evil",
                   "PIP_INDEX_URL": "http://evil", "PYTHONPATH": "/evil", "SSL_CERT_FILE": "/evil",
                   "UV_CACHE_DIR": str(self.tmp / "cache"), "HTTPS_PROXY": "http://proxy:1"}
        with mock.patch.dict(os.environ, hostile):
            self.hook(f"{K} x")
        text = self.log.read_text()
        self.assertEqual(os.path.realpath(text.splitlines()[0]), os.path.realpath(self.data))
        for name in ("UV_FIND_LINKS", "UV_NO_INDEX", "UV_INDEX_URL", "UV_PYTHON", "PIP_INDEX_URL", "PYTHONPATH",
                     "SSL_CERT_FILE", "CLAUDE_PROJECT_DIR", "GUARDRAILS_MANAGED_PATH"):
            self.assertNotIn(f"{name}=", text)
        self.assertIn("UV_CACHE_DIR=", text)
        self.assertIn("HTTPS_PROXY=http://proxy:1", text)
        args = next(ln for ln in text.splitlines() if ln.startswith("ARGS pip"))
        for flag in ("--no-config", "--require-hashes", "--only-binary :all:", "--default-index https://pypi.org/simple"):
            self.assertIn(flag, args)
        self.assertIn("-r " + astrun.REQUIREMENTS, args)

    def test_a_planted_uv_toml_and_env_cannot_get_a_fake_wheel_imported(self) -> None:
        mode = ast_mode()
        if mode is None:
            self.skipTest("uv cannot install ast-grep-py here")
        os.environ.pop("GUARDRAILS_UV", None)
        os.environ.pop("GUARDRAILS_AST_INPROCESS", None)
        pwned = self.tmp / "PWNED"
        links = self.tmp / "links"
        links.mkdir()
        wheel = links / f"ast_grep_py-{astrun.PIN}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("ast_grep_py/__init__.py", f"open({str(pwned)!r}, 'w').close()\n")
            zf.writestr(f"ast_grep_py-{astrun.PIN}.dist-info/METADATA",
                        f"Metadata-Version: 2.1\nName: ast-grep-py\nVersion: {astrun.PIN}\n")
            zf.writestr(f"ast_grep_py-{astrun.PIN}.dist-info/WHEEL",
                        "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            zf.writestr(f"ast_grep_py-{astrun.PIN}.dist-info/RECORD", "")
        (self.proj / "uv.toml").write_text(f'no-index = true\nfind-links = ["{links}"]\n')
        env = {**os.environ, "UV_NO_INDEX": "1", "UV_FIND_LINKS": str(links)}
        payload = json.dumps({"session_id": "iso", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": f"sudo {K} x"}})
        proc = subprocess.run(["bash", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True, text=True,
                              check=False, env=env, cwd=self.proj)
        self.assertFalse(pwned.exists())
        out = json.loads(proc.stdout)
        self.assertTrue(is_denied(out) or "unavailable" in proc.stdout)


class Degradation(Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"strings": {"match": {"program": "strings"}, "message": "No strings."},
                                        "ast": {"match": {"ast": BY_NAME}, "message": "No kill."}}})

    def main(self, command: str, tool: str = "Bash") -> dict[str, Any] | None:
        import guard

        payload = json.dumps({"session_id": "m1", "cwd": str(self.proj), "tool_name": tool,
                              "tool_input": {"command": command}})
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
            self.assertEqual(guard.main([]), 0)
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_an_unexpected_error_in_the_ast_stage_keeps_the_other_rules(self) -> None:
        for patched in (mock.patch.object(astrun, "call", side_effect=RuntimeError("boom")),
                        mock.patch.object(matching, "_evaluate", side_effect=KeyError("x"))):
            with patched:
                out = self.hook("strings x")
            self.assertTrue(is_denied(out))
            self.assertIn("unexpected error", json.dumps(out))

    def test_an_incomplete_worker_reply_degrades(self) -> None:
        import astworker

        for reply in ({"ok": True}, {"ok": True, "verdicts": None, "errors": {}}, {"ok": True, "verdicts": {},
                                                                                     "errors": None}):
            with mock.patch.dict(os.environ, {"GUARDRAILS_AST_INPROCESS": "1"}), \
                    mock.patch.object(astworker, "handle", return_value=reply):
                out = self.hook(f"strings x; {K} y", f"r{len(str(reply))}")
            self.assertTrue(is_denied(out), reply)
            self.assertIn("No strings.", deny_text(out))
            self.assertIn("No kill.", deny_text(out))
            self.assertIn("unavailable", deny_text(out))

    def test_a_failure_after_matching_falls_back_to_a_minimal_evaluation(self) -> None:
        with mock.patch.object(engine, "evaluate", side_effect=RuntimeError("late")):
            out = self.main("strings /bin/ls")
        self.assertTrue(is_denied(out))
        self.assertIn("No strings.", deny_text(out))
        self.assertIn("failed internally (RuntimeError)", deny_text(out))
        assert out is not None
        self.assertIn("failed internally", out["systemMessage"])

    def test_the_minimal_evaluation_applies_ast_rules_by_name_and_warns_otherwise(self) -> None:
        with mock.patch.object(engine, "evaluate", side_effect=RuntimeError("late")):
            self.assertTrue(is_denied(self.main(f"sudo {K} x")))
            out = self.main("ls")
        assert out is not None
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("failed internally", out["hookSpecificOutput"]["additionalContext"])

    def test_a_failure_inside_the_state_lock_is_not_silent(self) -> None:
        with mock.patch.object(engine.store, "locked", side_effect=ValueError("lock")):
            self.assertTrue(is_denied(self.main("strings x")))

    def test_when_even_the_minimal_evaluation_fails_the_user_is_told(self) -> None:
        with mock.patch.object(engine, "evaluate", side_effect=RuntimeError("a")), \
                mock.patch.object(engine, "run_safe", side_effect=RuntimeError("b")):
            out = self.main("strings x")
        assert out is not None
        self.assertIn("could not evaluate", out["systemMessage"])

    def test_payloads_without_a_command_stay_silent(self) -> None:
        for payload in ("", "garbage", "[]", json.dumps({"tool_name": "Bash", "tool_input": None})):
            import guard

            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
                self.assertEqual(guard.main([]), 0)
            self.assertEqual(out.getvalue(), "", payload)


class Limits(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"a": {"match": {"ast": BY_NAME}, "message": "No kill."}}})

    def test_seven_nested_shell_strings_are_analysed(self) -> None:
        command = f"{K} x"
        for _ in range(7):
            command = "bash -c " + shlex.quote(command)
        self.assertTrue(is_denied(self.hook(command)))
        nested = "sudo bash -c 'sudo bash -c \"sudo " + f"{K} x" + "\"'"
        self.assertTrue(is_denied(self.hook(nested, "n2")))

    def test_deep_eval_chains_fall_back_to_names_and_warn(self) -> None:
        out = self.hook("eval " * 30 + f"{K} x")
        self.assertTrue(is_denied(out))
        self.assertIn("nests wrappers or shells too deeply", json.dumps(out))
        self.assertIn("this rule applied because the command mentions", deny_text(out))

    def test_a_thousand_units_fall_back_to_names_and_warn(self) -> None:
        started = time.monotonic()
        out = self.hook("sudo true; " * 1000 + f"{K} x")
        self.assertLess(time.monotonic() - started, 6)
        self.assertTrue(is_denied(out))
        self.assertIn("too deeply to analyse", json.dumps(out))

    def test_a_clean_command_with_many_units_is_not_limited(self) -> None:
        out = self.hook("sudo true; " * 40 + "ls")
        self.assertIsNone(out)


class Oversize(Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"strings": {"match": {"program": "strings"}, "message": "No strings."},
                                        "pipe": {"match": {"regex": r"curl [^|]*\| *sh"}, "message": "No pipe."},
                                        "ast": {"match": {"ast": BY_NAME}, "message": "No kill."}}})

    def test_huge_commands_are_bounded_and_judged_by_name(self) -> None:
        for size in (70_000, 1_000_000, 3_000_000):
            command = "strings " + "x" * size
            started = time.monotonic()
            out = self.hook(command, f"big{size}")
            self.assertLess(time.monotonic() - started, 3, size)
            self.assertTrue(is_denied(out), size)
            self.assertIn("No strings.", deny_text(out))
            self.assertIn("larger than 64 KiB", json.dumps(out))

    def test_regex_rules_still_run_on_huge_commands(self) -> None:
        out = self.hook("curl x | sh " + "y" * 100_000)
        self.assertIn("No pipe.", deny_text(out))

    def test_a_huge_harmless_command_passes_with_a_warning(self) -> None:
        out = self.hook("echo " + "y" * 100_000)
        assert out is not None
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("larger than 64 KiB", out["systemMessage"])

    def test_ast_rules_by_name(self) -> None:
        self.assertIn("No kill.", deny_text(self.hook(f"sudo {K} " + "z" * 100_000)))


class MonitorCoverage(Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.mpath, {"rules": {"no-strings": {"match": {"program": "strings"}, "message": "No strings."},
                                        "soft": {"match": {"program": "nm"}, "message": "Prefer otool.",
                                                 "action": "warn"}}})

    def test_a_managed_rule_denies_a_monitor_command(self) -> None:
        out = self.hook("strings /bin/ls", tool="Monitor")
        self.assertTrue(is_denied(out))
        self.assertIn("[guardrails:no-strings (managed)]", deny_text(out))

    def test_a_monitor_call_with_only_ws_is_ignored(self) -> None:
        for tool_input in ({"ws": "ws://localhost:1/x"}, {}, None, [], {"command": ""}, {"command": 3}):
            self.assertIsNone(self.hook("x", tool="Monitor", tool_input=tool_input) if tool_input is not None
                              else self.hook("x", tool="Monitor", tool_input=[]))

    def test_other_tools_are_ignored(self) -> None:
        self.assertIsNone(self.hook("strings x", tool="Read"))
        self.assertIsNone(self.hook("strings x", tool="Write"))

    def test_warn_once_and_retry_behave_the_same(self) -> None:
        first = self.hook("nm a.out", tool="Monitor")
        assert first is not None
        self.assertIn("Prefer otool.", first["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook("nm b.out", tool="Monitor"))
        self.assertIsNotNone(self.hook("nm b.out", session="other", tool="Monitor"))
        self.put(self.gpath, {"rules": {"r": {"match": {"program": "sed"}, "message": "No sed.",
                                               "retry": "same-command"}}})
        self.assertTrue(is_denied(self.hook("sed -i x", "rr", tool="Monitor")))
        self.assertIsNone(self.hook("sed -i x", "rr", tool="Monitor"))
        self.assertTrue(is_denied(self.hook("sed -i y", "rr", tool="Monitor")))

    def test_a_monitor_acknowledgement_is_shared_with_bash(self) -> None:
        self.put(self.gpath, {"rules": {"r": {"match": {"program": "sed"}, "message": "No sed.",
                                               "retry": "same-command"}}})
        self.assertTrue(is_denied(self.hook("sed -i x", "sh", tool="Bash")))
        self.assertIsNone(self.hook("sed -i x", "sh", tool="Monitor"))

    def test_rules_for_monitor_only(self) -> None:
        self.put(self.gpath, {"rules": {"m": {"match": {"program": "sed"}, "message": "Monitor only.",
                                               "tool": "Monitor"}}})
        self.assertIsNone(self.hook("sed x", "b", tool="Bash"))
        self.assertTrue(is_denied(self.hook("sed x", "m", tool="Monitor")))

    def test_hooks_json_matches_bash_and_monitor(self) -> None:
        hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash|Monitor")


class Wrapper(Isolated):
    script = (HOOKS / "guardrails.sh").read_text()

    def stub(self, path: Path, label: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho "{label} $*"\n')
        path.chmod(0o755)

    def run_wrapper(self, script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        copy = self.tmp / "hooks" / "guardrails.sh"
        copy.parent.mkdir(exist_ok=True)
        copy.write_text(script)
        return subprocess.run(["/bin/sh", str(copy), "arg"], capture_output=True, text=True, check=False, env=env)

    def test_path_is_never_mutated_and_linuxbrew_is_linux_only(self) -> None:
        self.assertNotIn("PATH=", self.script.replace("for dir in $PATH", ""))
        self.assertNotIn("export", self.script)
        lines = [ln for ln in self.script.splitlines() if "linuxbrew" in ln]
        self.assertEqual(len(lines), 2)
        self.assertIn("uname -s", " ".join(lines))
        for line in self.script.splitlines():
            if re.search(r"(?<![\w/])/home/", line):
                self.assertIn("linuxbrew", line)

    def test_no_forks_for_paths(self) -> None:
        for forbidden in ("dirname", "$(cd", "command -v", "bash"):
            self.assertNotIn(forbidden, self.script)
        self.assertTrue(self.script.startswith("#!/bin/sh\n"))
        self.assertIn("${0%/*}", self.script)

    def test_selection_order_is_venv_then_path_then_fixed_locations(self) -> None:
        order = [self.script.index(x) for x in ("venv/bin/python", "for dir in $PATH", "/opt/homebrew/bin/python3",
                                                 "/usr/local/bin/python3", "/home/linuxbrew", "/usr/bin/python3")]
        self.assertEqual(order, sorted(order))

    def test_a_ready_venv_wins_and_a_stale_one_does_not(self) -> None:
        venv_py = self.data / "venv" / "bin" / "python"
        self.stub(venv_py, "venv")
        self.stub(self.tmp / "bin" / "python3", "path")
        env = {"PATH": str(self.tmp / "bin"), "CLAUDE_PLUGIN_DATA": str(self.data), "HOME": str(self.tmp)}
        stale = self.run_wrapper(self.script, env)
        self.assertTrue(stale.stdout.startswith("path -I -S "), stale.stdout)
        (self.data / "venv" / "guardrails-ast-ready").write_text("x\n")
        ready = self.run_wrapper(self.script, env)
        self.assertTrue(ready.stdout.startswith("venv -I "), ready.stdout)
        self.assertNotIn("-S", ready.stdout.split("guard.py")[0])
        self.assertTrue(ready.stdout.rstrip().endswith("guard.py arg"))

    def test_the_first_python3_on_path_is_used_and_the_path_is_left_alone(self) -> None:
        self.stub(self.tmp / "a" / "python3", "a")
        self.stub(self.tmp / "b" / "python3", "b")
        env = {"PATH": f"{self.tmp}/none:{self.tmp}/a:{self.tmp}/b", "CLAUDE_PLUGIN_DATA": str(self.data),
               "HOME": str(self.tmp)}
        self.assertTrue(self.run_wrapper(self.script, env).stdout.startswith("a -I -S "))

    def test_without_any_python_it_says_so_and_exits_zero(self) -> None:
        script = re.sub(r"/(opt/homebrew|usr/local)/bin/python3|/usr/bin/python3|/home/linuxbrew/\.linuxbrew/bin/python3",
                        "/nonexistent/python3", self.script)
        proc = self.run_wrapper(script, {"PATH": str(self.tmp / "empty"), "CLAUDE_PLUGIN_DATA": str(self.data),
                                         "HOME": str(self.tmp)})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("no python3 was found", json.loads(proc.stdout)["systemMessage"])

    def test_the_wrapper_runs_the_real_hook(self) -> None:
        payload = json.dumps({"session_id": "w", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "strings x"}})
        self.put(self.gpath, {"rules": {"s": {"match": {"program": "strings"}, "message": "No."}}})
        proc = subprocess.run(["sh", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True, text=True,
                              check=False, env=dict(os.environ))
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


class Mentions(Isolated):
    def test_validation(self) -> None:
        ok = {"match": {"ast": BY_NAME, "mentions": ["pkill", "killall"]}, "message": "m"}
        policy.validate_rule(ok)
        for bad in ([], "pkill", [""], ["a b"], [3]):
            with self.assertRaises(policy.Invalid):
                policy.validate_rule({"match": {"ast": BY_NAME, "mentions": bad}, "message": "m"})
        with self.assertRaises(policy.Invalid):
            policy.validate_rule({"match": {"mentions": ["x"]}, "message": "m"})

    def test_derived_names(self) -> None:
        rule = {"match": {"ast": {"pattern": "xargs kill $$$", "inside": {"kind": "pipeline"}}}, "message": "m"}
        self.assertEqual(policy.mentions_of(rule), ["xargs", "kill"])
        named = {"match": {"ast": {"kind": "command", "has": {"field": "name", "regex": "(^|/)pkill$"}}}}
        self.assertEqual(policy.mentions_of(named), ["pkill"])
        self.assertEqual(policy.mentions_of({"match": {"ast": {"kind": "pipeline"}}}), [])

    def test_settable_from_the_cli(self) -> None:
        rule = json.dumps({"match": {"ast": {"kind": "pipeline"}}, "message": "m"})
        self.assertEqual(self.cli("rule", "add", "r", "--json", rule)[0], 0)
        self.assertEqual(self.cli("rule", "set", "r", "mentions=xargs, sudo")[0], 0)
        self.assertEqual(self.get(self.gpath)["rules"]["r"]["match"]["mentions"], ["xargs", "sudo"])

    def test_word_boundaries(self) -> None:
        self.assertTrue(policy.mentioned("sudo /usr/bin/pkill x", ["pkill"]))
        self.assertFalse(policy.mentioned("echo pkills pkill-ish", ["pkill"]))


class Interpreter(Isolated):
    def test_wheel_range(self) -> None:
        self.assertEqual([astrun.suitable((3, m)) for m in (9, 10, 14, 15)], [False, True, True, False])
        self.assertFalse(astrun.suitable((2, 12)))

    def test_the_running_interpreter_is_kept_when_it_has_a_wheel(self) -> None:
        with mock.patch.object(sys, "version_info", (3, 12, 0, "final", 0)):
            self.assertEqual(astrun.find_interpreter(), sys.executable)

    def test_python_39_looks_for_a_newer_interpreter_and_otherwise_degrades_precisely(self) -> None:
        old = (3, 9, 6, "final", 0)
        newest = self.tmp / "bin" / "python3.13"
        newest.parent.mkdir()
        for minor in (11, 13):
            path = self.tmp / "bin" / f"python3.{minor}"
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        with mock.patch.object(sys, "version_info", old), mock.patch.dict(os.environ, {"PATH": str(self.tmp / "bin")}):
            found = astrun.find_interpreter()
        if not found.startswith(("/opt/homebrew", "/usr/local", "/usr/bin")):
            self.assertEqual(found, str(newest))
        with mock.patch.object(sys, "version_info", old), mock.patch.dict(os.environ, {"PATH": ""}), \
                mock.patch.object(astrun.os, "access", return_value=False), self.assertRaises(astrun.Unavailable) as ctx:
            astrun.find_interpreter()
        self.assertIn("no wheel for Python 3.9", str(ctx.exception))
        self.assertIn("3.10 to 3.14", str(ctx.exception))

    def test_ensure_reports_the_python_problem_before_trying_to_install(self) -> None:
        with mock.patch.object(astrun, "find_interpreter", side_effect=astrun.Unavailable("no wheel for Python 3.9")), \
                mock.patch.object(astrun, "_run") as run, self.assertRaises(astrun.Unavailable):
            astrun.ensure(str(self.tmp / "d"))
        run.assert_not_called()


class Requirements(unittest.TestCase):
    def test_the_committed_file_matches_the_pin_and_lists_every_wheel_platform(self) -> None:
        text = Path(astrun.REQUIREMENTS).read_text()
        self.assertTrue(text.startswith(f"ast-grep-py=={astrun.PIN} \\\n"))
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", text)
        self.assertEqual(len(hashes), 28)
        self.assertEqual(len(set(hashes)), 28)
        self.assertFalse(text.rstrip().endswith("\\"))

    def test_the_regeneration_script_reads_the_same_pin(self) -> None:
        script = (LIB.parent / "scripts" / "regen-ast-requirements.py").read_text()
        self.assertIn("lib/astrun.py", script)
        self.assertIn(f'PIN = "{astrun.PIN}"', (LIB / "astrun.py").read_text())


class Stamp(Isolated):
    def test_a_future_stamp_is_not_recent(self) -> None:
        stamp = self.tmp / "stamp"
        stamp.write_text("")
        future = time.time() + 1e9
        os.utime(stamp, (future, future))
        self.assertFalse(astrun._recent(str(stamp)))
        now = time.time()
        os.utime(stamp, (now, now))
        self.assertTrue(astrun._recent(str(stamp)))


if __name__ == "__main__":
    unittest.main()
