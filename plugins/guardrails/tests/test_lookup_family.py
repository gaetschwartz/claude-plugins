from __future__ import annotations  # noqa: I001

import json

from helpers import ROOT, AstIsolated

import matching
import policy

DRAFT = json.loads((ROOT / "tests" / "lookup_family.extended.json").read_text())

STILL_DENIED = [
    "kill $(pgrep -f x)", "pgrep -f x | xargs kill", "ps aux | grep x | xargs kill", "kill $(lsof -ti:80)",
    "kill $(pidof vite)", "lsof -ti:3000 | xargs kill -9", "echo $(pgrep -f x)", "pgrep -f x | head -1",
]

ASSIGNED_THEN_KILLED = [
    "PID=$(ps aux | grep X | awk '{print $2}'); kill $PID",
    "PID=$(lsof -ti:80); kill $PID",
    "PID=$(pidof foo); kill -9 $PID",
    "PID=$(ps aux | grep X | awk '{print $2}') && kill \"$PID\"",
    "PID=$(ps -C node -o pid=); kill ${PID}",
    "PID=$(ps aux | rg vite | awk '{print $2}'); if [ -n \"$PID\" ]; then kill $PID; fi",
]
LOOPED = [
    "for p in $(ps aux | grep X | awk '{print $2}'); do kill $p; done",
    "for p in $(lsof -ti:3000); do kill -9 $p; done",
    "for p in $(pidof foo); do kill $p; done",
    "ps aux | grep X | awk '{print $2}' | while read p; do kill $p; done",
    "lsof -ti:80 | while read p; do kill $p; done",
    "ps -C node -o pid= | while read p; do kill -9 $p; done",
]
GENERATED_SCRIPT = [
    "ps aux | grep X | awk '{print \"kill \" $2}' | sh",
    "ps aux | grep X | awk '{print \"kill -9 \" $2}' | bash",
    "ps aux | grep X | awk '{print \"kill \" $2}' | zsh",
    "ps aux | grep X | awk '{print \"kill \" $2}' | sudo sh",
    "lsof -ti:80 | sed 's/^/kill /' | sh",
]

PASSES = [
    "for pid in 8357 73298; do info=$(ps -p $pid -o pid=,comm=); case \"$info\" in *name*) kill -9 $pid;; esac; done",
    "for pid in 8357; do info=$(ps -p $pid -o comm=); [ \"$info\" = node ] && kill $pid; done",
    "info=$(ps -p 8357 -o comm=); case \"$info\" in *node*) kill 8357;; esac",
    "PID=$(ps -p 8357 -o ppid=); kill $PID",
    "kill 12345", "kill $!", "kill %1", "kill $(cat pid.txt)", "PID=$(cat pid.txt); kill $PID",
    "for p in $(cat pids.txt); do kill $p; done", "cat pids.txt | while read p; do kill $p; done",
    "sleep 100 & PID=$!; kill $PID", "ps -p 123 -o comm=", "ps -o pid,comm -p 123", "ps aux | grep -v grep",
    "ps aux | grep X | awk '{print $2}'", "PID=$(ps aux | grep X | awk '{print $2}'); echo $PID",
    "echo \"kill 5\" | sh", "pgrep -f x", "lsof -ti:80", "kill -0 $$",
    "P=$(ps -Ao pid,command | awk '/[b]ench.py/ {print $1}'); while kill -0 $P 2>/dev/null; do sleep 20; done",
    "PID=$(lsof -ti:80); kill -0 $PID && echo alive",
]


class LookupFamily(AstIsolated):
    def kind(self, command: str, rule: policy.Rule | None = None) -> str | None:
        return matching.evaluate(command, {"r": rule or self.rule()}).kinds["r"]

    def assert_denied(self, commands: list[str]) -> None:
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(self.kind(command))

    def rule(self) -> policy.Rule:
        return policy.Rule.from_json(DRAFT)

    def test_the_draft_keeps_the_live_alternatives_and_adds_four(self) -> None:
        self.assertEqual(set(DRAFT), {"action", "description", "match", "message", "messageShort", "messages"})
        self.assertEqual(len(DRAFT["match"]["any"]), 8)

    def test_existing_catches_stay_denied(self) -> None:
        self.assert_denied(STILL_DENIED)

    def test_a_pid_assigned_from_an_imprecise_lookup_then_killed_is_denied(self) -> None:
        self.assert_denied(ASSIGNED_THEN_KILLED)

    def test_a_loop_over_an_imprecise_lookup_is_denied(self) -> None:
        self.assert_denied(LOOPED)

    def test_a_generated_kill_script_piped_to_a_shell_is_denied(self) -> None:
        self.assert_denied(GENERATED_SCRIPT)

    def test_verify_then_kill_and_precise_forms_pass(self) -> None:
        for command in PASSES:
            with self.subTest(command=command):
                self.assertIsNone(self.kind(command))

    def test_the_new_shapes_pass_today_and_are_denied_by_the_draft(self) -> None:
        today = policy.Rule.from_json({**DRAFT, "match": {"any": DRAFT["match"]["any"][:4]}})
        for command in (*ASSIGNED_THEN_KILLED, *LOOPED, *GENERATED_SCRIPT):
            with self.subTest(command=command):
                self.assertIsNone(self.kind(command, today))
