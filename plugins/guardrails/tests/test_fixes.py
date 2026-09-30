from __future__ import annotations  # noqa: I001

import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import HOOKS, LIB, AstIsolated, Isolated

import astbin
import astcli
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
    "xargs -I{} KILL {}", "xargs -I{} sh -c 'KILL {}'", "env - KILL x", 'env "A=1 B" KILL x', "bash -c -- 'KILL x'",
    "sudo -nu bob KILL x", "sudo -Eu bob KILL x", "sudo -iu bob KILL x", "xargs -i KILL {}", "cat <<EOF\n$(KILL x)\nEOF",
    "echo $(cat <<EOF\n$(KILL x)\nEOF\n)", 'echo "$(nm $(KILL z))"', "sudo -u bob -- KILL x",
]


class Forms(AstIsolated):
    def test_program_sees_every_form_and_quoted_heredocs_stay_data(self) -> None:
        for command in FORMS:
            with self.subTest(command=command):
                self.assertIsNotNone(matching.evaluate(command.replace("KILL", K), {"r": PROGRAM_RULE}).kinds["r"])
        for command in (f"cat <<'EOF'\n$({K} x)\nEOF", f"cat <<\"EOF\"\n`{K} x`\nEOF"):
            self.assertIsNone(matching.evaluate(command, {"r": PROGRAM_RULE}).kinds["r"], command)


class Layering(AstIsolated):
    def managed_rules(self) -> None:
        self.put(self.mpath, {"rules": {"no-kill": {"match": {"program": K}, "message": "No kill by name."},
                                        "no-strings": {"match": {"program": "strings", "args": "^strings -n"},
                                                       "message": "No strings."}}})

    def test_lower_layers_only_add_wrapper_names(self) -> None:
        self.managed_rules()
        self.put(self.ppath, {"wrappers": {"mywrap": {}, "sudo": {"flagsWithValue": ["-E"], "noCommandFlags": ["-E"]},
                                           K: {"skip": 2}}})
        for n, command in enumerate([f"sudo -E {K} x", f"nohup {K} x", f"env -i {K} x", f"mywrap -x {K} x", f"{K} x",
                                     "strings -n 4 /bin/ls"]):
            self.assertTrue(is_denied(self.hook(command, f"b{n}")), command)

    def test_a_global_layer_is_held_to_the_same_rule(self) -> None:
        self.managed_rules()
        self.put(self.gpath, {"wrappers": {"sudo": {"skip": 3}, K: {}}})
        self.assertTrue(is_denied(self.hook(f"sudo -E {K} x", "g1")))
        self.assertTrue(is_denied(self.hook(f"{K} x", "g2")))

    def test_old_wrapper_entries_load_and_their_options_are_ignored(self) -> None:
        old = {"flagsWithValue": ["-x"], "shellString": "-c", "skip": 1, "assignments": True, "noCommandFlags": ["-v"]}
        self.put(self.gpath, {"wrappers": {"mywrap": old}, "rules": {"k": {"match": {"program": K}, "message": "m"}}})
        self.assertTrue(is_denied(self.hook(f"mywrap -x 3 {K} a")))
        self.assertEqual(self.cli("status", "--problems")[0], 0)

    def test_the_cli_takes_names_and_refuses_builtins(self) -> None:
        code, _, err = self.cli("wrapper", "add", "sudo")
        self.assertEqual(code, 2)
        self.assertIn("already built in", err)
        self.assertFalse(self.gpath.exists())

    def test_adding_names_never_reduces_detection(self) -> None:
        rules = {"p": PROGRAM_RULE, "s": policy.with_defaults({"match": {"program": "strings"}, "message": "m"}),
                 "g": policy.with_defaults({"match": {"builtin": "grep-recursive"}, "message": "m"})}
        corpus = [*DENY, *ALLOW[:20], f"sudo -E {K} x", f"nohup {K} x", f"timeout 5 {K} a", f"bash -c '{K} x'",
                  "grep -r foo .", "sudo grep -rn foo .", "strings -n 4 /bin/ls"]
        base = {c: matching.evaluate(c, rules).kinds for c in corpus}
        names = wrappers.effective({"wrappers": {K: {}, "strings": {}, "grep": {}, "mywrap": {}}})
        for command in corpus:
            after = matching.evaluate(command, rules, names).kinds
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
            for name in ("GUARDRAILS_UV", "GUARDRAILS_AST_BOOTSTRAP", "GUARDRAILS_AST_INPROCESS", "GUARDRAILS_AST_GREP",
                         "CLAUDE_PLUGIN_ROOT"):
                self.assertNotIn(name, text)
        used = set(re.findall(r'environ(?:\.get)?[\[(]"([A-Z_a-z]+)"', (LIB / "astbin.py").read_text()))
        self.assertEqual(used, set())
        evil = self.plant("ast-grep")
        hostile = {"GUARDRAILS_AST_GREP": str(evil), "CLAUDE_PLUGIN_ROOT": str(self.proj), "PATH": str(evil.parent),
                   "GUARDRAILS_AST_INPROCESS": "1"}
        with mock.patch.dict(os.environ, hostile), self.assertRaises(astbin.Missing):
            astbin.locate(str(self.data))

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


RULES_FOR_OUTAGES: dict[str, Any] = {
    "strings": {"match": {"program": "strings"}, "message": "No strings."},
    "ast": {"match": {"ast": BY_NAME}, "message": "No kill."},
    "pipe": {"match": {"regex": r"curl [^|]*\| *sh"}, "message": "No pipe."}}


class LoudAndAllow(AstIsolated):
    """Without a working engine, rules that need the parser cannot judge: the command is allowed, loudly."""

    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": RULES_FOR_OUTAGES})

    def both_channels(self, out: dict[str, Any] | None) -> str:
        assert out is not None
        self.assertNotIn("permissionDecision", out.get("hookSpecificOutput", {}))
        self.assertEqual(out["systemMessage"], out["hookSpecificOutput"]["additionalContext"])
        return out["systemMessage"]

    def test_a_missing_engine_allows_notices_once_per_session_and_names_the_rules(self) -> None:
        self.use_engine(False)
        text = self.both_channels(self.hook(f"strings x; {K} y", "a"))
        for needle in ("GUARDRAILS ENGINE MISSING", "NOT enforced", "program, args, builtin or match.ast",
                       "Rules that use regex", "still enforced", "Affected rules: ast, strings", "You MUST tell the user",
                       "guardrails engine install", "npm ci"):
            self.assertIn(needle, text)
        self.assertIsNone(self.hook("strings again", "a"))
        self.assertIn("GUARDRAILS ENGINE MISSING", self.both_channels(self.hook("strings x", "b")))

    def test_managed_parse_rules_are_named_as_failing_open(self) -> None:
        self.put(self.mpath, {"rules": {"m-zap": {"match": {"program": "zap"}, "message": "No zap."}}})
        self.use_engine(False)
        text = self.both_channels(self.hook("zap x", "m"))
        self.assertIn("1 of them are MANAGED rules, which fail open too", text)
        self.assertIn("MANAGED rules fail open too: m-zap", self.cli("status", "--problems")[1])

    def test_regex_rules_keep_running_while_the_engine_is_missing(self) -> None:
        self.use_engine(False)
        out = self.hook("curl x | sh", "r")
        self.assertTrue(is_denied(out))
        self.assertIn("No pipe.", deny_text(out))
        self.assertIn("GUARDRAILS ENGINE MISSING", deny_text(out))

    def test_each_missing_reason_says_only_fixes_that_can_work(self) -> None:
        self.use_engine(False)
        unsupported = astbin.Missing("unsupported platform (Plan9 mips)", True)
        old_glibc = astbin.Missing("glibc 2.17 is older than the 2.28 the wheel needs", wheel=False)
        with mock.patch.object(astbin, "locate", side_effect=unsupported):
            text = self.both_channels(self.hook("strings x", "u"))
        self.assertIn("cannot run on this platform", text)
        self.assertIn("unsupported platform (Plan9 mips)", text)
        self.assertNotIn("engine install", text)
        self.assertNotIn("npm ci", text)
        with mock.patch.object(astbin, "locate", side_effect=old_glibc):
            text = self.both_channels(self.hook("strings x", "g"))
        self.assertIn("npm ci", text)
        self.assertNotIn("engine install`", text)
        self.assertEqual(text.count("GUARDRAILS ENGINE MISSING"), 1)

    def test_an_untrusted_binary_is_reported_with_the_reason_it_was_ignored(self) -> None:
        plat = astbin.detect()
        planted = self.plugin_root / "node_modules" / "@ast-grep" / f"cli-{plat.npm}" / "ast-grep"
        planted.parent.mkdir(parents=True)
        planted.write_text("#!/bin/sh\n")
        planted.chmod(0o777)
        (planted.parent / "package.json").write_text(json.dumps({"name": f"@ast-grep/cli-{plat.npm}",
                                                                 "version": astbin.pin()}))
        self.use_engine(False)
        out = self.hook("strings x", "t")
        assert out is not None
        self.assertIn("ignored the npm binary as an executable to run", out["systemMessage"])
        self.assertIn("writable by group or others", out["systemMessage"])
        self.assertIn("GUARDRAILS ENGINE MISSING", out["systemMessage"])

    def test_each_engine_failure_allows_with_its_reason_once_per_session(self) -> None:
        ok = json.dumps({"runs": [{"results": []}], "version": "x"})
        failures = {"crash": ("echo boom >&2; exit 3", "exit 3"), "garbled": ("echo not-sarif", "unreadable output"),
                    "no canary": (f"echo '{ok}'", "built-in check match"), "empty": ("exit 0", "unreadable output")}
        for name, (body, reason) in failures.items():
            with self.subTest(name):
                self.stub_engine(body)
                text = self.both_channels(self.hook("strings x", name))
                self.assertIn("syntax-tree engine failed", text)
                self.assertIn(reason, text)
                self.assertNotIn("GUARDRAILS ENGINE MISSING", text)
                self.assertIsNone(self.hook("strings y", name))
                self.assertIn(reason, self.both_channels(self.hook("strings y", name + "-other")))

    def test_a_timeout_and_an_unexpected_error_are_reported_not_silent(self) -> None:
        self.stub_engine("sleep 5")
        with mock.patch.object(astrun, "DEADLINE", 0.3):
            started = time.monotonic()
            text = self.both_channels(self.hook("strings x", "slow"))
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("timed out", text)
        with mock.patch.object(astrun, "call", side_effect=RuntimeError("boom")):
            self.assertIn("unexpected error: RuntimeError", self.both_channels(self.hook("strings x", "boom")))

    def test_failures_still_apply_regex_rules(self) -> None:
        self.stub_engine("exit 3")
        out = self.hook("curl x | sh", "c")
        self.assertTrue(is_denied(out))
        self.assertIn("syntax-tree engine failed", deny_text(out))

    def test_rules_that_need_no_engine_never_touch_it(self) -> None:
        self.put(self.gpath, {"rules": {"pipe": RULES_FOR_OUTAGES["pipe"]}})
        with mock.patch.object(astrun, "call", side_effect=AssertionError("engine used")):
            self.assertTrue(is_denied(self.hook("curl x | sh")))
            self.assertIsNone(self.hook("ls"))

    def test_a_rule_that_does_not_compile_is_skipped_and_reported_once(self) -> None:
        self.put(self.gpath, {"rules": {"bad": {"match": {"ast": {"kind": "no_such_kind"}}, "message": "m"},
                                        "strings": RULES_FOR_OUTAGES["strings"]}})
        out = self.hook("strings x", "i")
        self.assertTrue(is_denied(out))
        assert out is not None
        self.assertIn("rule bad does not compile", out["systemMessage"])
        self.assertNotIn("systemMessage", self.hook("strings y", "i") or {})

    def test_a_failure_after_matching_falls_back_to_regex_rules_only(self) -> None:
        import guard

        payload = json.dumps({"session_id": "m1", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "curl x | sh"}})
        for failure in (RuntimeError("late"), TimeoutError("late")):
            out = io.StringIO()
            with mock.patch.object(engine, "evaluate", side_effect=failure), \
                    mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
                self.assertEqual(guard.main([]), 0)
            result = json.loads(out.getvalue())
            self.assertIn("No pipe.", result["hookSpecificOutput"]["permissionDecisionReason"])
            self.assertIn("only rules with regex were applied", result["systemMessage"])

    def test_a_failure_inside_the_state_lock_is_not_silent(self) -> None:
        self.put(self.gpath, {"rules": {"pipe": RULES_FOR_OUTAGES["pipe"]}})
        import guard

        payload = json.dumps({"session_id": "m1", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "curl x | sh"}})
        out = io.StringIO()
        with mock.patch.object(engine.store, "locked", side_effect=ValueError("lock")), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
            guard.main([])
        self.assertIn("No pipe.", out.getvalue())

    def test_when_even_the_minimal_evaluation_fails_the_user_is_told(self) -> None:
        import guard

        payload = json.dumps({"session_id": "m1", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "strings x"}})
        out = io.StringIO()
        with mock.patch.object(engine, "evaluate", side_effect=RuntimeError("a")), \
                mock.patch.object(engine, "run_safe", side_effect=RuntimeError("b")), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
            guard.main([])
        self.assertIn("could not evaluate", json.loads(out.getvalue())["systemMessage"])

    def test_payloads_without_a_command_stay_silent(self) -> None:
        import guard

        for payload in ("", "garbage", "[]", json.dumps({"tool_name": "Bash", "tool_input": None})):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", out):
                self.assertEqual(guard.main([]), 0)
            self.assertEqual(out.getvalue(), "", payload)


class FailurePolicy(AstIsolated):
    """What a failing engine means for a command that is big enough for the content to be the cause."""

    BIG = 9 * 1024

    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"z": {"match": {"program": "zap"}, "message": "No zap."}}})

    def slow(self, after: int = 0) -> Any:
        """A scan that answers normally `after` times, then times out."""
        real = astcli.Cli.scan
        calls = [0]

        def scan(cli: astcli.Cli, *args: Any, **kwargs: Any) -> Any:
            calls[0] += 1
            if calls[0] > after:
                raise astcli.Unavailable("timed out", "timeout")
            return real(cli, *args, **kwargs)

        return mock.patch.object(astcli.Cli, "scan", scan)

    def test_a_timeout_on_a_big_command_is_a_denial_and_on_a_small_one_a_warning(self) -> None:
        with self.slow():
            big = self.hook("sudo " * (self.BIG // 5) + "zap x", "big")
            small = self.hook("sudo " * 20 + "zap x", "small")
        self.assertTrue(is_denied(big))
        self.assertIn("command too complex to check (the parser timed out on it)", deny_text(big))
        self.assertIn("script file", deny_text(big))
        assert small is not None
        self.assertNotIn("permissionDecision", small["hookSpecificOutput"])
        self.assertIn("timed out", small["systemMessage"])

    def test_a_hit_from_a_completed_level_stands_when_a_deeper_level_fails(self) -> None:
        with self.slow(after=1):
            out = self.hook("zap x; bash -c 'echo hi'", "hit")
            other = self.hook("echo hi; bash -c 'echo " + "a" * self.BIG + "'", "big")
            tiny = self.hook("echo hi; bash -c 'echo hi'", "tiny")
        self.assertTrue(is_denied(out))
        self.assertIn("No zap.", deny_text(out))
        self.assertTrue(is_denied(other))
        self.assertIn("command too complex to check", deny_text(other))
        assert tiny is not None
        self.assertNotIn("permissionDecision", tiny["hookSpecificOutput"])

    def test_warn_only_rules_never_turn_a_refusal_into_a_denial(self) -> None:
        self.put(self.gpath, {"rules": {"z": {"match": {"program": "zap"}, "message": "Careful.", "action": "warn"}}})
        with self.slow():
            out = self.hook("sudo " * (self.BIG // 5) + "zap x", "warn")
        assert out is not None
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("allowed because only warn rules", out["systemMessage"])
        huge = self.hook("echo " + "y" * (matching.MAX_COMMAND + 1), "huge")
        assert huge is not None
        self.assertNotIn("permissionDecision", huge["hookSpecificOutput"])

    def test_failures_are_keyed_by_class_and_repeat_after_ten_minutes(self) -> None:
        self.stub_engine("exit 3")
        first = self.hook("zap x", "s")
        assert first is not None
        self.assertIn("crash", json.dumps(self.get(self.gpath)["sessions"]["s"]["engineFailure"]))
        self.assertIsNone(self.hook("zap y", "s"))
        real_time = time.time
        with mock.patch.object(engine.time, "time", lambda: real_time() + 601):
            again = self.hook("zap z", "s")
        assert again is not None
        self.assertIn("syntax-tree engine failed", again["systemMessage"])

    def test_timeout_texts_share_one_key(self) -> None:
        import matching as m

        texts = ["timed out", "timed out after 4.0s"]
        keys = set()
        for text in texts:
            ev = m.Evaluation()
            ev.outage = astcli.Unavailable(text, "timeout")
            ev.unevaluated = {"z"}
            keys |= {key for key, _ in ev.warnings()}
        self.assertEqual(keys, {"engine-failed:timeout"})

    def test_an_unwritable_session_warns_on_every_failure(self) -> None:
        self.stub_engine("exit 3")
        with mock.patch.object(engine.store, "write", side_effect=OSError("read-only")):
            for _ in range(2):
                self.assertIn("syntax-tree engine failed", json.dumps(self.hook("zap x", "ro")))

    def test_status_and_rule_test_name_the_last_failure(self) -> None:
        self.stub_engine("exit 3")
        self.hook("zap x", "s")
        out = self.cli("status", "--session-id", "s")[1]
        self.assertIn("last engine failure", out)
        self.assertIn("crash", out)
        rule = json.dumps({"match": {"program": "zap"}, "message": "m"})
        self.assertIn("crash", self.cli("rule", "test", "--json", rule, "zap x")[1])


class Oversize(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": RULES_FOR_OUTAGES})

    def test_a_command_over_the_cap_is_denied_and_never_parsed(self) -> None:
        for command in ("echo " + "y" * matching.MAX_COMMAND, "strings " + "é" * matching.MAX_COMMAND,
                        "echo " + "a" * matching.MAX_COMMAND + f"; {K} x"):
            with mock.patch.object(astrun, "call", side_effect=AssertionError("parsed")):
                out = self.hook(command, f"big{len(command)}")
            self.assertTrue(is_denied(out))
            self.assertIn("command too large to check", deny_text(out))

    def test_regex_only_rules_need_no_parser_so_size_alone_is_not_a_denial(self) -> None:
        self.put(self.gpath, {"rules": {"pipe": RULES_FOR_OUTAGES["pipe"]}})
        self.assertIsNone(self.hook("echo " + "y" * (matching.MAX_COMMAND + 10)))
        self.assertTrue(is_denied(self.hook("curl x | sh " + "y" * (matching.MAX_COMMAND + 10))))

    def test_just_under_the_cap_is_analysed(self) -> None:
        started = time.monotonic()
        out = self.hook("echo " + "y" * (matching.MAX_COMMAND - 100) + f"; {K} x")
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(is_denied(out))
        self.assertIn("No kill.", deny_text(out))

    def test_the_cap_counts_bytes(self) -> None:
        out = self.hook("echo " + "日" * (matching.MAX_COMMAND // 3 + 10))
        self.assertTrue(is_denied(out))


class Limits(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"a": {"match": {"program": K}, "message": "No kill."}}})

    def test_seven_nested_shell_strings_are_analysed(self) -> None:
        command = f"{K} x"
        for _ in range(7):
            command = "bash -c " + shlex.quote(command)
        self.assertTrue(is_denied(self.hook(command)))

    def test_nesting_beyond_the_caps_is_refused_not_passed(self) -> None:
        command = f"{K} x"
        for _ in range(9):
            command = "bash -c " + shlex.quote(command)
        out = self.hook(command, "deep")
        self.assertTrue(is_denied(out))
        self.assertIn("command too complex to check", deny_text(out))
        self.assertIn("nests shell strings too deeply", deny_text(out))
        out = self.hook("eval " * 30 + f"{K} x", "evals")
        self.assertIn("nests shell strings too deeply", deny_text(out))
        out = self.hook("; ".join(f"bash -c 'echo {n}'" for n in range(100)), "wide")
        self.assertIn("too many shell strings", deny_text(out))

    def test_a_clean_command_with_many_shell_strings_is_not_limited(self) -> None:
        self.assertIsNone(self.hook("bash -c 'true'; " * 200 + "ls"))
        self.assertIsNone(self.hook("sudo true; " * 500 + "ls"))


class MonitorCoverage(AstIsolated):
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


class Wrapper(AstIsolated):
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
        self.link_engine()
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


RUNNABLE = {"ticks", "heredocs", "heredoc-open", "squote", "dquote", "list", "pipes", "word", "words", "ansi",
            "backslash", "balanced", "wrappers", "dollar", "dash-heredoc"}


class Complexity(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {
            "no-kill": {"match": {"program": K}, "message": "No kill."},
            "rx": {"match": {"regex": r"never-present-\d+"}, "message": "No rx."},
            "ast": {"match": {"ast": BY_NAME}, "message": "No ast."}}})

    def test_adversarial_shapes_are_answered_quickly_and_the_deny_rule_still_fires(self) -> None:
        for size in (2_000, 10_000):
            for name, text in adversarial(size).items():
                started = time.monotonic()
                out = self.hook(text + f"; {K} x", f"c{size}{name}")
                self.assertLess(time.monotonic() - started, 3.0, f"{name} at {size}")
                if name in RUNNABLE:
                    self.assertTrue(is_denied(out), f"{name} at {size}")

    def test_the_largest_shapes_never_crash_and_are_never_silent(self) -> None:
        for name in ("open-subst", "balanced", "word", "list", "heredoc-open"):
            started = time.monotonic()
            out = self.hook(adversarial(100_000)[name] + f"; {K} x", f"big{name}")
            self.assertLess(time.monotonic() - started, 8.0, name)
            self.assertTrue(is_denied(out) or (out is not None and "syntax-tree engine" in json.dumps(out)), name)


class Budget(AstIsolated):
    def test_the_overall_watchdog_applies_regex_rules_and_warns(self) -> None:
        import guard

        self.put(self.gpath, {"rules": {"s": {"match": {"regex": r"\bstrings\b"}, "message": "No strings."}}})

        def spin(*_: Any, **__: Any) -> Any:
            while True:
                pass

        payload = json.dumps({"session_id": "b", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "sudo strings /bin/ls"}})
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


class RetiredMentions(AstIsolated):
    def test_a_state_file_with_mentions_still_loads_and_the_key_does_nothing(self) -> None:
        rule = {"match": {"ast": BY_NAME, "mentions": ["pkill", "killall"]}, "message": "m"}
        policy.validate_rule(rule)
        self.put(self.gpath, {"rules": {"r": rule}})
        self.assertIsNone(self.hook("echo pkill"))
        self.assertTrue(is_denied(self.hook(f"{K} x")))
        self.assertEqual(self.cli("status", "--problems")[0], 0)
        self.assertEqual(self.cli("rule", "set", "r", "mentions=x")[0], 2)


if __name__ == "__main__":
    unittest.main()
