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
                    policy.Rule.from_json(rule, preset.get("matchers"))
                    for mode in policy.json_modes(rule):
                        self.assertIn(mode, preset.get("modes", {}))
            used = {m for rule in preset["rules"].values() for m in policy.Rule.from_json(rule, preset.get("matchers")).matchers}
            self.assertEqual(sorted(preset.get("matchers", {})), sorted(used), f"{name}: every matcher is used")

    def test_no_regex_is_written_twice_or_grows_big(self) -> None:
        found: list[str] = []

        def walk(node: Any) -> None:
            for key, value in (node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()):
                if key in ("regex", "args") and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)

        for name in cli.preset_names():
            walk(cli.load_preset(name)["rules"])
            walk(cli.load_preset(name).get("matchers", {}))
        self.assertEqual(len(found), len(set(found)), "a regex written twice belongs in a matcher")
        self.assertEqual([r for r in found if len(r) > 20], [], "a long regex is a table; use atoms")

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
    doc = cli.load_preset(preset)
    return policy.Rule.from_json(doc["rules"][rid], doc.get("matchers"))


class Examples(AstIsolated):
    def test_every_rule_of_the_tool_presets_has_catch_and_pass_examples_that_hold(self) -> None:
        for name in ("modern-cli", "shell-hygiene"):
            preset = cli.load_preset(name)
            self.assertEqual(sorted(preset["examples"]), sorted(preset["rules"]))
            for rid, example in preset["examples"].items():
                rule = rule_of(name, rid)
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

    def test_kill_9_catches_every_spelling_of_sigkill_and_nothing_else(self) -> None:
        rule = rule_of("process-safety", "kill-9")
        for command in ("kill -9 1", "kill -KILL 1 2", "kill -SIGKILL 1", "kill -s 9 1", "kill -s KILL 1", "kill -s SIGKILL 1",
                        "sudo kill -9 1", "/bin/kill -9 1", "ps | xargs kill -9", "A=1 kill -9 1", "x && kill -9 $(pgrep y)"):
            with self.subTest(command=command):
                self.assertIsNotNone(matching.evaluate(command, {"r": rule}).kinds["r"])
        for command in ("kill 1", "kill -TERM 1", "kill -15 1", "kill -s TERM 1", "echo kill -9", "kill -l 9", "kill 1 -9",
                        "man kill", "kill -90 1"):
            with self.subTest(command=command):
                self.assertIsNone(matching.evaluate(command, {"r": rule}).kinds["r"])

    def kinds_of(self, preset: str, rid: str, commands: list[str]) -> dict[str, bool]:
        rule = rule_of(preset, rid)
        return {c: matching.evaluate(c, {"r": rule}).kinds["r"] is not None for c in commands}

    def test_cargo_nextest_reads_the_first_operand_after_toolchain_and_global_flags(self) -> None:
        caught = ["cargo test", "cargo +stable test", "cargo +nightly test -p x", "cargo -q test", "cargo --locked test",
                  "cargo -q --locked test", ">/dev/null cargo test", "RUST_BACKTRACE=1 cargo +nightly test", "sudo cargo test -q",
                  "cd x && cargo test 2>&1 | tail -3", "/usr/bin/cargo test", "cargo test --workspace -- --nocapture"]
        passed = ["cargo test --doc", "cargo +stable test --doc", "cargo nextest run test", "cargo build --features test",
                  "cargo new test", "echo cargo test", "man cargo-test", "ssh host cargo test", "cargo build --locked --features test",
                  "git grep cargo test"]
        self.assertEqual(self.kinds_of("modern-cli", "cargo-nextest", [*caught, *passed]),
                         {**dict.fromkeys(caught, True), **dict.fromkeys(passed, False)})

    def test_http_wait_warns_on_equality_with_a_2xx_only(self) -> None:
        code = "$(curl -s -o /dev/null -w %{http_code} URL)"
        caught = [f'until [ "{code}" = 200 ]; do sleep 5; done', f'until [ "{code}" -eq 200 ]; do sleep 5; done',
                  f"until [[ {code} == 204 ]]; do sleep 1; done", f'until [ "{code}" = "200" ]; do sleep 1; done',
                  f'for i in 1 2 3; do c={code}; [ "$c" = 200 ] && break; sleep 5; done',
                  f'while :; do c={code}; case $c in 200|302) break;; esac; sleep 2; done']
        passed = [f'until [ "{code}" != 200 ]; do sleep 5; done', f'until [ "{code}" -ne 200 ]; do sleep 5; done',
                  f'until [ "{code}" -ge 500 ]; do sleep 5; done', f'until [ "{code}" -gt 200 ]; do sleep 5; done',
                  f'until [ "{code}" -lt 300 ]; do sleep 5; done', f'while [ "{code}" -eq 200 ]; do sleep 1; done',
                  f"while [ {code} -le 200 ]; do sleep 1; done", f'for i in 1 2; do [ "{code}" -ne 503 ] && break; done']
        self.assertEqual(self.kinds_of("shell-hygiene", "http-wait-exact-status", [*caught, *passed]),
                         {**dict.fromkeys(caught, True), **dict.fromkeys(passed, False)})

    def test_tail_pipe_buffered_warns_for_awk_unless_its_program_flushes(self) -> None:
        kinds = self.kinds_of("shell-hygiene", "tail-pipe-buffered", ["tail -f log | awk '{print $1}'",
                                                                      "tail -f log | awk '{print; fflush()}'",
                                                                      "tail -f log | sed 's/a/b/'", "tail -f log | uniq"])
        self.assertEqual(list(kinds.values()), [True, False, True, True])

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
        head = self.says("shell-hygiene", "pipe-status", "make | head -5 || echo failed")
        self.assertIn("SIGPIPE", head)
        self.assertNotIn("pipestatus", head)
        default = self.says("shell-hygiene", "pipe-status", "make | tail -5 || echo failed")
        self.assertIn("set -o pipefail", default)
        self.assertNotIn("SIGPIPE", default)

    def test_pipe_status_names_the_last_command_of_the_whole_pipeline(self) -> None:
        for command, last in (("make | tail -5 || echo ok", "tail"), ("a | b | sort || echo ok", "sort"),
                              ("a | b | c | wc -l || echo ok", "wc"), ("make | cat > o 2>&1 || echo ok", "cat"),
                              ("make | /usr/bin/sort || echo ok", "/usr/bin/sort"),
                              ("make | tail | wc || echo ok", "wc"), ("make | sort -u | tee out || echo ok", "tee"),
                              ("make 2>&1 | grep err | tail -5 || echo ok", "tail"),
                              ("grep pat f | wc -l || echo found", "wc")):
            with self.subTest(command=command):
                self.assertIn(f"last command's (`{last}`)", self.says("shell-hygiene", "pipe-status", command))

    def fires(self, command: str) -> bool:
        return matching.evaluate(command, {"r": rule_of("shell-hygiene", "pipe-status")}).kinds["r"] is not None

    def chain(self, command: str) -> bool:
        return matching.evaluate(command, {"r": rule_of("shell-hygiene", "pipe-status-chain")}).kinds["r"] is not None

    def test_and_warns_whatever_follows_and_only_the_other_consumers_deny(self) -> None:
        for follower in ("echo ok", "echo ---", 'echo "=====PYPROJECT====="', "echo", "printf '\\n'", "git status", "git push",
                         "git commit -m x", "rm -rf build", "cp a b", "cargo publish", "git push 2>&1 | tail -2", "./run.sh",
                         "./target/debug/x4c", "bash script.sh", "python3 x.py", "python3 -m pytest", "bash -c x", "ls",
                         "cargo build", "make | tail", "true", "sudo systemctl stop x", "timeout 5 git push", "sudo git push"):
            with self.subTest(follower=follower):
                command = f"make | tail && {follower}"
                self.assertEqual((self.chain(command), self.fires(command)), (True, False))
        for command in ("make | tail || echo failed", "! make | tail", "if make | tail; then ls; fi",
                        "while make | wc -l; do sleep 1; done", "make | tail; echo $?"):
            with self.subTest(command=command):
                self.assertEqual((self.chain(command), self.fires(command)), (False, True))

    def test_the_chain_is_a_warning_and_no_rule_names_a_follower(self) -> None:
        chain = rule_of("shell-hygiene", "pipe-status-chain")
        self.assertEqual((chain.action, chain.retry), (policy.Action.WARN, policy.Retry.NONE))
        self.assertEqual(sorted(cli.load_preset("shell-hygiene")["rules"]), ["http-wait-exact-status", "pipe-status",
                                                                              "pipe-status-chain", "ps-grep-self-match",
                                                                              "tail-pipe-buffered"])

    def test_a_longer_chain_is_warned_once_for_each_masked_pipeline(self) -> None:
        self.assertTrue(self.chain("make | tail && echo ---"))
        self.assertTrue(self.chain("make | tail && ls && git push"))
        self.assertTrue(self.chain("x && make | tail && ls && make | head && git push"))

    def test_the_warning_sees_the_same_nesting_as_the_denial(self) -> None:
        for template in ("{} && ls", "x && {} && ls", "x && y && {} && ls", "x && {} >log && ls", "{} || echo failed",
                         "{} && git push", "x && {} && git push"):
            for pipeline in ("make | tail", "(make | tail)", "{ make | tail; }", "make | sort | cat", "make | tail >log"):
                command = template.format(pipeline)
                with self.subTest(command=command):
                    self.assertEqual((self.chain(command), self.fires(command)), ("&&" in template, "||" in template))

    def test_pipe_status_ignores_a_deliberately_discarded_status(self) -> None:
        for command in ("make | tail || true", "make | tail || :", "make | tail >log 2>&1 || true", "a | b | sort || true",
                        "x && make | tail || true", "(make | tail) || true", "{ make | tail; } || true",
                        "make | tail || true; echo $?", "make | tail || true && echo ok",
                        "if make | tail || true; then y; fi"):
            with self.subTest(command=command):
                self.assertFalse(self.fires(command))
        for command in ("make | tail || false", "make | tail || echo failed", "make | tail || echo ok && echo failed"):
            with self.subTest(command=command):
                self.assertTrue(self.fires(command))
        self.assertFalse(self.fires("make | tail && echo ok || echo failed"))

    def test_pipe_status_reads_question_mark_only_in_the_next_command(self) -> None:
        for command in ("make | tail; echo $?", "make | tail\necho $?", "make | tail;\necho $?",
                        "(make | tail); echo $?", "x && { y; make | tail; }; echo $?", "{ make | tail; }; echo $?", "make | tail >o; echo $?",
                        "make | tail # note\necho $?", 'make | tail; echo "rc=$?"', "make | tail; [ $? -eq 0 ] && echo ok",
                        "make | tail; if [ $? -ne 0 ]; then x; fi", "if y; then make | tail; echo $?; fi"):
            with self.subTest(command=command):
                self.assertTrue(self.fires(command))
        for command in ("make | tail; echo x; echo $?", "make | tail; true; echo $?", "make | tail\nls\necho $?",
                        'just test > log; echo "exit=$?"', "make > out.log; echo rc=$?",
                        "(make | tail); echo x; echo $?", "make | tail; echo ok; if [ $? -ne 0 ]; then x; fi",
                        "make | tail && echo $?", "make | tail && (sleep 1; echo $?)", "for f in $(ls | sort); do x; echo $?; done",
                        "make | tail & echo $?"):
            with self.subTest(command=command):
                self.assertFalse(self.fires(command))

    def test_pipe_status_says_question_mark_for_a_status_read_in_the_next_command(self) -> None:
        for command in ("(make | tail); echo $?", "make | tail; echo \"rc=$?\"", "make | tail; echo $?"):
            with self.subTest(command=command):
                self.assertIn("pipestatus[1]", self.says("shell-hygiene", "pipe-status", command))

    def test_pipefail_and_a_pipestatus_read_after_the_pipeline_exempt_every_rule(self) -> None:
        for command in ("set -o pipefail; {}", "set -eo pipefail; {}", "setopt pipefail; {}", "x; set -o pipefail; y; {}",
                        "{}; echo ${{PIPESTATUS[0]}}", "{}; echo ${{pipestatus[1]}}"):
            for tail in ("make | tail && ls", "make | tail && git push", "make | tail || echo failed", "make | tail; echo $?"):
                text = command.format(tail)
                with self.subTest(command=text):
                    self.assertEqual((self.chain(text), self.fires(text)), (False, False))
        self.assertTrue(self.chain("set +o pipefail; make | tail && ls"))
        self.assertTrue(self.fires("set -o errexit; make | tail || echo failed"))
        self.assertTrue(self.chain("echo ${PIPESTATUS[0]}; make | tail && ls"))
        self.assertTrue(self.chain("echo $PIPESTATUS; make | tail && git push"))

    def test_the_warning_says_what_and_tests_and_names_the_last_stage(self) -> None:
        text = self.says("shell-hygiene", "pipe-status-chain", "make | sort | cat && ls")
        self.assertIn("`cat`", text)
        self.assertIn("set -o pipefail", text)

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
        self.assertEqual(actions, {"pipe-status": "deny", "pipe-status-chain": "warn", "ps-grep-self-match": "warn",
                                   "tail-pipe-buffered": "warn", "http-wait-exact-status": "warn"})


class Installed(AstIsolated):
    def denied(self, command: str, tool: str = "Bash", tool_input: Any = None) -> str | None:
        out = self.hook(command, session=f"s-{tool}-{command}-{tool_input}", tool=tool, tool_input=tool_input)
        return None if out is None else out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_pipe_status_denies_and_the_ps_warning_does_not_block(self) -> None:
        self.cli("preset", "install", "shell-hygiene")
        self.assertIn("[guardrails:pipe-status]", plain(self.denied("make | tail -5 || echo failed") or ""))
        self.assertIsNone(self.denied("set -o pipefail; make | tail -5 || echo failed"))
        pushed = self.hook("make | tail -5 && git push", session="push")
        assert pushed is not None
        self.assertNotIn("permissionDecision", pushed["hookSpecificOutput"])
        chained = self.hook("make | tail -5 && echo ok", session="chain")
        assert chained is not None
        self.assertIn("[guardrails:pipe-status-chain]", plain(chained["hookSpecificOutput"]["additionalContext"]))
        self.assertNotIn("permissionDecision", chained["hookSpecificOutput"])
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
