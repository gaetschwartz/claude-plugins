"""Markdown the CLI prints for the skills to paste verbatim: rule cards and the status listing."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

import policy

MAX_WIDTH = 40
SOURCES = ("yours", "inferred", "you chose")
EXPECTATIONS = ("match", "pass")
UNEVALUATED = "unevaluated"
NEWLINE = "⏎"


@dataclass
class Result:
    cmd: str
    source: str
    kind: str | None
    expect: str | None = None

    @property
    def matched(self) -> bool:
        return self.kind in ("direct", "wrapped")

    @property
    def mismatch(self) -> bool:
        return self.expect is not None and (self.kind == UNEVALUATED or (self.expect == "match") != self.matched)


@dataclass
class RuleRow:
    id: str
    action: str
    layers: list[str]
    state: str


@dataclass
class ModeRow:
    name: str
    on: str
    agent_may_enable: bool
    layers: list[str]


@dataclass
class Status:
    managed: str
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


def describe_match(match: dict[str, object], programs: list[str]) -> str:
    parts = []
    if programs:
        parts.append("program = " + words(programs))
    for key in ("args", "regex", "builtin"):
        value = match.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key} = {span(value)}")
    if isinstance(match.get("ast"), dict) and match["ast"]:
        parts.append(f"ast = {span(compact(match['ast']))}")
    return "; ".join(parts) or "(no matcher)"


def raw_matcher(match: dict[str, object]) -> str:
    value = match.get("regex")
    if isinstance(value, str) and value:
        return value
    patterns = policy.ast_patterns(match.get("ast"))
    if patterns:
        return " | ".join(patterns)
    value = match.get("args")
    return value if isinstance(value, str) else ""


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}{'es' if word.endswith('ch') else 's'}"


def kind_group(result: Result) -> str | None:
    return "direct" if result.matched else result.kind if result.kind == UNEVALUATED else None


def rule_card(rid: str, rule: dict[str, object], programs: list[str], message: str, scope: str, intent: str,
              results: list[Result], notes: list[str], file: str = "") -> str:
    action = str(rule.get("action"))
    head = [f"### {clean(rid)}", clean(action)]
    if rule.get("retry") == "same-command":
        head.append("retry same-command")
    head.append(scope)
    lines = [" · ".join(head)]
    if file:
        lines.append(f"**File** {span(file)}")
    lines.append("")
    if intent:
        lines.append(f"**Intent** {prose(intent)}")
    match = rule.get("match")
    match = match if isinstance(match, dict) else {}
    lines.append(f"**Match** {describe_match(match, programs)}")
    lines.append(f"**Message** {prose(message)}")

    shown = [clean(r.cmd) for r in results]
    width = common_width(shown)
    hit = "Warn" if action == "warn" else "Block"
    for heading, glyph, wanted in ((hit, "✗", "direct"), ("Allow", "✓", None), ("Not evaluated", "?", UNEVALUATED)):
        group = [(text, r) for text, r in zip(shown, results) if kind_group(r) == wanted]
        if not group:
            continue
        group.sort(key=lambda pair: display_width(pair[0]) > width)
        lines += ["", f"**{heading}**"]
        for text, r in group:
            flag = "⚠ " if r.mismatch else ""
            tag = r.source + (" · wrapped" if r.kind == "wrapped" else "")
            lines.append(f"- {glyph} {flag}{span(text, width)} {tag}")

    bad = sum(r.mismatch for r in results)
    count = f"{plural(len(results), 'command')}, {plural(bad, 'mismatch')}"
    blind = sum(r.kind == UNEVALUATED for r in results)
    lines += ["", f"**Verified** NOT verified: {blind} of {plural(len(results), 'command')} could not be evaluated" if blind
              else f"**Verified** matcher checked with `rule test`, {count}"]
    if notes:
        lines.append(f"**Note** {prose('; '.join(notes))}")
    raw = raw_matcher(match)
    if raw:
        lines.append(f"**Raw** {span(raw)}")
    return "\n".join(lines)


def padded_rows(names: list[str]) -> list[str]:
    width = common_width(names)
    return [span(n, width) for n in names]


def status_listing(status: Status) -> str:
    lines = [status.managed, ""]
    count = plural(len(status.rules), "rule")
    head = f"### Guardrails · {count} · hook {'on' if status.hook_on else 'off'}"
    if status.project_off:
        head += " · project rules off"
    lines.append(head)
    lines += [f"**Note** {prose(note)}" for note in status.notes]
    lines += ["", "**Rules**"]
    if status.rules:
        for row, cell in zip(status.rules, padded_rows([r.id for r in status.rules])):
            lines.append(f"- {cell} {clean(row.action)} · {'+'.join(row.layers)} · {clean(row.state)}")
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


def rule_row(row: RuleRow) -> str:
    return f"- {span(row.id)} {clean(row.action)} · {'+'.join(row.layers)} · {clean(row.state)}"
