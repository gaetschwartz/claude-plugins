"""Wrapper table: commands that run another command (sudo, env, bash -c, ...), as data with user-extensible entries."""

from __future__ import annotations

from typing import Any

Entry = dict[str, Any]
Table = dict[str, Entry]

DEFAULTS: Table = {
    "sudo": {"flagsWithValue": ["-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "--user", "--group"], "assignments": True},
    "doas": {"flagsWithValue": ["-u", "-C"]},
    "env": {"flagsWithValue": ["-u", "-C", "-S", "--unset", "--chdir"], "assignments": True},
    "timeout": {"flagsWithValue": ["-k", "-s", "--kill-after", "--signal"], "skip": 1},
    "nice": {"flagsWithValue": ["-n", "--adjustment"]},
    "ionice": {"flagsWithValue": ["-c", "-n", "-p"]},
    "nohup": {},
    "time": {},
    "command": {"noCommandFlags": ["-v", "-V"]},
    "exec": {"flagsWithValue": ["-a"]},
    "builtin": {},
    "stdbuf": {"flagsWithValue": ["-i", "-o", "-e"]},
    "setsid": {},
    "xargs": {"flagsWithValue": ["-I", "-i", "-d", "-a", "-E", "-L", "-n", "-P", "-s", "--replace", "--delimiter",
                                 "--arg-file", "--max-args", "--max-procs", "--max-lines"]},
    "watch": {"flagsWithValue": ["-n", "--interval"], "shellString": "rest"},
    "script": {"shellString": "-c"},
    "eval": {"shellString": "rest"},
    **{shell: {"flagsWithValue": ["-o", "-O", "--rcfile", "--init-file"], "shellString": "-c"}
       for shell in ("bash", "sh", "zsh", "dash", "ksh")},
}

KEYS = ("flagsWithValue", "shellString", "skip", "assignments", "noCommandFlags")
MAX_SKIP = 5


def _flags(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x.startswith("-") and len(x) > 1 for x in value)


def validate(name: object, entry: object) -> None:
    """Raise ValueError when a wrapper name or entry is malformed."""
    if not isinstance(name, str) or not name or "/" in name or any(c.isspace() for c in name):
        raise ValueError(f"wrapper name {name!r} must be a command name without '/' or spaces")
    if not isinstance(entry, dict):
        raise ValueError(f"wrapper '{name}' must be a JSON object")  # noqa: TRY004
    unknown = set(entry) - set(KEYS) - {"setBy"}
    if unknown:
        raise ValueError(f"wrapper '{name}' has unknown keys: {', '.join(sorted(unknown))} (known: {', '.join(KEYS)})")
    for key in ("flagsWithValue", "noCommandFlags"):
        if key in entry and not _flags(entry[key]):
            raise ValueError(f"wrapper '{name}': '{key}' must be a list of flags such as \"-u\"")
    shell = entry.get("shellString")
    if "shellString" in entry and not (isinstance(shell, str) and (shell == "rest" or (
            shell.startswith("-") and len(shell) > 1))):
        raise ValueError(f"wrapper '{name}': 'shellString' must be a flag such as \"-c\" or \"rest\"")
    skip = entry.get("skip", 0)
    if "skip" in entry and not (isinstance(skip, int) and not isinstance(skip, bool) and 0 <= skip <= MAX_SKIP):
        raise ValueError(f"wrapper '{name}': 'skip' must be an integer from 0 to {MAX_SKIP}")
    if "assignments" in entry and not isinstance(entry["assignments"], bool):
        raise ValueError(f"wrapper '{name}': 'assignments' must be true or false")


def merge(base: Entry, extra: Entry) -> Entry:
    """Add look-through: flag lists union, scalars keep the existing value, 'assignments' can only be turned on."""
    out = dict(base)
    for key in ("flagsWithValue", "noCommandFlags"):
        if key in extra:
            out[key] = sorted({*out.get(key, []), *extra[key]})
    for key in ("shellString", "skip"):
        if key in extra and key not in out:
            out[key] = extra[key]
    if extra.get("assignments") is True:
        out["assignments"] = True
    return out


def effective(*layers: object) -> Table:
    """The built-in table plus each layer's 'wrappers' entries, earlier (higher) layers first; invalid ones are skipped."""
    table: Table = {name: dict(entry) for name, entry in DEFAULTS.items()}
    for layer in layers:
        entries = layer.get("wrappers") if isinstance(layer, dict) else None
        for name, entry in (entries.items() if isinstance(entries, dict) else ()):
            try:
                validate(name, entry)
            except ValueError:
                continue
            table[name] = merge(table.get(name, {}), entry)
    return table


def problems(where: str, layer: object) -> list[str]:
    entries = layer.get("wrappers") if isinstance(layer, dict) else None
    if entries is None:
        return []
    if not isinstance(entries, dict):
        return [f"{where}: 'wrappers' must be an object, so all its entries are ignored"]
    out = []
    for name, entry in entries.items():
        try:
            validate(name, entry)
        except ValueError as exc:
            out.append(f"{where}: {exc}, so it is ignored")
    return out
