"""Markdown the CLI prints for the skills to paste verbatim: rule cards and the status listing."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import policy
import telemetry

MAX_WIDTH = 40
NEWLINE = "⏎"


class Source(StrEnum):
    YOURS = "yours"
    INFERRED = "inferred"
    CHOSEN = "you chose"


class Expect(StrEnum):
    MATCH = "match"
    PASS = "pass"


class Outcome(StrEnum):
    DIRECT = "direct"
    WRAPPED = "wrapped"
    ALLOWED = "allowed"
    UNEVALUATED = "unevaluated"


@dataclass(frozen=True, slots=True)
class Result:
    cmd: str
    source: Source
    outcome: Outcome
    expect: Expect | None = None
    case: int | None = None

    @property
    def matched(self) -> bool:
        return self.outcome in (Outcome.DIRECT, Outcome.WRAPPED)

    @property
    def mismatch(self) -> bool:
        return self.expect is not None and (self.outcome is Outcome.UNEVALUATED
                                            or (self.expect is Expect.MATCH) != self.matched)


@dataclass
class RuleRow:
    id: str
    action: str
    layers: list[str]
    state: str
    conditions: str = ""


@dataclass
class ModeRow:
    name: str
    on: str
    agent_may_enable: bool
    layers: list[str]


@dataclass
class Status:
    files: list[str]
    hook_on: bool
    project_off: bool
    rules: list[RuleRow]
    modes: list[ModeRow]
    problems: list[str]
    no_rules: str = "No rules installed. guardrails:setup installs presets."
    notes: list[str] = field(default_factory=list)


def char_width(ch: str) -> int:
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in text)


GLYPHS = str.maketrans({"\n": NEWLINE, "\t": "⇥", "\x1b": "␛", "\x7f": "␡"})
INVISIBLE = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")


def clean(text: str) -> str:
    """Replace everything that could start a new line, move the cursor or hide text with a visible form."""
    shown = text.replace("\r\n", "\n").replace("\r", "\n").translate(GLYPHS)
    return "".join(f"\\u{{{ord(ch):x}}}" if unicodedata.category(ch) in INVISIBLE else ch for ch in shown)


def prose(text: str) -> str:
    return clean(" ".join(text.split()))


def span(text: str, width: int = 0) -> str:
    """Inline code padded to width display columns; the delimiter spaces of a longer fence are not counted."""
    text = clean(text)
    pad = " " * max(0, width - display_width(text))
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    if longest or text.startswith(" "):
        fence = "`" * (longest + 1)
        return f"{fence} {text}{pad} {fence}"
    return f"`{text}{pad}`"


def common_width(texts: list[str]) -> int:
    return min(max((display_width(t) for t in texts), default=0), MAX_WIDTH)


def words(values: list[str]) -> str:
    return ", ".join(span(v) for v in values)


def compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def describe_match(rule: policy.Rule) -> str:
    return span(compact(rule.match)) + ("" if rule.wrappers else " · not through wrappers")


def describe_conditions(rule: policy.Rule) -> str:
    """The status row's tail: the rule's `when` and how many message cases it has."""
    parts = [f"when {span(compact(rule.when))}"] if rule.when is not None else []
    if rule.messages:
        parts.append(plural(len(rule.messages), "message case"))
    return " · ".join(parts)


def case_tag(rule: policy.Rule, result: Result) -> str:
    if not rule.messages or not result.matched:
        return ""
    return f" · case {result.case + 1}" if result.case is not None else " · default message"


def texts_under(node: object, keys: tuple[str, ...]) -> list[str]:
    """Every value under one of these keys in a rule object, in document order (a pattern object: its context)."""
    if isinstance(node, list):
        return [text for item in node for text in texts_under(item, keys)]
    if not isinstance(node, dict):
        return []
    out: list[str] = []
    for key, value in node.items():
        if key in keys and isinstance(value, str | dict):
            out.append(value if isinstance(value, str) else str(policy.view(value, "context")))
        else:
            out += texts_under(value, keys)
    return out


def raw_matcher(match: dict[str, Any]) -> str:
    """The match's patterns, else its regexes (`regex`, and `args` of a command atom), joined."""
    return " | ".join(texts_under(match, ("pattern",)) or texts_under(match, ("regex", "args")))


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}{'es' if word.endswith('ch') else 's'}"


def rule_card(rid: str, rule: policy.Rule, message: str, scope: str, intent: str,
              results: list[Result], notes: list[str], applies: bool = True, cases: tuple[str, ...] = ()) -> str:
    """`message` and `cases` are the texts as shown (placeholders other than captures filled)."""
    action = str(rule.action)
    head = [f"### {clean(rid)}", clean(action)]
    if rule.retry is policy.Retry.SAME_COMMAND:
        head.append("retry same-command")
    head.append(scope)
    lines = [" · ".join(head)]
    lines.append("")
    if intent:
        lines.append(f"**Intent** {prose(intent)}")
    lines.append(f"**Match** {describe_match(rule)}")
    if rule.when is not None:
        lines.append(f"**When** {span(compact(rule.when))} · {'holds here' if applies else 'does not hold here'}")
    lines.append(f"**Message** {prose(message)}")
    for i, (case, text) in enumerate(zip(rule.messages, cases, strict=True), 1):
        lines.append(f"**Case {i}** when {span(compact(case.when))} · {prose(text)}")

    shown = [clean(r.cmd) for r in results]
    width = common_width(shown)
    hit = "Warn" if rule.action is policy.Action.WARN else "Block"
    for heading, glyph, wanted in ((hit, "✗", Outcome.DIRECT), ("Allow", "✓", Outcome.ALLOWED), ("Not evaluated", "?", Outcome.UNEVALUATED)):
        group = [(text, r) for text, r in zip(shown, results) if (Outcome.DIRECT if r.matched else r.outcome) is wanted]
        if not group:
            continue
        group.sort(key=lambda pair: display_width(pair[0]) > width)
        lines += ["", f"**{heading}**"]
        for text, r in group:
            flag = "⚠ " if r.mismatch else ""
            tag = r.source + (" · wrapped" if r.outcome is Outcome.WRAPPED else "") + case_tag(rule, r)
            lines.append(f"- {glyph} {flag}{span(text, width)} {tag}")

    bad = sum(r.mismatch for r in results)
    count = f"{plural(len(results), 'command')}, {plural(bad, 'mismatch')}"
    blind = sum(r.outcome is Outcome.UNEVALUATED for r in results)
    lines += ["", f"**Verified** NOT verified: {blind} of {plural(len(results), 'command')} could not be evaluated" if blind
              else f"**Verified** matcher checked with `rule test`, {count}"]
    if notes:
        lines.append(f"**Note** {prose('; '.join(notes))}")
    raw = raw_matcher(rule.match)
    if raw:
        lines.append(f"**Raw** {span(raw)}")
    return "\n".join(lines)


def padded_rows(names: list[str]) -> list[str]:
    width = common_width(names)
    return [span(n, width) for n in names]


def status_listing(status: Status) -> str:
    lines = [*status.files, ""]
    count = plural(len(status.rules), "rule")
    head = f"### Guardrails · {count} · hook {'on' if status.hook_on else 'off'}"
    if status.project_off:
        head += " · project rules off"
    lines.append(head)
    lines += [f"**Note** {prose(note)}" for note in status.notes]
    lines += ["", "**Rules**"]
    if status.rules:
        for row, cell in zip(status.rules, padded_rows([r.id for r in status.rules])):
            lines.append(f"- {cell} {row_tail(row)}")
    else:
        lines.append(status.no_rules)
    if status.modes:
        lines += ["", "**Modes**"]
        for row, cell in zip(status.modes, padded_rows([m.name for m in status.modes])):
            lines.append(f"- {cell} {clean(row.on)} · agent may enable: {'yes' if row.agent_may_enable else 'no'} · "
                         f"{'+'.join(row.layers)}")
    if status.problems:
        lines += ["", "**Problems**"]
        lines += [f"- {prose(p)}" for p in status.problems]
    return "\n".join(lines)


def problems_listing(problems: list[str]) -> str:
    if not problems:
        return "No problems."
    return "\n".join(["**Problems**", *(f"- {prose(p)}" for p in problems)])


def row_tail(row: RuleRow) -> str:
    """Everything after the id: `conditions` is already rendered markdown."""
    tail = f"{clean(row.action)} · {'+'.join(row.layers)} · {clean(row.state)}"
    return f"{tail} · {row.conditions}" if row.conditions else tail


def rule_row(row: RuleRow) -> str:
    return f"- {span(row.id)} {row_tail(row)}"


@dataclass(slots=True)
class Total:
    key: str
    deny: int = 0
    warn: int = 0
    passed: int = 0
    suspended: int = 0
    micros: int = 0
    peak: int = 0
    last: int = 0

    def add(self, row: telemetry.Row) -> None:
        self.deny += row.deny
        self.warn += row.warn
        self.passed += row.passed
        self.suspended += row.suspended
        self.micros += row.micros
        self.peak = max(self.peak, row.peak)
        if row.deny or row.warn:
            self.last = max(self.last, row.hour)

    @property
    def runs(self) -> int:
        return self.deny + self.warn + self.passed + self.suspended

    @property
    def average(self) -> float:
        return self.micros / self.runs if self.runs else 0


def local_hour(hour: int) -> str:
    return datetime.fromtimestamp(hour * 3600, UTC).astimezone().strftime("%Y-%m-%d %H:00")


def fold(rows: list[telemetry.Row], key: Callable[[telemetry.Row], str]) -> dict[str, Total]:
    totals: dict[str, Total] = {}
    for row in rows:
        totals.setdefault(key(row), Total(key(row))).add(row)
    return totals


def timing(total: Total) -> str:
    return f"avg {total.average / 1000:.2f} ms · max {total.peak / 1000:.2f} ms"


def counts(total: Total) -> str:
    fired = f" · last fired {local_hour(total.last)}" if total.last else ""
    return (f"{total.deny} deny · {total.warn} warn · {total.passed} pass · {total.suspended} suspended · "
            f"{timing(total)}{fired}")


def stats_card(rows: list[telemetry.Row], known: list[str], days: int, slow: bool) -> str:
    totals = fold(rows, lambda row: row.rule)
    rules = [t for t in totals.values() if not t.key.startswith("@")]
    rules.sort(key=(lambda t: -t.average) if slow else (lambda t: (-(t.deny + t.warn), -t.runs, t.key)))
    calls = totals["@hook"].passed if "@hook" in totals else 0
    lines = [f"### Guardrails stats · last {plural(days, 'day')} · {plural(calls, 'call')}"]
    if rules:
        width = common_width([t.key for t in rules])
        lines += ["", "**Rules**", *(f"- {span(t.key, width)} {counts(t)}" for t in rules)]
    quiet = sorted({*known, *(t.key for t in rules)} - {t.key for t in rules if t.deny or t.warn})
    if quiet:
        width = common_width(quiet)
        lines += ["", "**Never fired**", *(f"- {span(rid, width)} " + (f"{totals[rid].passed} pass" if rid in totals
                                                                       else "no data") for rid in quiet)]
    slowest = sorted((t for t in rules if t.runs), key=lambda t: -t.average)[:3]
    if slowest:
        width = common_width([t.key for t in slowest])
        lines += ["", "**Slowest**", *(f"- {span(t.key, width)} {timing(t)}" for t in slowest)]
    ops = sorted((t for t in totals.values() if t.key.startswith("@")), key=lambda t: t.key)
    if ops:
        width = common_width([t.key for t in ops])
        lines += ["", "**Operational**", *(f"- {span(t.key, width)} {plural(t.deny + t.passed, 'time')}"
                                           + (f" · {timing(t)}" if t.micros else "") for t in ops)]
    return "\n".join(lines)


def rule_days(rows: list[telemetry.Row], rule: str, days: int) -> str:
    by_day = fold(rows, lambda row: local_hour(row.hour)[:10])
    width = common_width(list(by_day))
    return "\n".join([f"### {clean(rule)} · last {plural(days, 'day')}", "", "**Days**",
                      *(f"- {span(day, width)} {counts(by_day[day])}" for day in sorted(by_day, reverse=True))])
