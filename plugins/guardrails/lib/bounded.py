"""Run a function in a child process that is killed at a deadline.

A native call into ast-grep holds the GIL, so neither a signal nor a watchdog thread can interrupt it; a child process
can be killed whatever it is doing, and the parent always gets control back. The answer travels as JSON, never pickle:
the child parses hostile input and the parent must not execute what it sends back.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import multiprocessing.context
import os
import sys
from collections.abc import Callable
from enum import StrEnum
from multiprocessing.connection import Connection
from typing import NamedTuple

try:
    import resource
except ImportError:
    resource = None


class Outcome(StrEnum):
    DONE = "done"
    TIMEOUT = "timeout"
    CRASHED = "crashed"
    GARBLED = "garbled"


class Result(NamedTuple):
    outcome: Outcome
    payload: object = None


def child_main(work: Callable[[], object], tx: Connection, seconds: float) -> None:
    """In the child: cap CPU time (so an orphan cannot spin on), compute, send the answer, never print a traceback."""
    try:
        if resource is not None:
            cap = int(seconds) + 2
            resource.setrlimit(resource.RLIMIT_CPU, (cap, cap))
        tx.send_bytes(json.dumps(work()).encode())
    except BaseException:  # noqa: BLE001
        os._exit(1)
    os._exit(0)


def start_context() -> multiprocessing.context.BaseContext:
    """Fork where the platform has it: starting a child costs nothing then."""
    return multiprocessing.get_context("fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn")


def call(work: Callable[[], object], seconds: float, after_start: Callable[[], None] | None = None) -> Result:
    """What `work` returns (a JSON value), or TIMEOUT after `seconds`, CRASHED when the child dies, GARBLED for an
    unreadable answer. `work` must be picklable.

    `after_start` runs in the parent once the child exists, before it waits."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.flush()
    context = start_context()
    rx, tx = context.Pipe(duplex=False)
    child = context.Process(target=child_main, args=(work, tx, seconds), daemon=True)
    child.start()
    tx.close()
    answer = None
    try:
        if after_start is not None:
            after_start()
        if rx.poll(seconds):
            with contextlib.suppress(EOFError):
                answer = rx.recv_bytes()
            if answer is None:
                answer = b""
    finally:
        child.kill()
        child.join()
        rx.close()
    if answer is None:
        return Result(Outcome.TIMEOUT)
    if not answer:
        return Result(Outcome.CRASHED)
    try:
        return Result(Outcome.DONE, json.loads(answer))
    except ValueError:
        return Result(Outcome.GARBLED)
