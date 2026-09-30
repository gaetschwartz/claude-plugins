"""Wall-clock limits for the hook: one overall deadline and nested local limits, both raised from SIGALRM."""

from __future__ import annotations

import contextlib
import signal
import threading
import time
from collections.abc import Iterator


class Expired(BaseException):
    """The overall deadline passed; not an Exception so ordinary handlers cannot swallow it."""


class Local(Exception):
    """A local limit passed."""


_until: list[float | None] = [None]


def usable() -> bool:
    return hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()


def _handler(*_: object) -> None:
    until = _until[0]
    if until is not None and time.monotonic() >= until - 0.01:
        raise Expired
    raise Local


def start(seconds: float) -> None:
    if usable():
        signal.signal(signal.SIGALRM, _handler)
        _until[0] = time.monotonic() + seconds
        signal.setitimer(signal.ITIMER_REAL, seconds)


def stop() -> None:
    if usable():
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
    _until[0] = None


@contextlib.contextmanager
def limit(seconds: float) -> Iterator[None]:
    """Raise TimeoutError if the block runs longer than this (or than the overall deadline allows)."""
    if not usable():
        yield
        return
    until = _until[0]
    previous = signal.signal(signal.SIGALRM, _handler)
    wait = seconds if until is None else max(0.001, min(seconds, until - time.monotonic()))
    signal.setitimer(signal.ITIMER_REAL, wait)
    try:
        yield
    except Local as exc:
        raise TimeoutError from exc
    finally:
        if until is None:
            signal.setitimer(signal.ITIMER_REAL, 0)
        else:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, until - time.monotonic()))
        signal.signal(signal.SIGALRM, previous)
