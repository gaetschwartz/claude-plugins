"""Conditions: the `when` of a rule and of each message case, a tree of `all` / `any` / `not` over atoms.

Environment atoms read where the hook runs (binaries on PATH, OS, architecture, host, environment variables, files in
the project, the calling tool). Hit atoms (`matches`, `wrapped`) read the command node that decided a rule's verdict and
exist only in message cases. All input here is trusted configuration.
"""

from __future__ import annotations

import functools
import os
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

import policy

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_DEPTH = 8
MAX_NODES = 64
TOOLS = ("Bash", "Monitor")
OSES = ("linux", "macos")
ARCHES = ("arm64", "x86_64")
COMBINATORS = ("all", "any", "not")
ENVIRONMENT_ATOMS = ("bin", "os", "arch", "host", "env", "file", "tool")
HIT_ATOMS = ("matches", "wrapped")
MACHINES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}


class Env(NamedTuple):
    """What a condition may read beyond the process environment: the calling tool and the project root."""

    tool: str = "Bash"
    root: Path | None = None


DEFAULT = Env()


class Hit(Protocol):
    """The node that decided a rule's verdict, as message cases see it."""

    wrapped: bool

    def matches(self, rule: Mapping[str, Any]) -> bool: ...


def variable_name(name: object) -> bool:
    return isinstance(name, str) and name.isascii() and name.isidentifier()


def relative_path(path: object) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    pure = PurePosixPath(path)
    return not pure.is_absolute() and ".." not in pure.parts


class Checker:
    """Validates one condition tree, counting nodes against the caps."""

    def __init__(self, hit: bool) -> None:
        self.hit = hit
        self.nodes = 0

    def check(self, node: object, where: str, depth: int = 1) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise policy.Invalid(f"'{where}': a condition has more than {MAX_NODES} nodes")
        if depth > MAX_DEPTH:
            raise policy.Invalid(f"'{where}': a condition nests deeper than {MAX_DEPTH} levels")
        if not isinstance(node, dict) or len(node) != 1:
            raise policy.Invalid(f"'{where}' must be an object with exactly one key (combine several with all)")
        ((key, value),) = node.items()
        if key in ("all", "any"):
            if not isinstance(value, list) or not value:
                raise policy.Invalid(f"'{where}.{key}' must be a non-empty list of conditions")
            for i, item in enumerate(value):
                self.check(item, f"{where}.{key}[{i}]", depth + 1)
        elif key == "not":
            self.check(value, f"{where}.not", depth + 1)
        elif key in HIT_ATOMS:
            if not self.hit:
                raise policy.Invalid(f"'{where}.{key}' only works in a message case's when, which sees the matched "
                                     "command; a rule's when is checked before any command is read")
            self.hit_atom(key, value, f"{where}.{key}")
        elif key in ENVIRONMENT_ATOMS:
            environment_atom(key, value, f"{where}.{key}")
        else:
            known = (*COMBINATORS, *ENVIRONMENT_ATOMS, *(HIT_ATOMS if self.hit else ()))
            raise policy.Invalid(f"unknown condition '{where}.{key}' (known: {', '.join(known)})")

    def hit_atom(self, key: str, value: object, where: str) -> None:
        if key == "wrapped":
            if not isinstance(value, bool):
                raise policy.Invalid(f"'{where}' must be true or false")
            return
        try:
            policy.match_of(value)
        except policy.Invalid as exc:
            raise policy.Invalid(f"'{where}': {exc}") from None


def well_formed(key: str, value: object) -> bool:
    match key:
        case "bin":
            names = [value] if isinstance(value, str) else value
            return isinstance(names, list) and bool(names) and all(policy.command_name(n) for n in names)
        case "os":
            return value in OSES
        case "arch":
            return value in ARCHES
        case "host":
            return isinstance(value, str) and bool(value) and not any(c.isspace() for c in value)
        case "env":
            return variable_name(value) or (isinstance(value, dict) and len(value) == 1
                                            and all(variable_name(k) and isinstance(v, str) for k, v in value.items()))
        case "file":
            return relative_path(value)
        case "tool":
            return value in TOOLS
        case _:
            return False


def environment_atom(key: str, value: object, where: str) -> None:
    if not well_formed(key, value):
        raise policy.Invalid(f"'{where}' must be {SHAPES[key]}")


SHAPES = {
    "bin": "a command name or a non-empty list of them (no spaces or '/')",
    "os": f"one of {', '.join(OSES)}",
    "arch": f"one of {', '.join(ARCHES)}",
    "host": "a host name (no spaces)",
    "env": 'a variable name, or {"NAME": "value"} with one variable',
    "file": "a path relative to the project root (not absolute, no '..')",
    "tool": f"one of {', '.join(TOOLS)}",
}


def check(node: object, where: str, *, hit: bool = False) -> None:
    """Raise policy.Invalid naming the node path when the condition is malformed or over the caps."""
    Checker(hit).check(node, where)


@functools.cache
def which(name: str, path: str | None) -> bool:
    import shutil

    return shutil.which(name, path=path) is not None


def on_path(name: str) -> bool:
    return which(name, os.environ.get("PATH"))


@functools.cache
def hostname() -> str:
    import socket

    return socket.gethostname()


def current_os() -> str:
    return "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else sys.platform


def current_arch() -> str | None:
    import platform

    return MACHINES.get(platform.machine().lower())


def atom_holds(key: str, value: Any, env: Env, hit: Hit | None) -> bool:
    match key:
        case "bin":
            return any(on_path(name) for name in ([value] if isinstance(value, str) else value))
        case "os":
            return current_os() == value
        case "arch":
            return current_arch() == value
        case "host":
            full = hostname()
            return value in (full, full.split(".", 1)[0])
        case "env":
            if isinstance(value, str):
                return bool(os.environ.get(value))
            ((name, wanted),) = value.items()
            return os.environ.get(name) == wanted
        case "file":
            return env.root is not None and (env.root / value).exists()
        case "tool":
            return env.tool == value
        case "matches":
            return hit is not None and hit.matches(value)
        case "wrapped":
            return hit is not None and hit.wrapped is value
        case _:
            raise policy.Invalid(f"unknown condition {key}")


def holds(node: Mapping[str, Any] | None, env: Env, hit: Hit | None = None) -> bool:
    """Whether a validated condition holds; no condition always holds."""
    if node is None:
        return True
    ((key, value),) = node.items()
    if key == "all":
        return all(holds(item, env, hit) for item in value)
    if key == "any":
        return any(holds(item, env, hit) for item in value)
    if key == "not":
        return not holds(value, env, hit)
    return atom_holds(key, value, env, hit)


def match_atoms(node: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """The rule object of each `matches` atom, in document order."""
    ((key, value),) = node.items()
    if key in ("all", "any"):
        for item in value:
            yield from match_atoms(item)
    elif key == "not":
        yield from match_atoms(value)
    elif key == "matches":
        yield value


def bin_atoms(node: Mapping[str, Any] | None) -> Iterator[list[str]]:
    """The names of each `bin` atom outside any `not`, in document order."""
    if node is None:
        return
    ((key, value),) = node.items()
    if key in ("all", "any"):
        for item in value:
            yield from bin_atoms(item)
    elif key == "bin":
        yield [value] if isinstance(value, str) else value


def found(node: Mapping[str, Any] | None) -> str | None:
    """The `{found}` placeholder: the first name of a `bin` atom (outside `not`) that is on PATH."""
    return next((name for names in bin_atoms(node) for name in names if on_path(name)), None)
