#!/usr/bin/env python3
"""Keep a tgrep server warm for the repository a Claude Code session runs in.

SessionStart: if the session's cwd is inside a git repository, start `tgrep serve <root>`
unless `tgrep status <root>` already reports a reachable server. SessionEnd: stop the
server this hook started once no other tracked session uses that root.

Enabled by default. Turn it off by setting "enabled": false in the state file (see
state_path()), or with TGREP_SERVE_HOOK=0, which overrides the file. Individual
repositories can be kept out with "excludedRoots".

Also usable as a CLI: --status, --enable, --disable [reason], --exclude [root],
--include [root], --stop [root].
"""

import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

TIMEOUT = 10
READY_WAIT = 3.0
READY_POLL = 0.25
SESSION_TTL_DAYS = 7


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def state_path():
    override = os.environ.get("TGREP_SERVE_STATE")
    if override:
        return override
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "tgrep-serve", "state.json")


def read_state():
    try:
        with open(state_path()) as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {}


def write_state(state):
    """Atomic replace; concurrent hook invocations must not truncate each other."""
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def is_enabled(state):
    env = os.environ.get("TGREP_SERVE_HOOK")
    if env is not None and env != "":
        return env.strip().lower() in {"1", "true", "yes", "on"}
    return state.get("enabled", True) is not False


def run(argv, cwd=None):
    try:
        p = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, (p.stdout + p.stderr).rstrip("\n")


def git_root(cwd):
    code, out = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    if code != 0 or not out:
        return None
    return os.path.realpath(out.splitlines()[0])


def tgrep_ignored(root):
    code, _ = run(["git", "-C", root, "check-ignore", "-q", ".tgrep"])
    return code == 0


def server_status(root):
    """Ask tgrep itself; it only reports a server it could actually reach."""
    code, out = run(["tgrep", "status", root])
    info = {"running": False, "indexed": False, "pid": None, "port": None, "indexing": None}
    if code != 0:
        return info
    info["running"] = out.startswith("Server status for")
    info["indexed"] = info["running"] or out.startswith("Index status for")
    for line in out.splitlines():
        key, _, value = line.strip().partition(":")
        value = value.strip()
        if key == "PID" and value.isdigit():
            info["pid"] = int(value)
        elif key == "Port" and value.isdigit():
            info["port"] = int(value)
        elif key == "Indexing":
            info["indexing"] = value
    return info


def spawn_server(root, extra_args):
    """Detached in its own session so it outlives the hook process."""
    try:
        subprocess.Popen(
            ["tgrep", "serve", root, *extra_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=root,
            start_new_session=True,
        )
    except OSError:
        return None
    deadline = time.monotonic() + READY_WAIT
    while time.monotonic() < deadline:
        info = server_status(root)
        if info["running"]:
            return info
        time.sleep(READY_POLL)
    return server_status(root)


def stop_server(root, pid):
    """Only signal a pid that tgrep status currently attributes to this root."""
    info = server_status(root)
    if not info["running"] or info["pid"] != pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def prune_sessions(sessions):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=SESSION_TTL_DAYS
    )
    kept = {}
    for sid, seen_at in (sessions or {}).items():
        try:
            seen = datetime.datetime.fromisoformat(seen_at)
        except (TypeError, ValueError):
            continue
        if seen >= cutoff:
            kept[sid] = seen_at
    return kept


def emit(event, context):
    json.dump(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
        sys.stdout,
    )
    sys.exit(0)


def session_start(state, payload):
    if not shutil.which("tgrep"):
        return
    root = git_root(payload.get("cwd") or os.getcwd())
    if not root or root in (state.get("excludedRoots") or []):
        return

    servers = state.setdefault("servers", {})
    entry = servers.get(root) or {}
    session_id = payload.get("session_id") or "nosession"

    before = server_status(root)
    if before["running"]:
        info = before
        owned = entry.get("owned", False) and entry.get("pid") == info["pid"]
        verb = "already running"
    else:
        info = spawn_server(root, state.get("serveArgs") or [])
        owned = True
        verb = "started"

    sessions = prune_sessions(entry.get("sessions"))
    sessions[session_id] = now()
    servers[root] = {
        "pid": info["pid"],
        "port": info["port"],
        "owned": owned,
        "startedAt": entry.get("startedAt") if verb == "already running" else now(),
        "sessions": sessions,
    }
    state.setdefault("enabled", True)
    write_state(state)

    if not info["running"]:
        emit(
            "SessionStart",
            f"tgrep serve was launched for `{root}` but has not answered `tgrep status` yet. "
            "Check `tgrep status .` from the repo root before relying on indexed searches.",
        )

    lines = [
        f"tgrep server {verb} for `{root}` (pid {info['pid']}, port {info['port']}). "
        "Search with `tgrep [flags] -- <pattern> <root>` (see the tgrep:search skill)."
    ]
    if not before["indexed"]:
        lines.append(
            "No index existed, so the first build is running in the background: searches "
            "return nothing until `tgrep status .` shows `Indexing: complete`; pass "
            "`--no-index` if a search cannot wait."
        )
    elif info["indexing"] and info["indexing"] != "complete":
        lines.append(f"Indexing: {info['indexing']} — results are partial until it completes.")
    if not tgrep_ignored(root):
        lines.append("`.tgrep/` is not gitignored in this repository; add it before committing.")
    emit("SessionStart", "\n".join(lines))


def session_end(state, payload):
    root = git_root(payload.get("cwd") or os.getcwd())
    servers = state.get("servers") or {}
    entry = servers.get(root) if root else None
    if not entry:
        return
    session_id = payload.get("session_id") or "nosession"
    sessions = prune_sessions(entry.get("sessions"))
    sessions.pop(session_id, None)
    entry["sessions"] = sessions
    # /clear fires SessionEnd then SessionStart; keep the server warm across it.
    if not sessions and payload.get("reason") != "clear":
        if entry.get("owned") and entry.get("pid"):
            stop_server(root, entry["pid"])
        servers.pop(root, None)
    write_state(state)


def cli(argv):
    state = read_state()
    cmd = argv[0]
    rest = " ".join(argv[1:]).strip()
    if cmd == "--status":
        env = os.environ.get("TGREP_SERVE_HOOK")
        print(f"state file: {state_path()}")
        print(f"enabled:    {is_enabled(state)}")
        if env:
            print(f"            (forced by TGREP_SERVE_HOOK={env})")
        if state.get("disabledReason"):
            print(f"reason:     {state['disabledReason']}")
        print(f"updatedAt:  {state.get('updatedAt', '-')}")
        print(f"serveArgs:  {' '.join(state.get('serveArgs') or []) or '-'}")
        excluded = state.get("excludedRoots") or []
        print(f"excluded:   {', '.join(excluded) if excluded else '-'}")
        servers = state.get("servers") or {}
        print(f"servers:    {len(servers)} tracked")
        for root, entry in sorted(servers.items()):
            live = server_status(root)
            liveness = (
                f"live pid {live['pid']} port {live['port']}, indexing {live['indexing']}"
                if live["running"]
                else "not running"
            )
            n = len(prune_sessions(entry.get("sessions")))
            print(
                f"  {root}\n    {liveness}; {'started by hook' if entry.get('owned') else 'external'}; "
                f"{n} session(s)"
            )
        return 0
    if cmd in ("--enable", "--disable"):
        state["enabled"] = cmd == "--enable"
        state["updatedAt"] = now()
        if cmd == "--disable":
            state["disabledReason"] = rest or "disabled by user"
        else:
            state.pop("disabledReason", None)
        write_state(state)
        print(f"tgrep serve hook {'enabled' if state['enabled'] else 'disabled'}")
        return 0
    if cmd in ("--exclude", "--include", "--stop"):
        root = git_root(rest or os.getcwd())
        if not root:
            print("not inside a git repository; pass the repository root explicitly")
            return 2
        excluded = [r for r in (state.get("excludedRoots") or []) if r != root]
        if cmd == "--exclude":
            excluded.append(root)
            state["excludedRoots"] = sorted(excluded)
            state["updatedAt"] = now()
            write_state(state)
            print(f"excluded {root}")
            entry = (state.get("servers") or {}).get(root)
            if entry and entry.get("owned"):
                print("a hook-started server is still up; --stop to end it now")
            return 0
        if cmd == "--include":
            state["excludedRoots"] = sorted(excluded)
            state["updatedAt"] = now()
            write_state(state)
            print(f"included {root}")
            return 0
        entry = (state.get("servers") or {}).pop(root, None)
        live = server_status(root)
        if not live["running"]:
            write_state(state)
            print(f"no server running for {root}")
            return 0
        if stop_server(root, live["pid"]):
            write_state(state)
            print(f"stopped tgrep server for {root} (pid {live['pid']})")
            return 0
        print(f"could not stop pid {live['pid']}")
        return 1
    print(__doc__)
    return 2


def main():
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))

    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    state = read_state()
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        if is_enabled(state):
            session_start(state, payload)
    elif event == "SessionEnd":
        session_end(state, payload)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        pass
