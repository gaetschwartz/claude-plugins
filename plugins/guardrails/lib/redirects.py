"""Typed ast-grep rules for the redirect atoms (`redirect`, `discards`) and the `statement` atom. A command with a
redirect is wrapped in a `redirected_statement` whose body is the command and whose other children are the redirects,
left to right. All of it is data for ast-grep; nothing here reads shell syntax."""

from __future__ import annotations

import re
from typing import Any

import policy

type Rule = Any

WRAPPER = "redirected_statement"
REDIRECT_KINDS = ("file_redirect", "heredoc_redirect", "herestring_redirect")
WRITES = (">", ">>", ">|")
BOTH = ("&>", "&>>")
OPERATORS = (*WRITES, "<", "<&", "<&-", ">&", ">&-", *BOTH)
FDS = (0, 1, 2, "&")
STREAMS = ("stdout", "stderr", "all")
DUP_DEPTH = 3
DEVNULL = "/dev/null"
QUOTED = "^(?:{0}|'{0}'|\"{0}\")$"
BODY = {"inside": {"kind": WRAPPER, "field": "body"}}


def token(*operators: str) -> Rule:
    return {"has": {"regex": "^(?:" + "|".join(re.escape(op) for op in operators) + ")$"}}


def descriptor(number: int) -> Rule:
    return {"has": {"field": "descriptor", "regex": f"^{number}$"}}


def target(regex: str) -> Rule:
    return {"has": {"field": "destination", "regex": regex}}


def quoted(text: str) -> str:
    return QUOTED.format(re.escape(text))


NO_DESCRIPTOR: Rule = {"not": {"has": {"field": "descriptor", "kind": "file_descriptor"}}}
NUMBER: Rule = {"has": {"field": "destination", "kind": "number"}}
NOT_NUMBER: Rule = {"not": NUMBER}
TO_NULL = target(quoted(DEVNULL))


def stdout_fd() -> Rule:
    return {"any": [descriptor(1), NO_DESCRIPTOR]}


def writes(stream: int) -> Rule:
    """A redirect that points the stream at a file: `>f`, `2>>f`, `&>f`, `>&f`."""
    own = {"all": [token(*WRITES), stdout_fd() if stream == 1 else descriptor(2)]}
    return {"kind": "file_redirect", "any": [own, token(*BOTH), {"all": [token(">&"), NO_DESCRIPTOR, NOT_NUMBER]}]}


def duplicates(stream: int) -> Rule:
    """`2>&1` (stream 2) or `>&2` (stream 1): the stream becomes a copy of the other; `>&1` onto itself is nothing."""
    own = 1 if stream == 1 else 2
    return {"kind": "file_redirect",
            "all": [token(">&"), stdout_fd() if stream == 1 else descriptor(2), NUMBER, {"not": target(f"^{own}$")}]}


def closes(stream: int) -> Rule:
    return {"kind": "file_redirect", "all": [token(">&-"), stdout_fd() if stream == 1 else descriptor(2)]}


def determines(stream: int) -> Rule:
    """A redirect that decides where the stream goes from here on."""
    return {"any": [writes(stream), duplicates(stream), closes(stream)]}


def nulls(stream: int, depth: int) -> Rule:
    """A redirect after which the stream is /dev/null, following copies of the other stream `depth` times."""
    direct = {"all": [writes(stream), TO_NULL]}
    if depth == 0:
        return direct
    other = 2 if stream == 1 else 1
    copy = {"kind": "file_redirect",
            "all": [token(">&"), stdout_fd() if stream == 1 else descriptor(2), NUMBER, target(f"^{other}$")],
            "follows": {"all": [determines(other), nulls(other, depth - 1)], "stopBy": determines(other)}}
    return {"any": [direct, copy]}


def sends_to_null(stream: int) -> Rule:
    """The last redirect that decides the stream is one that leaves it at /dev/null."""
    return {"has": {"kind": "file_redirect", "all": [nulls(stream, DUP_DEPTH)],
                    "not": {"precedes": {**determines(stream), "stopBy": "end"}}}}


def discards(value: object, where: str) -> Rule:
    if value not in STREAMS:
        raise policy.Invalid(f"'{where}.discards' must be one of {', '.join(map(repr, STREAMS))}, not {value!r}")
    streams = {"stdout": (1,), "stderr": (2,), "all": (1, 2)}[str(value)]
    return {"kind": WRAPPER, "all": [sends_to_null(stream) for stream in streams]}


def redirect(value: object, where: str) -> Rule:
    if not isinstance(value, dict) or (unknown := set(value) - {"fd", "op", "to"}):
        raise policy.Invalid(f"'{where}.redirect' must be an object with optional 'fd', 'op' and 'to'"
                             + (f" (unknown key {min(unknown)!r})" if isinstance(value, dict) else ""))
    parts: list[Rule] = []
    if "fd" in value:
        parts.append(stream_rule(value["fd"], where))
    if "op" in value:
        if value["op"] not in OPERATORS:
            raise policy.Invalid(f"'{where}.redirect.op' must be one of {', '.join(OPERATORS)}, not {value['op']!r}")
        parts.append(token(value["op"]))
    if "to" in value:
        parts.append(target(destination(value["to"], where)))
    if not parts:
        return {"any": [{"kind": kind} for kind in REDIRECT_KINDS]}
    return {"kind": "file_redirect", "all": parts}


def stream_rule(fd: object, where: str) -> Rule:
    if isinstance(fd, bool) or not (fd == "&" or (isinstance(fd, int) and fd >= 0)):
        raise policy.Invalid(f"'{where}.redirect.fd' must be a descriptor number or \"&\" (both streams), not {fd!r}")
    both: Rule = {"any": [token(*BOTH), {"all": [token(">&"), NO_DESCRIPTOR, NOT_NUMBER]}]}
    match fd:
        case "&":
            return both
        case 0:
            return {"any": [descriptor(0), {"all": [NO_DESCRIPTOR, token("<", "<&", "<&-")]}]}
        case 1:
            return {"any": [descriptor(1), {"all": [NO_DESCRIPTOR, token(*WRITES, ">&", ">&-")]}, token(*BOTH)]}
        case 2:
            return {"any": [descriptor(2), both]}
        case int(number):
            return descriptor(number)
        case _:
            raise policy.Invalid(f"'{where}.redirect.fd' must be a descriptor number or \"&\", not {fd!r}")


def destination(value: object, where: str) -> str:
    if isinstance(value, str) and value:
        return quoted(value)
    if isinstance(value, dict) and set(value) == {"regex"} and isinstance(value["regex"], str):
        return value["regex"]
    raise policy.Invalid(f"'{where}.redirect.to' must be a non-empty string or {{\"regex\": \"...\"}}")


def statement(inner: Rule) -> Rule:
    """The statement of a node matching the rule: the redirect wrapper when the node is its body, else the node."""
    return {"any": [{"kind": WRAPPER, "has": {"field": "body", "all": [inner]}},
                    {"all": [inner], "not": BODY}]}
