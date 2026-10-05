"""Message texts: placeholders (`{found}`, `{ARG}` captures, `{{` `}}` for braces) and the cases that pick a text.

Templates are tokenized by the standard library's `string.Formatter().parse()` and filled by substituting simple
field names only; rule text never goes through `str.format`.
"""

from __future__ import annotations

import string
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, NamedTuple

import policy

if TYPE_CHECKING:
    from ast_grep_py import SgNode

FOUND = "found"
MAX_CAPTURES = 8
MAX_CAPTURE_CHARS = 200
MAX_CASES = 16
CASE_KEYS = frozenset({"when", "text", "messageShort"})
CAPTURE_START = frozenset(string.ascii_uppercase + "_")
CAPTURE_CHARS = CAPTURE_START | frozenset(string.digits)


class Case(NamedTuple):
    when: dict[str, Any]
    text: str
    message_short: str | None = None


def capture_name(name: str) -> bool:
    """An ast-grep metavariable name without its `$`: `ARG` for `$ARG` and `$$$ARG`."""
    return bool(name) and name[0] in CAPTURE_START and set(name) <= CAPTURE_CHARS


def fields(text: str, where: str) -> list[str]:
    """The placeholder names of a template, in order; raises policy.Invalid on anything but `{name}`."""
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise policy.Invalid(f"'{where}': {exc}; write {{{{ and }}}} for literal braces") from None
    names = []
    for _, name, spec, conversion in parts:
        if name is None:
            continue
        if conversion is not None or spec:
            raise policy.Invalid(f"'{where}': placeholder {{{name}}} takes no conversion or format spec; write "
                                 "{{ and }} for literal braces")
        if name != FOUND and not capture_name(name):
            raise policy.Invalid(f"'{where}': placeholder {{{name}}} must be {{found}} or a metavariable name such "
                                 "as {ARG} (no attribute access or indexing); write {{ and }} for literal braces")
        names.append(name)
    return names


def check_texts(texts: list[tuple[str, str]], has_bin: bool, bound: Callable[[], Collection[str] | None]) -> None:
    """Validate every template of one rule together: `{found}` needs a `bin` atom in the rule's `when`, and every
    other placeholder must be one of the names the rule's match `bound` (read once, and only when a text has one)."""
    captures: set[str] = set()
    names: Collection[str] | None = None
    read = False
    for where, text in texts:
        for name in fields(text, where):
            if name != FOUND:
                if not read:
                    names, read = bound(), True
                if names is not None and name not in names:
                    raise policy.Invalid(f"'{where}': placeholder {{{name}}} is never bound by the match; bind it "
                                         f"with a capture atom or a ${name} metavariable in a pattern")
                captures.add(name)
            elif not has_bin:
                raise policy.Invalid(f"'{where}' uses {{found}}, which needs a bin atom (outside not) in the rule's "
                                     "when")
    if len(captures) > MAX_CAPTURES:
        raise policy.Invalid(f"the messages use {len(captures)} capture placeholders; at most {MAX_CAPTURES}")


def cases_of(raw: object, where: str = "messages") -> tuple[Case, ...]:
    import conditions

    if not isinstance(raw, list) or not raw:
        raise policy.Invalid(f"'{where}' must be a non-empty list of cases")
    if len(raw) > MAX_CASES:
        raise policy.Invalid(f"'{where}' has {len(raw)} cases; at most {MAX_CASES}")
    out = []
    for i, case in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(case, dict) or "when" not in case or "text" not in case:
            raise policy.Invalid(f"'{at}' must be an object with 'when', 'text' and optional 'messageShort'")
        if unknown := set(case) - CASE_KEYS:
            raise policy.Invalid(f"unknown field {at}.{min(unknown)}: a case has 'when', 'text' and 'messageShort'")
        if not isinstance(case["text"], str) or not case["text"].strip():
            raise policy.Invalid(f"'{at}.text' must be a non-empty string")
        if "messageShort" in case and not isinstance(case["messageShort"], str):
            raise policy.Invalid(f"'{at}.messageShort' must be a string")
        conditions.check(case["when"], f"{at}.when", hit=True)
        out.append(Case(case["when"], case["text"], case.get("messageShort")))
    return tuple(out)


def rule_texts(rule: policy.Rule) -> list[tuple[str, str]]:
    """(where, template) for every text of a rule."""
    texts = [("message", rule.message)]
    if rule.message_short is not None:
        texts.append(("messageShort", rule.message_short))
    for i, case in enumerate(rule.messages):
        texts.append((f"messages[{i}].text", case.text))
        if case.message_short is not None:
            texts.append((f"messages[{i}].messageShort", case.message_short))
    return texts


def chosen(rule: policy.Rule, case: int | None) -> tuple[str, str | None]:
    """The (full, short) templates for a hit: the chosen case's, else the rule's own."""
    if case is None or not 0 <= case < len(rule.messages):
        return rule.message, rule.message_short
    picked = rule.messages[case]
    return picked.text, picked.message_short


def captures_wanted(texts: tuple[str, str | None]) -> list[str]:
    names: list[str] = []
    for text in texts:
        if text is not None:
            names += [n for _, n, _, _ in string.Formatter().parse(text) if n and n != FOUND and n not in names]
    return names


def capture(node: SgNode, name: str) -> str | None:
    """What the metavariable bound on the matched node: one node's text, or a `$$$` run's texts joined by a space."""
    single = node.get_match(name)
    if single is not None:
        text = single.text()
    else:
        many = node.get_multiple_matches(name)
        if not many:
            return None
        text = " ".join(part.text() for part in many)
    text = " ".join(line.strip() for line in text.splitlines())
    return text if len(text) <= MAX_CAPTURE_CHARS else text[:MAX_CAPTURE_CHARS] + "…"


def fill(template: str, values: dict[str, str]) -> str:
    """The template with each placeholder replaced; one without a value (an unbound capture) becomes empty."""
    return "".join(literal + (values.get(name, "") if name is not None else "")
                   for literal, name, _, _ in string.Formatter().parse(template))
