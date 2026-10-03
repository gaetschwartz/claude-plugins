"""Run a function in a forked child that is killed at a deadline.

A native call into ast-grep holds the GIL, so neither a signal nor a watchdog thread can interrupt it; a child process
can be killed whatever it is doing, and the parent always gets control back. The answer travels as JSON, never pickle:
the child parses hostile input and the parent must not execute what it sends back.
"""

from __future__ import annotations

import json
import os
import resource
import select
import signal
import sys
import time
from collections.abc import Callable
from enum import StrEnum
from typing import NamedTuple

CHUNK = 1 << 16


class Outcome(StrEnum):
    DONE = "done"
    TIMEOUT = "timeout"
    CRASHED = "crashed"
    GARBLED = "garbled"


class Result(NamedTuple):
    outcome: Outcome
    payload: object = None


def child_main(work: Callable[[], object], tx: int, seconds: float) -> None:
    """In the child: cap CPU time (so an orphan cannot spin on), compute, send the answer, never print a traceback."""
    try:
        cap = int(seconds) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cap, cap))
        pending = memoryview(json.dumps(work()).encode())
        while pending:
            pending = pending[os.write(tx, pending):]
    except BaseException:  # noqa: BLE001
        os._exit(1)
    os._exit(0)


def drain(fd: int, seconds: float) -> bytes | None:
    """Everything written to `fd` until it closes; None when `seconds` pass first."""
    deadline = time.monotonic() + seconds
    chunks: list[bytes] = []
    while (left := deadline - time.monotonic()) > 0 and select.select([fd], [], [], left)[0]:
        if not (chunk := os.read(fd, CHUNK)):
            return b"".join(chunks)
        chunks.append(chunk)
    return None


def call(work: Callable[[], object], seconds: float, after_fork: Callable[[], None] | None = None) -> Result:
    """What `work` returns (a JSON value), or TIMEOUT after `seconds`, CRASHED when the child dies, GARBLED for an
    unreadable answer.

    `after_fork` runs in the parent once the child exists, before it waits."""
    sys.stdout.flush()
    sys.stderr.flush()
    rx, tx = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rx)
        child_main(work, tx, seconds)
    os.close(tx)
    answer = None
    try:
        if after_fork is not None:
            after_fork()
        answer = drain(rx, seconds)
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(rx)
    if answer is None:
        return Result(Outcome.TIMEOUT)
    if not answer:
        return Result(Outcome.CRASHED)
    try:
        return Result(Outcome.DONE, json.loads(answer))
    except ValueError:
        return Result(Outcome.GARBLED)
