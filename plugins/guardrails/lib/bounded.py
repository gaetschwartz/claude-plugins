"""Run a function in a forked child that is killed at a deadline.

A native call into ast-grep holds the GIL, so neither a signal nor a watchdog thread can interrupt it; a child process
can be killed whatever it is doing, and the parent always gets control back.
"""

from __future__ import annotations

import multiprocessing
import os
import resource
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Connection


class Outcome(StrEnum):
    DONE = "done"
    TIMEOUT = "timeout"
    CRASHED = "crashed"
    GARBLED = "garbled"


@dataclass(frozen=True, slots=True)
class Result[T]:
    outcome: Outcome
    payload: T | None = None


def child_main[T](work: Callable[[], T], tx: Connection, seconds: float) -> None:
    """In the child: cap CPU time (so an orphan cannot spin on), compute, send the answer, never print a traceback."""
    try:
        cap = int(seconds) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cap, cap))
        tx.send(work())
    except BaseException:  # noqa: BLE001
        os._exit(1)


def call[T](work: Callable[[], T], seconds: float, after_fork: Callable[[], None] | None = None) -> Result[T]:
    """What `work` returns, or TIMEOUT after `seconds`, CRASHED when the child dies, GARBLED for an unreadable answer.

    `after_fork` runs in the parent once the child exists, before it waits."""
    context = multiprocessing.get_context("fork")
    rx, tx = context.Pipe(duplex=False)
    proc = context.Process(target=child_main, args=(work, tx, seconds), daemon=True)
    proc.start()
    tx.close()
    try:
        if after_fork is not None:
            after_fork()
        if not rx.poll(seconds):
            return Result(Outcome.TIMEOUT)
        try:
            return Result(Outcome.DONE, rx.recv())
        except EOFError:
            return Result(Outcome.CRASHED)
        except Exception:  # noqa: BLE001
            return Result(Outcome.GARBLED)
    finally:
        proc.kill()
        proc.join()
        rx.close()
