"""The vocabulary of a match: how a rule selected a command, why one could not be judged, and the result."""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum
from typing import NamedTuple

import bootstrap

MAX_COMMAND_BYTES = 256 << 10


class Kind(StrEnum):
    DIRECT = "direct"
    WRAPPED = "wrapped"


class Limit(StrEnum):
    """Which bound a command ran into; the value reads after "the command"."""

    DEPTH = "nests shell strings too deeply"
    UNITS = "unpacks into too many shell strings"
    SIZE = "unpacks into too much shell text"
    VARIANTS = "unwraps into too many command variants"
    VARIANT_BYTES = "unwraps into too much command text to parse"


class Fault(StrEnum):
    """Why a call was not judged; the value is the pseudo rule id telemetry counts it under."""

    OVERSIZE = "@oversize"
    COMPLEXITY = "@complexity"
    TIMEOUT = "@timeout"
    CRASH = "@crash"
    ENGINE = "@engine-failure"


FAILED_PREFIX = "engine-failed:"


class Notice(NamedTuple):
    key: str
    text: str


class Detail(NamedTuple):
    """What a rule's message needs from the node that decided its verdict: the message case that holds (None: the
    rule's own message) and the captures its texts name."""

    case: int | None
    captures: dict[str, str]


class UnitTree(NamedTuple):
    """One parse unit as `guardrails rule ast` shows it: (depth, kind, text or None) per named node."""

    label: str
    src: str
    rows: list[tuple[int, str, str | None]]
    broken: bool


class Evaluation:
    """How each rule's matcher selects one command, and what kept a rule from being judged."""

    __slots__ = ("details", "failure", "fault", "invalid", "kinds", "micros", "parse_us", "runtime_broken", "unchecked",
                 "unevaluated", "unparsed")

    def __init__(self, kinds: dict[str, Kind | None]) -> None:
        self.kinds = kinds
        self.details: dict[str, Detail] = {}
        self.unevaluated: set[str] = set()
        self.invalid: dict[str, str] = {}
        self.failure: str | None = None
        self.unchecked: str | None = None
        self.runtime_broken = False
        self.fault: Fault | None = None
        self.micros: dict[str, int] = {}
        self.parse_us = 0
        self.unparsed = False

    def warnings(self, managed: Collection[str] = ()) -> list[Notice]:
        """A stable key and text per problem. Keys under FAILED_PREFIX are per failure class and repeat; others are shown once."""
        out = [Notice(f"rule-invalid:{rid}", f"guardrails: rule {rid} does not compile ({why}) and is skipped")
               for rid, why in sorted(self.invalid.items())]
        if self.failure is not None:
            affected = sorted(self.unevaluated)
            listed = f" Affected rules: {', '.join(affected)}." if affected else ""
            fail_open = sum(1 for rid in affected if rid in managed)
            managed_note = f" {fail_open} of them are MANAGED rules, which fail open too." if fail_open else ""
            out.append(Notice(FAILED_PREFIX + "engine", (
                f"[guardrails plugin notice] The rules engine failed on this command ({self.failure}), so rules could "
                f"not be checked and the command was allowed.{listed}{managed_note} Tell the user if this keeps "
                f"happening (this notice repeats every {bootstrap.REPEAT_SECONDS // 60} minutes while it does); "
                "`guardrails engine status` shows the runtime.")))
        return out
