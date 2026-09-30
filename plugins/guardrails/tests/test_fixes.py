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
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from helpers import HOOKS, LIB, AstIsolated, Isolated

import astbin
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


class HostileExecutables(Isolated):
    def plant(self, name: str) -> Path:
        path = self.proj / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!/bin/sh\ntouch {self.tmp}/PWNED-{name}\necho '{{}}'\n")
        path.chmod(0o755)
        return path

    def test_no_environment_variable_selects_an_executable_a_url_or_an_engine_mode(self) -> None:
        for module in (astrun, astbin, matching, engine, policy):
            text = Path(module.__file__).read_text()
            for name in ("GUARDRAILS_UV", "GUARDRAILS_AST_BOOTSTRAP", "GUARDRAILS_AST_INPROCESS", "GUARDRAILS_PARITY",
                         "GUARDRAILS_AST_GREP", "CLAUDE_PLUGIN_ROOT"):
                self.assertNotIn(name, text)
        used = set(re.findall(r'environ(?:\.get)?[\[(]"([A-Z_a-z]+)"', (LIB / "astbin.py").read_text()))
        self.assertEqual(used, set())
        evil = self.plant("ast-grep")
        hostile = {"GUARDRAILS_AST_GREP": str(evil), "CLAUDE_PLUGIN_ROOT": str(self.proj), "PATH": str(evil.parent),
                   "GUARDRAILS_PARITY": "ast", "GUARDRAILS_AST_INPROCESS": "1"}
        with mock.patch.dict(os.environ, hostile):
            with self.assertRaises(astbin.Missing):
                astbin.locate(str(self.data))
            self.assertFalse(matching.PARITY)

    def test_a_node_modules_planted_in_the_project_is_never_the_engine(self) -> None:
        plat = astbin.detect()
        planted = self.proj / "node_modules" / "@ast-grep" / f"cli-{plat.npm}" / "ast-grep"
        planted.parent.mkdir(parents=True)
        planted.write_text(f"#!/bin/sh\ntouch {self.tmp}/PWNED\n")
        planted.chmod(0o755)
        (planted.parent / "package.json").write_text(json.dumps({"name": f"@ast-grep/cli-{plat.npm}",
                                                                 "version": astbin.pin()}))
        previous = os.getcwd()
        os.chdir(self.proj)
        self.addCleanup(os.chdir, previous)
        with self.assertRaises(astbin.Missing) as ctx:
            astbin.locate(str(self.data))
        self.assertIn("npm install missing", str(ctx.exception))
        self.assertFalse((self.tmp / "PWNED").exists())

    def test_the_engine_is_found_from_any_working_directory_and_project(self) -> None:
        plat = astbin.detect()
        binary = self.plugin_root / "node_modules" / "@ast-grep" / f"cli-{plat.npm}" / "ast-grep"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\necho 'ast-grep " + astbin.pin() + "'\n")
        binary.chmod(0o755)
        (binary.parent / "package.json").write_text(json.dumps({"name": f"@ast-grep/cli-{plat.npm}",
                                                                "version": astbin.pin()}))
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        for cwd in ("/", os.path.expanduser("~"), str(self.plugin_root), str(self.tmp)):
            for project in (str(self.proj), str(self.plugin_root), str(self.tmp), "/"):
                with self.subTest(cwd=cwd, project=project), mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": project}):
                    os.chdir(cwd)
                    self.assertEqual(astbin.locate(str(self.data)).binary, str(binary))

    def test_untrusted_reasons(self) -> None:
        root = self.tmp / "tree"
        exe = root / "bin" / "tool"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        exe.chmod(0o755)
        self.assertIsNone(astbin.untrusted(str(exe), str(root)))
        exe.parent.chmod(0o775)
        self.assertIsNone(astbin.untrusted(str(exe), str(root)), "a group-writable directory of ours is allowed")
        exe.parent.chmod(0o777)
        self.assertIn("writable by everyone", astbin.untrusted(str(exe), str(root)) or "")
        exe.parent.chmod(0o755)
        exe.chmod(0o775)
        self.assertIn("writable by group or others", astbin.untrusted(str(exe), str(root)) or "")
        exe.chmod(0o644)
        self.assertIn("not executable", astbin.untrusted(str(exe), str(root)) or "")
        exe.chmod(0o755)
        link = root / "bin" / "link"
        link.symlink_to(exe)
        self.assertIn("symlink", astbin.untrusted(str(link), str(root)) or "")
        self.assertIn("resolves outside", astbin.untrusted(str(exe), str(self.tmp / "elsewhere")) or "")
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.tmp)}):
            self.assertIsNone(astbin.untrusted(str(exe), str(root)))
        real_lstat = os.lstat

        class Foreign:
            def __init__(self, info: os.stat_result) -> None:
                self.st_mode, self.st_uid = info.st_mode, os.getuid() + 1

        with mock.patch.object(astbin.os, "lstat", lambda p, *a, **k: Foreign(real_lstat(p))):
            self.assertIn("owned by another user", astbin.untrusted(str(exe), str(root)) or "")

    def test_the_wrapper_never_runs_a_python3_from_the_project_path(self) -> None:
        evil = self.plant("python3")
        payload = json.dumps({"session_id": "h", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "ls"}})
        env = {**os.environ, "PATH": f"{evil.parent}:{os.environ.get('PATH', '')}"}
        subprocess.run(["/bin/sh", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True, text=True,
                       check=False, env=env, cwd=self.proj)
        env["PATH"] = str(evil.parent)
        script = (HOOKS / "guardrails.sh").read_text()
        only_path = re.sub(r"/opt/homebrew/bin/python3 /usr/local/bin/python3", "/nonexistent/a /nonexistent/b",
                           script).replace("/usr/bin/python3", "/nonexistent/c").replace("uname -s", "echo Darwin")
        copy = self.tmp / "hooks" / "guardrails.sh"
        copy.parent.mkdir()
        copy.write_text(only_path)
        proc = subprocess.run(["/bin/sh", str(copy)], input=payload, capture_output=True, text=True, check=False, env=env,
                              cwd=self.proj)
        self.assertIn("no python3 was found", proc.stdout)
        self.assertFalse((self.tmp / "PWNED-python3").exists())


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


class DegradationAst(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"strings": {"match": {"program": "strings"}, "message": "No strings."},
                                        "ast": {"match": {"ast": BY_NAME}, "message": "No kill."}}})

    def test_an_incomplete_worker_reply_degrades(self) -> None:
        import astworker

        for reply in ({"ok": True}, {"ok": True, "verdicts": None, "errors": {}}, {"ok": True, "verdicts": {},
                                                                                     "errors": None}):
            with mock.patch.object(astworker, "handle", return_value=reply):
                out = self.hook(f"strings x; {K} y", f"r{len(str(reply))}")
            self.assertTrue(is_denied(out), reply)
            self.assertIn("No strings.", deny_text(out))
            self.assertIn("No kill.", deny_text(out))
            self.assertIn("unavailable", deny_text(out))


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
        self.assertIn("too large once its wrappers", json.dumps(out))

    def test_a_clean_command_with_many_units_is_not_limited(self) -> None:
        out = self.hook("sudo true; " * 20 + "ls")
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
            self.assertIn("larger than 16 KiB", json.dumps(out))

    def test_regex_rules_still_run_on_huge_commands(self) -> None:
        out = self.hook("curl x | sh " + "y" * 100_000)
        self.assertIn("No pipe.", deny_text(out))

    def test_a_huge_harmless_command_passes_with_a_warning(self) -> None:
        out = self.hook("echo " + "y" * 100_000)
        assert out is not None
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("larger than 16 KiB", out["systemMessage"])

    def test_ast_rules_by_name(self) -> None:
        self.assertIn("No kill.", deny_text(self.hook(f"sudo {K} " + "z" * 100_000)))

    def test_padding_and_quoting_cannot_hide_a_name(self) -> None:
        self.put(self.gpath, {"rules": {"prog": {"match": {"program": K}, "message": "No prog."},
                                        "ast": {"match": {"ast": BY_NAME}, "message": "No ast."}}})
        pad = "echo " + "a" * 70_000 + "; "
        for n, name in enumerate(["p''kill x", 'p""kill x', "p\\kill x", "$'p\\x6bill' x", '"pkill" x',
                                  "$'\\160kill' x", "sudo 'pk''ill' x"]):
            out = self.hook(pad + name, f"pad{n}")
            self.assertTrue(is_denied(out), name)
            self.assertIn("No prog.", deny_text(out))
            self.assertIn("No ast.", deny_text(out))


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

    def stub(self, path: Path, label: str, code: int = 0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho "{label} $*"\nexit {code}\n')
        path.chmod(0o755)

    def isolated(self) -> str:
        return self.script.replace("/opt/homebrew/bin/python3 /usr/local/bin/python3", "/nonexistent/a /nonexistent/b") \
            .replace("/usr/bin/python3", "/nonexistent/c").replace("uname -s", "echo Darwin")

    def run_wrapper(self, script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        copy = self.tmp / "hooks" / "guardrails.sh"
        copy.parent.mkdir(exist_ok=True)
        copy.write_text(script)
        return subprocess.run(["/bin/sh", str(copy), *args], input="{}", capture_output=True, text=True, check=False,
                              env=env, cwd=self.proj)

    def env(self, path: str) -> dict[str, str]:
        return {"PATH": path, "CLAUDE_PLUGIN_DATA": str(self.data), "HOME": str(self.tmp),
                "CLAUDE_PROJECT_DIR": str(self.proj)}

    def test_path_is_never_mutated(self) -> None:
        self.assertNotIn("PATH=", self.script.replace("for dir in $PATH", ""))
        self.assertNotIn("export", self.script)

    def test_linux_brew_is_checked_only_after_uname_and_never_on_the_hot_path(self) -> None:
        lines = [ln for ln in self.script.splitlines() if "linuxbrew" in ln]
        self.assertEqual(len(lines), 2)
        self.assertLess(lines[0].index("uname -s"), lines[0].index("linuxbrew"))
        for line in self.script.splitlines():
            if re.search(r"(?<![\w/])/home/", line):
                self.assertIn("linuxbrew", line)

    def test_no_forks_for_paths_and_posix_sh(self) -> None:
        for forbidden in ("dirname", "$(cd", "command -v", "bash"):
            self.assertNotIn(forbidden, self.script)
        self.assertTrue(self.script.startswith("#!/bin/sh\n"))
        self.assertIn("${0%/*}", self.script)

    def test_selection_order(self) -> None:
        marks = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "uname -s", "for dir in $PATH",
                 "/usr/bin/python3")
        body = self.script.split("find_python() {", 1)[1]
        order = [body.index(x) for x in marks]
        self.assertEqual(order, sorted(order))
        self.assertIn("-I -S", self.script)
        self.assertNotIn("venv", self.script)

    def test_the_first_trusted_python3_on_path_is_used(self) -> None:
        self.stub(self.tmp / "a" / "python3", "a")
        self.stub(self.tmp / "b" / "python3", "b")
        env = self.env(f"{self.tmp}/none:{self.tmp}/a:{self.tmp}/b")
        self.assertTrue(self.run_wrapper(self.isolated(), env).stdout.startswith("a -I -S "))

    def test_path_entries_inside_the_project_are_skipped(self) -> None:
        self.stub(self.proj / "bin" / "python3", "evil")
        self.stub(self.tmp / "ok" / "python3", "ok")
        env = self.env(f"{self.proj}/bin:{self.tmp}/ok")
        self.assertTrue(self.run_wrapper(self.isolated(), env).stdout.startswith("ok -I -S "))

    def test_without_any_python_it_says_so_and_exits_zero(self) -> None:
        proc = self.run_wrapper(self.isolated(), self.env(str(self.tmp / "empty")))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("no python3 was found", json.loads(proc.stdout)["systemMessage"])

    def test_the_wrapper_runs_the_real_hook(self) -> None:
        payload = json.dumps({"session_id": "w", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "strings x"}})
        self.put(self.gpath, {"rules": {"s": {"match": {"program": "strings"}, "message": "No."}}})
        proc = subprocess.run(["/bin/sh", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True, text=True,
                              check=False, env=dict(os.environ))
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


def adversarial(n: int) -> dict[str, str]:
    return {
        "open-subst": "$(" * (n // 2) + f" {K} x", "open-subst-words": "$(a " * (n // 4) + K, "ticks": "`" * n,
        "tick-subst": "`$(" * (n // 3), "heredocs": "<<A\n" * (n // 4), "heredoc-open": "cat <<EOF\n" + "a\n" * (n // 2),
        "squote": "'" * n, "dquote": '"' * n, "list": "a;" * (n // 2) + K, "pipes": "a|" * (n // 2) + K,
        "braces": "{ " * (n // 2), "parens": "(" * n, "close": ")" * n, "word": "x" * n, "words": "a " * (n // 2),
        "redir": "<" * n, "amp": "&" * n, "ansi": "$'" * (n // 2), "backslash": "\\" * n,
        "assign-subst": "a=$(" * (n // 4), "balanced": "$(" * (n // 4) + ")" * (n // 4),
        "wrappers": "sudo " * (n // 5) + K, "dollar": "$" * n, "dash-heredoc": "<<-A\n\t" * (n // 6),
    }


class Complexity(Isolated):
    def test_the_lexer_is_linear_on_adversarial_shapes(self) -> None:
        import shellwords

        for size, budget in ((10_000, 1.0), (16_384, 1.0), (65_536, 2.0)):
            for name, text in adversarial(size).items():
                started = time.monotonic()
                try:
                    shellwords.simple_commands(text)
                except ValueError:
                    pass
                self.assertLess(time.monotonic() - started, budget, f"{name} at {size}")

    def test_the_whole_hook_answers_quickly_and_the_deny_rule_still_fires(self) -> None:
        self.put(self.gpath, {"rules": {
            "no-kill": {"match": {"program": K}, "message": "No kill."},
            "rx": {"match": {"regex": r"never-present-\d+"}, "message": "No rx."},
            "ast": {"match": {"ast": BY_NAME}, "message": "No ast."}}})
        for size in (10_000, 65_536, 1_000_000):
            for name, text in adversarial(size).items():
                started = time.monotonic()
                out = self.hook(text + f"; {K} x", f"c{size}{name}")
                self.assertLess(time.monotonic() - started, 3.0, f"{name} at {size}")
                self.assertTrue(is_denied(out), f"{name} at {size}")

    def test_the_subst_bomb_from_the_review(self) -> None:
        self.put(self.gpath, {"rules": {"no-kill": {"match": {"program": K}, "message": "No kill."}}})
        started = time.monotonic()
        out = self.hook("$(" * 32000 + f" {K} x")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(is_denied(out))


class Budget(Isolated):
    def test_the_overall_watchdog_degrades_to_names_and_warns(self) -> None:
        import guard

        self.put(self.gpath, {"rules": {"s": {"match": {"program": "strings"}, "message": "No strings."}}})

        def spin(*_: Any, **__: Any) -> Any:
            while True:
                pass

        payload = json.dumps({"session_id": "b", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "sudo 'str''ings' /bin/ls"}})
        out = io.StringIO()
        started = time.monotonic()
        with mock.patch.object(guard, "HOOK_BUDGET", 0.4), mock.patch.object(engine, "evaluate", spin), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
            guard.main([])
        self.assertLess(time.monotonic() - started, 2.0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("exceeded its time budget", result["systemMessage"])
        import signal

        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0.0)

    def test_a_local_limit_inside_the_budget_becomes_a_timeout_error(self) -> None:
        import watchdog

        watchdog.start(5.0)
        try:
            with self.assertRaises(TimeoutError), watchdog.limit(0.05):
                while True:
                    pass
        finally:
            watchdog.stop()

    def test_the_hook_budget_is_below_the_hook_timeout(self) -> None:
        import guard

        timeout = json.loads((HOOKS / "hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
        self.assertLess(guard.HOOK_BUDGET, timeout)
        self.assertLess(astrun.DEADLINE, guard.HOOK_BUDGET)


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


if __name__ == "__main__":
    unittest.main()
