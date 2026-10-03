from __future__ import annotations  # noqa: I001

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest import mock

from helpers import HOOKS, AstIsolated, RealRuntime

import guard
import telemetry

SECRETS = ("hunter2", "example.com", "/Users/gaetan/private", "ключ", "token=abc", "1234", "sess-secret", "No kill.",
           "Careful.", "No curl.")
CORPUS = ["kill -9 1234", "curl https://user:hunter2@example.com/x?token=abc", "ls /Users/gaetan/private",
          "echo 'ünïcode ключ'", "git status", "bash -c 'kill 1234'"]
HOSTILE = ["x" * 200, "evil'); DROP TABLE rule_hours;--", "\x1b[31mred", "@parse", "ünï"]
RULES = {"no-kill": {"match": {"program": "kill"}, "message": "No kill."},
         "warn-ls": {"match": {"program": "ls"}, "action": "warn", "message": "Careful."},
         "no-curl": {"match": {"program": "curl"}, "message": "No curl."}}


class Fixture(AstIsolated):
    silent_telemetry = False

    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": RULES})
        self.enterContext(warnings.catch_warnings(action="ignore", category=DeprecationWarning))
        patch = mock.patch.object(telemetry, "JOIN_SECONDS", 5.0)
        patch.start()
        self.addCleanup(patch.stop)

    @property
    def db(self) -> str:
        return str(self.data / telemetry.DB)

    def run_hook(self, command: str, session: str = "sess-secret") -> str:
        payload = {"session_id": session, "cwd": str(self.proj), "tool_name": "Bash", "tool_input": {"command": command}}
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            guard.hook(json.dumps(payload))
        for writer in threading.enumerate()[1:]:
            writer.join()
        return out.getvalue()

    def table(self) -> dict[str, tuple[int, ...]]:
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return {rule: (*counts,) for rule, *counts in conn.execute(
                "SELECT rule, sum(deny), sum(warn), sum(pass), sum(suspended) FROM rule_hours GROUP BY rule")}


class Counted(Fixture):
    def test_each_rule_counts_its_outcome_and_the_operational_rows_count_calls(self) -> None:
        for command in CORPUS:
            self.run_hook(command)
        self.assertEqual(self.table(), {"no-kill": (2, 0, 4, 0), "no-curl": (1, 0, 5, 0), "warn-ls": (0, 1, 5, 0),
                                        "@parse": (0, 0, 6, 0), "@hook": (0, 0, 6, 0)})

    def test_a_mode_suspends_and_a_disabled_rule_writes_nothing(self) -> None:
        self.put(self.gpath, {"rules": {**RULES, "no-kill": {**RULES["no-kill"], "modes": ["m"]},
                                        "no-curl": {**RULES["no-curl"], "enabled": False}},
                              "modes": {"m": {"active": True}}})
        self.run_hook("kill 1")
        self.assertEqual(self.table()["no-kill"], (0, 0, 0, 1))
        self.assertNotIn("no-curl", self.table())

    def test_only_rule_ids_integers_and_hours_reach_the_file_even_for_hostile_ids(self) -> None:
        self.put(self.gpath, {"rules": {**RULES, **{rid: {"match": {"program": "kill"}, "message": "No kill."}
                                                    for rid in HOSTILE}}})
        for command in CORPUS:
            self.run_hook(command)
        raw = b"".join(path.read_bytes() for path in sorted(self.data.glob("telemetry.db*")))
        self.assertEqual([s for s in SECRETS if s.encode() in raw], [])
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            ids = {rule for (rule,) in conn.execute("SELECT rule FROM rule_hours")}
            kinds = {t for row in conn.execute("SELECT * FROM rule_hours") for t in map(type, row[1:])}
        self.assertEqual(kinds, {int})
        self.assertTrue(all(len(i) <= 64 and i.isprintable() and (i[0] != "@" or i in ("@parse", "@hook")) for i in ids))
        self.assertLessEqual({"_parse", "x" * 64, "evil'); DROP TABLE rule_hours;--", "n", "@hook"}, ids)

    def test_the_checker_forks_first_then_the_thread_starts_and_writes_only_after_the_output_is_flushed(self) -> None:
        events: list[str] = []
        real_fork, real_start, real_upsert = os.fork, telemetry.Recorder.start, telemetry.upsert

        class Tape(io.StringIO):
            def write(self, text: str) -> int:
                events.append("stdout")
                return super().write(text)

            def flush(self) -> None:
                events.append("flush")

        def fork() -> int:
            events.append("fork")
            return real_fork()

        def start(self: telemetry.Recorder) -> None:
            events.append("start")
            real_start(self)

        def upsert(*args: Any) -> None:
            events.append("write")
            real_upsert(*args)

        payload = {"session_id": "s", "cwd": str(self.proj), "tool_name": "Bash", "tool_input": {"command": "kill 1"}}
        with (mock.patch.object(sys, "stdout", Tape()), mock.patch.object(os, "fork", fork),
              mock.patch.object(telemetry.Recorder, "start", start), mock.patch.object(telemetry, "upsert", upsert)):
            guard.hook(json.dumps(payload))
        self.assertLess(events.index("fork"), events.index("start"))
        self.assertLess(events.index("start"), events.index("stdout"))
        self.assertEqual([e for e in events[events.index("fork"):] if e in ("stdout", "flush", "write")],
                         ["stdout", "flush", "write"])

    def test_prune_drops_only_rows_older_than_a_year(self) -> None:
        self.run_hook("kill 1")
        with contextlib.closing(telemetry.connect(self.data / telemetry.DB)) as conn:
            telemetry.upsert(conn, telemetry.hour_now() - telemetry.KEEP_HOURS - 1, [telemetry.Sample("old", "deny", 1)])
        telemetry.prune(self.data)
        self.assertEqual(set(self.table()) & {"old", "no-kill"}, {"no-kill"})


class Harmless(Fixture):
    """Whatever goes wrong with the database, the answer is the one a run without telemetry gives."""

    def baseline(self, command: str, session: str) -> str:
        with mock.patch.object(telemetry.Recorder, "start"), mock.patch.object(telemetry.Recorder, "finish"):
            return self.run_hook(command, session)

    def test_every_failure_drops_the_sample_silently(self) -> None:
        self.run_hook("ls")
        hold = sqlite3.connect(self.db, isolation_level=None)
        hold.execute("BEGIN IMMEDIATE")
        self.addCleanup(hold.close)

        def disk_full(*args: Any) -> None:
            raise sqlite3.OperationalError("database or disk is full")

        for name, scope in {
            "locked": contextlib.nullcontext(),
            "no sqlite3": mock.patch.dict(sys.modules, {"sqlite3": None}),
            "disk full": mock.patch.object(telemetry, "upsert", disk_full),
            "thread crash": mock.patch.object(telemetry, "connect", side_effect=RuntimeError("boom")),
        }.items():
            with self.subTest(name), scope:
                for command in ("kill 1", "ls", "echo hi"):
                    self.assertEqual(self.run_hook(command, f"{name}{command}"), self.baseline(command, f"b{name}{command}"))

    def test_a_corrupt_database_is_set_aside_and_recreated_and_an_unwritable_one_is_skipped(self) -> None:
        (self.data / telemetry.DB).write_bytes(b"not a database" * 100)
        expected = self.baseline("kill 1", "base")
        self.assertEqual(self.run_hook("kill 1"), expected)
        self.assertTrue((self.data / "telemetry.db.corrupt").exists())
        self.assertEqual(self.table()["no-kill"], (1, 0, 0, 0))
        if os.geteuid() != 0:
            self.data.chmod(0o500)
            self.addCleanup(self.data.chmod, 0o700)
            self.assertEqual(self.run_hook("kill 1", "ro"), expected)


class Parallel(RealRuntime):
    def test_thirty_hooks_at_once_write_whole_calls_or_nothing(self) -> None:
        self.put(self.gpath, {"rules": RULES})
        payload = json.dumps({"session_id": "p", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "echo hi"}})

        def call(_: int) -> str:
            return subprocess.run(["sh", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True, text=True,
                                  check=False, env=dict(os.environ)).stderr

        with ThreadPoolExecutor(30) as pool:
            self.assertEqual(set(pool.map(call, range(30))), {""})
        time.sleep(0.1)
        with contextlib.closing(sqlite3.connect(self.data / telemetry.DB)) as conn:
            found = dict(conn.execute("SELECT rule, sum(pass) FROM rule_hours GROUP BY rule"))
        self.assertEqual(set(found), {"@hook", "@parse", "no-kill", "no-curl", "warn-ls"})
        self.assertEqual(len(set(found.values())), 1)
        self.assertGreater(found["@hook"], 0)


if __name__ == "__main__":
    unittest.main()
