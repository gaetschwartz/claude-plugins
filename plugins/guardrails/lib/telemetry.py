"""Per-rule hook counters in a local SQLite file: counts and timings keyed by rule id, never anything about a command.

Runs on the host's Python too (SessionStart prunes): standard library only, Python 3.9 syntax.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

import bootstrap

if TYPE_CHECKING:
    import sqlite3

Outcome = Literal["deny", "warn", "pass", "suspended"]

DB = "telemetry.db"
KEEP_HOURS = 365 * 24
MAX_ID = 64
BUSY_SECONDS = JOIN_SECONDS = 0.02
SETTLE_SECONDS = 0.05
IDLE_SECONDS = 15.0
SCHEMA = """CREATE TABLE IF NOT EXISTS rule_hours (rule TEXT, hour INTEGER, deny INTEGER, warn INTEGER, pass INTEGER,
    suspended INTEGER, eval_us INTEGER, eval_max_us INTEGER, PRIMARY KEY (rule, hour)) WITHOUT ROWID"""
UPSERT = """INSERT INTO rule_hours VALUES (?1, ?4, ?2 = 'deny', ?2 = 'warn', ?2 = 'pass', ?2 = 'suspended', ?3, ?3)
    ON CONFLICT (rule, hour) DO UPDATE SET deny = deny + excluded.deny, warn = warn + excluded.warn,
    pass = pass + excluded.pass, suspended = suspended + excluded.suspended, eval_us = eval_us + excluded.eval_us,
    eval_max_us = max(eval_max_us, excluded.eval_max_us)"""


class Sample(NamedTuple):
    rule: str
    outcome: Outcome
    micros: int


class Row(NamedTuple):
    rule: str
    hour: int
    deny: int
    warn: int
    passed: int
    suspended: int
    micros: int
    peak: int


def hour_now() -> int:
    return int(time.time()) // 3600


def rule_id(raw: str) -> str:
    """A rule id is user text: printable ASCII only, bounded, and never in the reserved `@` namespace."""
    text = bootstrap.sanitised(raw, MAX_ID) or "?"
    return "_" + text[1:] if text.startswith("@") else text


def connect(path: Path) -> sqlite3.Connection:
    """An open database with the table; a file that is not a database is set aside once and recreated."""
    import sqlite3

    for attempt in (1, 2):
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        conn = sqlite3.connect(path, timeout=BUSY_SECONDS, isolation_level=None, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute(SCHEMA)
            return conn
        except sqlite3.DatabaseError as exc:
            conn.close()
            if isinstance(exc, sqlite3.OperationalError) or attempt == 2:
                raise
            for suffix in ("-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
            path.replace(path.with_name(DB + ".corrupt"))
    raise AssertionError("unreachable")


def upsert(conn: sqlite3.Connection, hour: int, samples: list[Sample]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(UPSERT, [(*sample, hour) for sample in samples])
    conn.execute("COMMIT")


LIVE: set[Recorder] = set()


class Recorder:
    """One hook call's samples, written by a thread that opens the database while the checker child evaluates."""

    def __init__(self, data: Path) -> None:
        self.path = data / DB
        self.samples: list[Sample] = []
        self.thread: threading.Thread | None = None
        self.ready, self.go, self.done = threading.Event(), threading.Event(), threading.Event()

    def start(self) -> None:
        """Only after the checker is forked: a thread alive at fork time can leave a lock held in the child."""
        with contextlib.suppress(RuntimeError):
            if self.thread is None:
                LIVE.add(self)
                self.thread = threading.Thread(target=self.run, daemon=True)
                self.thread.start()

    def run(self) -> None:
        conn = None
        try:
            conn = connect(self.path)
            self.ready.set()
            if self.go.wait(IDLE_SECONDS):
                upsert(conn, hour_now(), self.samples)
        except Exception:  # noqa: BLE001
            return  # telemetry is disposable: any failure drops the sample
        finally:
            self.ready.set()
            self.done.set()
            LIVE.discard(self)
            if conn is not None:
                conn.close()

    def finish(self) -> None:
        """After the answer is out: hand the samples over and give the commit a moment. Never raises."""
        try:
            self.start()
            self.go.set()
            self.done.wait(JOIN_SECONDS)
        except Exception:  # noqa: BLE001
            return


def settle() -> None:
    for recorder in tuple(LIVE):
        recorder.ready.wait(SETTLE_SECONDS)


os.register_at_fork(before=settle)


def prune(data: Path) -> None:
    if not (data / DB).exists():
        return
    try:
        with contextlib.closing(connect(data / DB)) as conn:
            conn.execute("DELETE FROM rule_hours WHERE hour < ?", (hour_now() - KEEP_HOURS,))
    except Exception:  # noqa: BLE001
        return


def read(path: Path, since: int, rule: str | None = None) -> list[Row]:
    """The hour rows from `since` on (for one rule when given); an unreadable database raises OSError."""
    import sqlite3

    if not path.exists():
        return []
    query = "SELECT * FROM rule_hours WHERE hour >= ?" + ("" if rule is None else " AND rule = ?")
    try:
        with contextlib.closing(connect(path)) as conn:
            return [Row(*found) for found in conn.execute(query, (since,) if rule is None else (since, rule))]
    except sqlite3.Error as exc:
        raise OSError(f"cannot read the telemetry database: {exc}") from exc


def reset(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
