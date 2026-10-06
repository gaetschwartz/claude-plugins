from __future__ import annotations  # noqa: I001

import os
from typing import Any

from helpers import AstIsolated, plain

import cli
import engine
import matching
import policy


class Presets(AstIsolated):
    def test_all_presets_are_valid_and_self_contained(self) -> None:
        self.assertEqual(cli.preset_names(), ["docs-first", "modern-cli", "process-safety", "shell-hygiene"])
        for name in cli.preset_names():
            preset = cli.load_preset(name)
            self.assertTrue(preset.get("description"))
            for rid, rule in preset["rules"].items():
                with self.subTest(preset=name, rule=rid):
                    policy.Rule.from_json(rule)
                    for mode in policy.json_modes(rule):
                        self.assertIn(mode, preset.get("modes", {}))

    def test_modern_cli_rules_are_one_line_warnings(self) -> None:
        rules = cli.load_preset("modern-cli")["rules"]
        self.assertEqual(rules["find-fd"]["when"], {"bin": ["fd", "fdfind"]})
        self.assertEqual(rules["grep-rg"]["when"], {"bin": "rg"})
        self.assertEqual(rules["grep-rg"]["match"]["all"][0], {"command": ["grep", "egrep", "fgrep"]})
        self.assertIn("{found}", rules["find-fd"]["message"])
        self.assertEqual(sorted(rules), ["cargo-nextest", "du-dust", "find-fd", "grep-rg"])
        for rid, rule in rules.items():
            with self.subTest(rule=rid):
                self.assertEqual(rule["action"], "warn")
                for key in ("retry", "messageShort"):
                    self.assertNotIn(key, rule)
                self.assertEqual("messages" in rule, rid == "du-dust")
                for case in rule.get("messages", []):
                    self.assertNotIn("\n", case["text"])
                self.assertNotIn("\n", rule["message"])
                self.assertLess(len(rule["message"]), 200)
                self.assertNotRegex(rule["message"].replace("{found}", ""), "[{}]")

    def test_install_global_and_idempotent(self) -> None:
        code, out, _ = self.cli("preset", "install", "docs-first")
        self.assertEqual(code, 0)
        self.assertIn("rule no-strings: added", out)
        self.assertIn("mode reverse-engineering: added (agent may enable: yes)", out)
        state = self.get(self.gpath)
        self.assertEqual(sorted(state["rules"]), ["binary-spelunking", "no-strings"])
        self.assertEqual(state["rules"]["no-strings"]["setBy"]["reason"], "preset docs-first")
        self.assertTrue(state["modes"]["reverse-engineering"]["agentMayEnable"])
        out = self.cli("preset", "install", "docs-first")[1]
        self.assertIn("rule no-strings: unchanged", out)
        self.assertIn("mode reverse-engineering: kept existing declaration", out)
        self.cli("mode", "declare", "reverse-engineering")
        self.cli("preset", "install", "docs-first")
        mode = self.get(self.gpath)["modes"]["reverse-engineering"]
        self.assertEqual((mode["agentMayEnable"], bool(mode["description"])), (False, True))

    def test_install_subset_brings_only_referenced_modes(self) -> None:
        self.assertEqual(self.cli("preset", "install", "process-safety", "--only", "kill-9")[0], 0)
        state = self.get(self.gpath)
        self.assertEqual(sorted(state["rules"]), ["kill-9"])
        self.assertEqual(state["modes"], {})

    def test_install_unknown(self) -> None:
        self.assertEqual(self.cli("preset", "install", "nope")[0], 2)
        self.assertEqual(self.cli("preset", "install", "docs-first", "--only", "nope")[0], 2)
        self.assertEqual(self.cli("preset", "show", "../etc/passwd")[0], 2)

    def test_docs_first_end_to_end(self) -> None:
        self.cli("preset", "install", "docs-first")
        out = self.hook("strings /bin/ls")
        assert out is not None
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.hook("strings /bin/ls"))
        warn = self.hook("nm -g libfoo.dylib")
        assert warn is not None
        self.assertIn("additionalContext", warn["hookSpecificOutput"])

    def test_modern_cli_needs_tools(self) -> None:
        self.cli("preset", "install", "modern-cli")
        os.environ["PATH"] = str(self.tmp / "empty")
        self.assertIsNone(self.hook("find . -name x"))
        self.assertIsNone(self.hook("grep -r x ."))

    def stub_path(self, *names: str) -> str:
        folder = self.tmp / ("bin-" + "-".join(names))
        folder.mkdir()
        for name in names:
            (folder / name).write_text("#!/bin/sh\n")
            (folder / name).chmod(0o755)
        return str(folder)

    def warning(self, command: str) -> str | None:
        out = self.hook(command, session=f"s-{command}-{os.environ['PATH']}")
        if out is None:
            return None
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        return out["hookSpecificOutput"]["additionalContext"]

    def test_modern_cli_catches_and_passes_with_the_binary_it_finds(self) -> None:
        self.cli("preset", "install", "modern-cli")
        catches = {"find . -name x": "find-fd", "sudo find /tmp -type f": "find-fd", "grep -r x .": "grep-rg",
                   "egrep -R y src": "grep-rg", "grep -d recurse z .": "grep-rg", "find . -exec rm {} \\;": "find-fd"}
        passes = ["fd x", "rg x", "grep x file", "grep -- -r file", "echo find", "ls -la"]
        for names, fd_name in ((("fd", "rg"), "fd"), (("fdfind", "rg"), "fdfind")):
            os.environ["PATH"] = self.stub_path(*names)
            for command, rid in catches.items():
                with self.subTest(path=names, command=command):
                    text = self.warning(command)
                    assert text is not None
                    self.assertIn(f"[guardrails:{rid}]", plain(text))
                    if rid == "find-fd":
                        self.assertIn(f"Prefer `{fd_name}` over `find`", text)
                        self.assertNotIn("{found}", text)
                        other = "fdfind" if fd_name == "fd" else "fd "
                        self.assertNotIn(f"`{other}", text)
            for command in passes:
                with self.subTest(path=names, command=command):
                    self.assertIsNone(self.warning(command))
        os.environ["PATH"] = self.stub_path("fdfind")
        self.assertIn("fdfind", self.warning("find . -name y") or "")
        self.assertIsNone(self.warning("grep -r y ."))

    def test_modern_cli_warning_is_one_line_and_shown_once_per_session(self) -> None:
        self.cli("preset", "install", "modern-cli")
        os.environ["PATH"] = self.stub_path("fd")
        first = self.hook("find . -name x", session="once")
        assert first is not None
        self.assertNotIn("permissionDecision", first["hookSpecificOutput"])
        self.assertEqual(first["hookSpecificOutput"]["additionalContext"].count("\n"), 0)
        self.assertIsNone(self.hook("find . -name x", session="once"))
        self.assertIsNotNone(self.hook("find . -name x", session="other"))


def rule_of(preset: str, rid: str) -> policy.Rule:
    return policy.Rule.from_json(cli.load_preset(preset)["rules"][rid])


class Examples(AstIsolated):
    def test_every_rule_of_the_tool_presets_has_catch_and_pass_examples_that_hold(self) -> None:
        for name in ("modern-cli", "shell-hygiene"):
            preset = cli.load_preset(name)
            self.assertEqual(sorted(preset["examples"]), sorted(preset["rules"]))
            for rid, example in preset["examples"].items():
                rule = policy.Rule.from_json(preset["rules"][rid])
                self.assertGreaterEqual(len(example["catch"]), 3)
                self.assertGreaterEqual(len(example["pass"]), 3)
                for kind, expected in (("catch", True), ("pass", False)):
                    for command in example[kind]:
                        with self.subTest(preset=name, rule=rid, kind=kind, command=command):
                            self.assertEqual(matching.evaluate(command, {"r": rule}).kinds["r"] is not None, expected)

    def says(self, preset: str, rid: str, command: str) -> str:
        rule = rule_of(preset, rid)
        evaluation = matching.evaluate(command, {"r": rule})
        self.assertIsNotNone(evaluation.kinds["r"], command)
        return engine.texts_of(rule, evaluation.details.get("r")).full

    def test_modern_cli_messages_name_the_replacement(self) -> None:
        expect = {"cargo-nextest": ("cargo test -p foo my_test", ("cargo nextest run", "cargo test --doc", "`--nocapture` is `--no-capture`")),
                  "du-dust": ("du -sh /tmp/x", ("`dust -d 1 /tmp/x` for one level",))}
        for rid, (command, needles) in expect.items():
            text = self.says("modern-cli", rid, command)
            for needle in needles:
                with self.subTest(rule=rid):
                    self.assertIn(needle, text)

    def test_pipe_status_cases(self) -> None:
        self.assertIn("pipestatus[1]", self.says("shell-hygiene", "pipe-status", "make | tail; echo $?"))
        self.assertIn("pipestatus[1]", self.says("shell-hygiene", "pipe-status", "make | tail > o 2>&1; echo $?"))
        head = self.says("shell-hygiene", "pipe-status", "make | head -5 && echo ok")
        self.assertIn("SIGPIPE", head)
        self.assertNotIn("pipestatus", head)
        default = self.says("shell-hygiene", "pipe-status", "make | tail -5 && echo ok")
        self.assertIn("set -o pipefail", default)
        self.assertNotIn("SIGPIPE", default)

    def test_pipe_status_names_the_last_command_of_the_whole_pipeline(self) -> None:
        for command, last in (("make | tail -5 && echo ok", "tail"), ("a | b | sort && echo ok", "sort"),
                              ("a | b | c | wc -l && echo ok", "wc"), ("make | cat > o 2>&1 && echo ok", "cat"),
                              ("make | /usr/bin/sort && echo ok", "/usr/bin/sort"),
                              ("make | tail | wc && echo ok", "wc"), ("make | sort -u | tee out && echo ok", "tee"),
                              ("make 2>&1 | grep err | tail -5 && echo ok", "tail"),
                              ("grep pat f | wc -l && echo found", "wc")):
            with self.subTest(command=command):
                self.assertIn(f"last command's (`{last}`)", self.says("shell-hygiene", "pipe-status", command))

    def fires(self, command: str) -> bool:
        return matching.evaluate(command, {"r": rule_of("shell-hygiene", "pipe-status")}).kinds["r"] is not None

    def test_pipe_status_ignores_a_pipeline_that_only_feeds_a_separator_or_label_echo(self) -> None:
        for consumer in ("echo ---", 'echo "---"', 'echo "====="', 'echo "=====PYPROJECT====="', 'echo "--- listing ---"',
                         'echo "### Section"', 'echo ""', "echo ''", "echo", "echo -n ---", "echo -e '\\n--- x ---'",
                         "echo ===== PYPROJECT =====", "echo --- >&2", "printf '\\n'", "printf '=== %s ===\\n' x",
                         'printf "\\n=== x ===\\n"', "/bin/echo ---"):
            for form in ("make | tail && {}", "make | tail || {}", "a | b | sort && {}", "make | tail >o && {}",
                         "(make | tail) && {}", "x && make | tail && {}", "{{ make | tail; }} && {}",
                         "if make | tail && {}; then y; fi"):
                with self.subTest(command=form.format(consumer)):
                    self.assertFalse(self.fires(form.format(consumer)))

    def test_pipe_status_still_denies_an_echo_that_reports_or_expands(self) -> None:
        for consumer in ("echo found", "echo ok", 'echo "done"', 'echo "exit=$?"', 'echo "--- $x ---"', 'echo "--- $(date) ---"',
                         'echo "ok ---"', "echo ok ---", 'echo "- ok"', 'echo "x" "---"', "printf '%s\\n' done",
                         "printf '--- %s ---\\n' \"$x\"", "echo ---; echo $?", "git status", "true"):
            with self.subTest(consumer=consumer):
                self.assertTrue(self.fires(f"make | tail && {consumer}"))
        for command in ("make | tail && echo --- ; echo $?", "make | tail && echo --- && make | tail && git status",
                        "cargo build 2>&1 | tail -3 && echo \"exit=$?\"", "git push 2>&1 | tail -2 && git status",
                        "grep pat f | head && echo found", "make | tail && echo done", 'make | tail && echo "done"'):
            with self.subTest(command=command):
                self.assertTrue(self.fires(command))

    def test_pipe_status_ignores_a_deliberately_discarded_status(self) -> None:
        for command in ("make | tail || true", "make | tail || :", "make | tail >log 2>&1 || true", "a | b | sort || true",
                        "x && make | tail || true", "(make | tail) || true", "{ make | tail; } || true",
                        "make | tail || true; echo $?", "make | tail || true && echo ok",
                        "if make | tail || true; then y; fi"):
            with self.subTest(command=command):
                self.assertFalse(self.fires(command))
        for command in ("make | tail && true", "make | tail || false", "make | tail || true foo", "make | tail && git status || true",
                        "make | tail || echo failed", "make | tail && echo ok || true"):
            with self.subTest(command=command):
                self.assertTrue(self.fires(command))

    def test_pipe_status_reads_question_mark_only_in_the_next_command(self) -> None:
        for command in ("make | tail; echo $?", "make | tail\necho $?", "make | tail;\necho $?", "make | tail && echo $?",
                        "(make | tail); echo $?", "{ make | tail; }; echo $?", "make | tail >o; echo $?",
                        "make | tail # note\necho $?", 'make | tail; echo "rc=$?"', "make | tail; [ $? -eq 0 ] && echo ok",
                        "make | tail; if [ $? -ne 0 ]; then x; fi", "if y; then make | tail; echo $?; fi"):
            with self.subTest(command=command):
                self.assertTrue(self.fires(command))
        for command in ("make | tail; echo x; echo $?", "make | tail; true; echo $?", "make | tail\nls\necho $?",
                        'just test > log; echo "exit=$?"', "make > out.log; echo rc=$?", "make | tail & echo $?",
                        "(make | tail); echo x; echo $?", "make | tail; echo ok; if [ $? -ne 0 ]; then x; fi"):
            with self.subTest(command=command):
                self.assertFalse(self.fires(command))

    def test_pipe_status_says_question_mark_for_a_status_read_in_the_next_command(self) -> None:
        for command in ("(make | tail); echo $?", "make | tail && echo \"rc=$?\"", "make | tail; echo $?"):
            with self.subTest(command=command):
                self.assertIn("pipestatus[1]", self.says("shell-hygiene", "pipe-status", command))

    def test_du_dust_quotes_the_first_path_and_reads_fine_without_one(self) -> None:
        for command, path in (("du -sh /tmp/x", "/tmp/x"), ("du -d 1 ~", "~"), ("sudo du -sh a b", "a"),
                              ("du --max-depth=1 ./src | sort -h", "./src")):
            with self.subTest(command=command):
                self.assertIn(f"`dust -d 1 {path}` for one level", self.says("modern-cli", "du-dust", command))
        for command in ("du", "du -sh", "du -d 1"):
            with self.subTest(command=command):
                self.assertIn("`dust -d 1` for one level", self.says("modern-cli", "du-dust", command))

    def test_ps_grep_cases(self) -> None:
        self.assertIn("off by about 2", self.says("shell-hygiene", "ps-grep-self-match", "ps aux | grep x | wc -l"))
        self.assertIn("off by about 2", self.says("shell-hygiene", "ps-grep-self-match", "ps aux | grep -c x"))
        for command in ("ps aux | grep x | awk '{print $2}'", "ps aux | grep x | cut -c1-9"):
            self.assertIn("confirm the PID", self.says("shell-hygiene", "ps-grep-self-match", command))
        shown = self.says("shell-hygiene", "ps-grep-self-match", "ps aux | grep x")
        self.assertIn("grep -v grep", shown)
        self.assertIn("pgrep -fl", shown)

    def test_ps_grep_is_a_warning_and_the_others_deny(self) -> None:
        actions = {rid: str(rule_of("shell-hygiene", rid).action) for rid in cli.load_preset("shell-hygiene")["rules"]}
        self.assertEqual(actions, {"pipe-status": "deny", "ps-grep-self-match": "warn", "tail-pipe-buffered": "warn",
                                   "http-wait-exact-status": "warn"})


class Installed(AstIsolated):
    def denied(self, command: str, tool: str = "Bash", tool_input: Any = None) -> str | None:
        out = self.hook(command, session=f"s-{tool}-{command}-{tool_input}", tool=tool, tool_input=tool_input)
        return None if out is None else out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_pipe_status_denies_and_the_ps_warning_does_not_block(self) -> None:
        self.cli("preset", "install", "shell-hygiene")
        self.assertIn("[guardrails:pipe-status]", plain(self.denied("make | tail -5 && echo ok") or ""))
        self.assertIsNone(self.denied("set -o pipefail; make | tail -5 && echo ok"))
        warned = self.hook("ps aux | grep cargo", session="warn")
        assert warned is not None
        self.assertIn("additionalContext", warned["hookSpecificOutput"])
        self.assertNotIn("permissionDecision", warned["hookSpecificOutput"])

    def test_tail_pipe_buffered_warns_in_a_monitor_only_and_the_modern_tools_are_not_asked_of_a_remote_host(self) -> None:
        self.cli("preset", "install", "shell-hygiene")
        piped = "tail -n0 -F log | grep -E --line-buffered 'x|y' | cut -c1-220"
        monitor = self.hook(piped, session="m", tool="Monitor")
        assert monitor is not None
        self.assertIn("[guardrails:tail-pipe-buffered]", plain(monitor["hookSpecificOutput"]["additionalContext"]))
        self.assertIn("grep --line-buffered", monitor["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook(piped, session="b", tool="Bash"))
        self.assertIsNone(self.hook("tail -f log | sed -u 's/a/b/'", session="m2", tool="Monitor"))
        self.cli("preset", "install", "modern-cli")
        os.environ["PATH"] = self.stub_path("fd", "rg", "dust", "cargo-nextest")
        for command in ("ssh host find . -name x", "ssh host 'grep -r x .'", "ssh host du -sh /var", "ssh host cargo test"):
            self.assertIsNone(self.warned(command), command)
        self.assertIsNotNone(self.warned("find . -name x"))

    def test_http_wait_exact_status_warns_with_the_curl_retry_alternative(self) -> None:
        self.cli("preset", "install", "shell-hygiene")
        out = self.hook("until [ \"$(curl -s -o /dev/null -w %{http_code} URL)\" = 200 ]; do sleep 5; done")
        assert out is not None
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[guardrails:http-wait-exact-status]", plain(text))
        self.assertIn("-w '%{http_code}'", text)
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def stub_path(self, *names: str) -> str:
        folder = self.tmp / ("bin-" + "-".join(names))
        folder.mkdir()
        for name in names:
            (folder / name).write_text("#!/bin/sh\n")
            (folder / name).chmod(0o755)
        return str(folder)

    def warned(self, command: str) -> str | None:
        out = self.hook(command, session=f"w-{command}-{os.environ['PATH']}")
        if out is None:
            return None
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        return out["hookSpecificOutput"]["additionalContext"]

    def test_nextest_and_dust_rules_need_their_tools(self) -> None:
        self.cli("preset", "install", "modern-cli")
        os.environ["PATH"] = self.stub_path("fd")
        self.assertIsNone(self.denied("cargo test"))
        self.assertIsNone(self.denied("du -sh ."))
        os.environ["PATH"] = self.stub_path("cargo-nextest", "dust")
        self.assertIn("[guardrails:cargo-nextest]", plain(self.warned("cargo test -p x") or ""))
        self.assertIn("[guardrails:du-dust]", plain(self.warned("du -sh .") or ""))
        self.assertIsNone(self.warned("cargo test --doc"))
        self.assertIsNotNone(self.warned("du -sk ."))
        self.assertIsNone(self.warned("du -sk ."))
