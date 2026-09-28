#!/usr/bin/env python3
"""Report chezmoi drift that a tool call caused.

PreToolUse snapshots `chezmoi status`; PostToolUse snapshots it again and reports only
entries that appeared or changed in between. `chezmoi status` is authoritative
regardless of HOW a file was changed - Edit/Write, a shell redirect, sed -i, a script.

Each call records its time window in a shared directory so a change made while two
sessions were both running a tool can be attributed to the right one.

Enabled by default. Turn it off by setting "enabled": false in the state file
(see state_path()), or with CHEZMOI_DRIFT_HOOK=0, which overrides the file.

Also usable as a CLI: --status, --enable, --disable [reason].
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

TIMEOUT = 10
MAX_DETAIL = 8
CLOSED_WINDOW_TTL = 600
OPEN_WINDOW_TTL = 3600
SESSION_TTL = 7 * 86400
EDGE_SLACK = 0.5

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}

# `chezmoi status` second-column codes: effect of running `chezmoi apply`.
APPLY_EFFECT = {
    "A": "would be created by apply",
    "D": "would be deleted by apply",
    "M": "would be overwritten by apply",
    "R": "script would run on apply",
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def state_path():
    override = os.environ.get("CHEZMOI_DRIFT_STATE")
    if override:
        return override
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "chezmoi-drift", "state.json")


def state_subdir(name):
    return os.path.join(os.path.dirname(state_path()), name)


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_-]", "_", value or "none")


def read_json(path, default):
    try:
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, type(default)):
            return data
    except (OSError, ValueError):
        pass
    return default


def write_json(path, data):
    """Atomic replace; concurrent hook invocations must not truncate each other."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def read_state():
    return read_json(state_path(), {})


def is_enabled(state):
    env = os.environ.get("CHEZMOI_DRIFT_HOOK")
    if env is not None and env != "":
        return env.strip().lower() in {"1", "true", "yes", "on"}
    return state.get("enabled", True) is not False


def emit(event, context):
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def chezmoi(*args):
    """--no-tty and --skip-secrets keep a template that reads a password manager from
    blocking on an interactive unlock prompt."""
    try:
        p = subprocess.run(
            ["chezmoi", "--no-pager", "--color=false", "--no-tty", "--skip-secrets", *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, p.stdout.rstrip("\n")


def parse(status_text):
    for line in status_text.splitlines():
        if len(line) < 3:
            continue
        yield line[0], line[1], line[2:].strip()


def fstat(path):
    if not path:
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return [st.st_mtime, st.st_size]


def source_paths(targets):
    if not targets:
        return {}
    code, out = chezmoi("source-path", "--", *targets)
    lines = out.splitlines()
    if code != 0 or len(lines) != len(targets):
        return {}
    return dict(zip(targets, lines))


def snapshot():
    code, status = chezmoi("status", "--path-style=absolute")
    if code != 0:
        return None
    codes = {path: first + second for first, second, path in parse(status)}
    sources = source_paths(list(codes))
    return {
        path: {
            "codes": c,
            "source": sources.get(path),
            "dest": fstat(path),
            "src": fstat(sources.get(path)),
        }
        for path, c in codes.items()
    }


def tool_target(payload):
    tool_input = payload.get("tool_input") or {}
    if payload.get("tool_name") in EDIT_TOOLS:
        return tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    return tool_input.get("command") or ""


def prune_windows(directory):
    try:
        names = os.listdir(directory)
    except OSError:
        return
    cutoff = time.time()
    for name in names:
        path = os.path.join(directory, name)
        win = read_json(path, {})
        end, start = win.get("end"), win.get("start", 0)
        if (end and end < cutoff - CLOSED_WINDOW_TTL) or start < cutoff - OPEN_WINDOW_TTL:
            remove(path)


def other_windows(directory, session_id):
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    windows = []
    for name in names:
        win = read_json(os.path.join(directory, name), {})
        if win and win.get("session") != session_id:
            windows.append(win)
    return windows


def mentions(win, names):
    if win.get("tool") in EDIT_TOOLS:
        return win.get("target") in names
    command = win.get("target") or ""
    home = os.path.expanduser("~")
    for name in names:
        tilde = "~" + name[len(home):] if name.startswith(home + os.sep) else name
        if name in command or tilde in command or os.path.basename(name) in command:
            return True
    return False


def attribute(entry, path, me, others, end):
    """Return "exact", "likely", or None when the change belongs to another session."""
    names = {path, entry.get("source")} - {None}
    if me["tool"] in EDIT_TOOLS and me["target"] in names:
        return "exact"

    stamps = [s[0] for s in (entry.get("dest"), entry.get("src")) if s]
    lo, hi = (max(stamps), max(stamps)) if stamps else (me["start"], end)
    overlapping = [
        w for w in others
        if w.get("start", 0) - EDGE_SLACK <= hi
        and lo <= (w.get("end") or time.time()) + EDGE_SLACK
    ]
    if not overlapping:
        return "likely"
    if any(w.get("tool") in EDIT_TOOLS and mentions(w, names) for w in overlapping):
        return None
    if me["tool"] in EDIT_TOOLS:
        return None
    if any(mentions(w, names) for w in overlapping) and not mentions(me, names):
        return None
    return "likely"


def describe(first, second, path, source):
    effect = APPLY_EFFECT.get(second, "")

    if second == "R":
        return f"- `{path}`: {effect}."

    # First column set means the destination changed after chezmoi last wrote it,
    # i.e. something edited the deployed file rather than the source.
    if first in ("M", "A", "D"):
        if not source:
            return f"- `{path}`: destination edited{'; it ' + effect if effect else ''}."
        if source.endswith(".tmpl"):
            fix = (
                f"its source is a TEMPLATE (`{source}`), so `chezmoi re-add` will "
                f"silently skip it - port the edit into the template, then "
                f"`chezmoi apply {path}`"
            )
        else:
            fix = f"persist with `chezmoi re-add {path}`"
        tail = f", or leave it and it {effect}" if effect else ""
        return f"- `{path}`: destination edited; {fix}{tail}."

    return f"- `{path}`: source is ahead; it {effect}." if effect else f"- `{path}`: drifted."


def git_note():
    code, out = chezmoi("dump-config", "--format=json")
    if code != 0:
        return ""
    try:
        git = {k.lower(): v for k, v in (json.loads(out).get("git") or {}).items()}
    except ValueError:
        return ""
    if git.get("autocommit") and git.get("autopush"):
        return " Note: git.autoCommit and autoPush are on, so add/re-add will also commit and push."
    if git.get("autocommit"):
        return " Note: git.autoCommit is on, so add/re-add will also commit."
    return ""


def window_path(payload):
    return os.path.join(state_subdir("windows"), safe_name(payload.get("tool_use_id")) + ".json")


def pre(payload):
    snap = snapshot()
    if snap is None:
        return
    write_json(
        window_path(payload),
        {
            "session": payload.get("session_id"),
            "tool": payload.get("tool_name"),
            "target": tool_target(payload),
            "start": time.time(),
            "end": None,
            "snapshot": snap,
        },
    )


def post(payload):
    path_of_window = window_path(payload)
    me = read_json(path_of_window, {})
    if not me:
        return
    before = me.pop("snapshot", {})
    after = snapshot()
    end = time.time()
    me["end"] = end
    write_json(path_of_window, me)
    prune_windows(state_subdir("windows"))
    if after is None:
        return

    session_id = payload.get("session_id")
    session_path = os.path.join(state_subdir("sessions"), safe_name(session_id) + ".json")
    recorded = read_json(session_path, {})
    notified = {p: c for p, c in recorded.items() if p in after}

    others = other_windows(state_subdir("windows"), session_id)
    reported = []
    for path, entry in after.items():
        if before.get(path) == entry or notified.get(path) == entry["codes"]:
            continue
        verdict = attribute(entry, path, me, others, end)
        if verdict:
            reported.append((path, entry, verdict))
            notified[path] = entry["codes"]

    if notified != recorded:
        write_json(session_path, notified)
    if not reported:
        return

    lines = [
        describe(e["codes"][0], e["codes"][1], p, e.get("source"))
        for p, e, _ in reported[:MAX_DETAIL]
    ]
    if len(reported) > MAX_DETAIL:
        lines.append(f"- ...and {len(reported) - MAX_DETAIL} more (`chezmoi status`).")

    unsure = [p for p, _, verdict in reported if verdict != "exact"]
    if unsure and len(unsure) == len(reported):
        disclaimer = " If you didn't change these files, ignore this message."
    elif unsure:
        disclaimer = " If you didn't change " + ", ".join(f"`{p}`" for p in unsure) + ", ignore that part."
    else:
        disclaimer = ""

    emit(
        payload.get("hook_event_name") or "PostToolUse",
        "That tool call left chezmoi-managed files out of sync with their source:\n"
        + "\n".join(lines)
        + f"\n\nReview with `chezmoi diff` before acting.{git_note()} Raise this with "
        "the user rather than running a mutating chezmoi command unprompted."
        + disclaimer,
    )


def prune_sessions():
    directory = state_subdir("sessions")
    try:
        names = os.listdir(directory)
    except OSError:
        return
    cutoff = time.time() - SESSION_TTL
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < cutoff:
                remove(path)
        except OSError:
            pass


def cli(argv):
    state = read_state()
    cmd = argv[0]
    if cmd == "--status":
        env = os.environ.get("CHEZMOI_DRIFT_HOOK")
        print(f"state file: {state_path()}")
        print(f"enabled:    {is_enabled(state)}")
        if env:
            print(f"            (forced by CHEZMOI_DRIFT_HOOK={env})")
        if state.get("disabledReason"):
            print(f"reason:     {state['disabledReason']}")
        print(f"updatedAt:  {state.get('updatedAt', '-')}")
        return 0
    if cmd in ("--enable", "--disable"):
        state.pop("sessions", None)
        state["enabled"] = cmd == "--enable"
        state["updatedAt"] = now()
        reason = " ".join(argv[1:]).strip()
        if cmd == "--disable":
            state["disabledReason"] = reason or "disabled by user"
        else:
            state.pop("disabledReason", None)
        write_json(state_path(), state)
        print(f"chezmoi drift hook {'enabled' if state['enabled'] else 'disabled'}")
        return 0
    print(__doc__)
    return 2


def main():
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))

    if not is_enabled(read_state()) or not shutil.which("chezmoi"):
        return
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return

    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        pre(payload)
    elif event in ("PostToolUse", "PostToolUseFailure"):
        post(payload)
        prune_sessions()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        pass
