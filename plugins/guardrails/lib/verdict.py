"""The vocabulary of a match: how a rule selected a command, why one could not be judged, and the result."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple, assert_never


class Kind(StrEnum):
    DIRECT = "direct"
    WRAPPED = "wrapped"


class Limit(StrEnum):
    DEPTH = "depth"
    UNITS = "units"
    SIZE = "size"
    VARIANTS = "variants"
    VARIANT_BYTES = "variant-bytes"


class Refusal(StrEnum):
    OVERSIZE = "oversize"
    COMPLEX = "complex"
    TIMEOUT = "timeout"


FAILED_PREFIX = "engine-failed:"


class UnitTree(NamedTuple):
    """One parse unit as `guardrails rule ast` shows it: (depth, kind, text or None) per named node."""

    label: str
    src: str
    rows: list[tuple[int, str, str | None]]
    broken: bool


def limit_reason(limit: Limit) -> str:
    match limit:
        case Limit.DEPTH:
            return "nests shell strings too deeply"
        case Limit.UNITS:
            return "unpacks into too many shell strings"
        case Limit.SIZE:
            return "unpacks into too much shell text"
        case Limit.VARIANTS:
            return "unwraps into too many command variants"
        case Limit.VARIANT_BYTES:
            return "unwraps into too much command text to parse"
        case _:
            assert_never(limit)


@dataclass(slots=True)
class Evaluation:
    """How each rule's matcher selects one command, and what kept a rule from being judged."""

    kinds: dict[str, Kind | None] = field(default_factory=dict)
    unevaluated: set[str] = field(default_factory=set)
    invalid: dict[str, str] = field(default_factory=dict)
    failure: str | None = None
    refusal: str | None = None
    refusal_kind: Refusal | None = None

    def failure_kind(self) -> str:
        return str(self.refusal_kind) if self.refusal_kind else ("engine" if self.failure else "")

    def warnings(self, managed: Collection[str] = ()) -> list[tuple[str, str]]:
        """(stable key, text) per problem. Keys under FAILED_PREFIX are per failure class, not per message."""
        out = [(f"rule-invalid:{rid}", f"guardrails: rule {rid} does not compile ({why}) and is skipped")
               for rid, why in sorted(self.invalid.items())]
        if self.failure is not None:
            affected = sorted(self.unevaluated)
            listed = f" Affected rules: {', '.join(affected)}." if affected else ""
            fail_open = sum(1 for rid in affected if rid in managed)
            managed_note = f" {fail_open} of them are MANAGED rules, which fail open too." if fail_open else ""
            out.append((FAILED_PREFIX + "engine",
                        (f"[guardrails plugin notice] The rules engine failed on this command ({self.failure}), so rules "
                        "that use program, args, builtin or match.ast could not be checked and the command was allowed. "
                        f"Rules that use regex were still applied.{listed}{managed_note} Tell the user if this keeps "
                        "happening (this notice repeats every 10 minutes while it does); `guardrails engine status` "
                        "shows the runtime.")))
        return out
