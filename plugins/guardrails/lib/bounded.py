"""Run a function in a forked child that is killed at a deadline.

A native call into ast-grep holds the GIL, so neither a signal nor a watchdog thread can interrupt it; a child
process can be killed whatever it is doing, and the parent always gets control back.
"""

from __future__ import annotations

import os
import resource
import select
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    DONE = "done"
    TIMEOUT = "timeout"
    CRASHED = "crashed"
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class Result:
    outcome: Outcome
    payload: str = ""


def child_main(work: Callable[[], str], writer: int, seconds: float) -> None:
    """In the child: cap CPU time (so an orphan cannot spin on), compute, write the answer, leave without cleanup."""
    status = 1
    try:
        cap = int(seconds) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cap, cap))
        data = work().encode()
        while data:
            data = data[os.write(writer, data):]
        status = 0
    finally:
        os._exit(status)


def call(work: Callable[[], str], seconds: float) -> Result:
    """The string `work` returns, or TIMEOUT when it takes longer than `seconds`, CRASHED when the child dies."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(reader)
        child_main(work, writer, seconds)
    os.close(writer)
    end = time.monotonic() + seconds
    chunks: list[bytes] = []
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0 or not select.select([reader], [], [], left)[0]:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                return Result(Outcome.TIMEOUT)
            chunk = os.read(reader, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(reader)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        return Result(Outcome.CRASHED)
    return Result(Outcome.DONE, b"".join(chunks).decode())
