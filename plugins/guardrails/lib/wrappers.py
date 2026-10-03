"""Wrapper commands: names of commands that run another command (sudo, env, xargs, ...), extendable per layer."""

from __future__ import annotations

Names = tuple[str, ...]

DEFAULTS: Names = ("sudo", "doas", "env", "timeout", "nice", "nohup", "time", "command", "exec", "builtin", "stdbuf",
                   "setsid", "ionice", "xargs", "watch")


def check_name(name: object) -> None:
    """Raise ValueError when a wrapper name is malformed."""
    if not isinstance(name, str) or not name or "/" in name or any(c.isspace() for c in name):
        raise ValueError(f"wrapper name {name!r} must be a command name without '/' or spaces")


def resolve(labelled: list[tuple[str, object]]) -> tuple[Names, list[str]]:
    """The built-in names plus every valid name from the layers, and why entries were ignored.

    Layers only ever add names, so a lower layer can never narrow what a higher one looks through.
    """
    names = list(DEFAULTS)
    notes: list[str] = []
    for where, layer in labelled:
        entries = layer.get("wrappers") if isinstance(layer, dict) else None
        if entries is None:
            continue
        if not isinstance(entries, dict):
            notes.append(f"{where}: 'wrappers' must be an object, so all its entries are ignored")
            continue
        for name in entries:
            try:
                check_name(name)
            except ValueError as exc:
                notes.append(f"{where}: {exc}, so it is ignored")
                continue
            if name not in names:
                names.append(name)
    return tuple(names), notes
