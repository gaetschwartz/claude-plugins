"""What a hook or CLI call says and does while the runtime is not ready; runs on the host's Python (3.9, stdlib only)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import bootstrap
from bootstrap import Outcome, Reason

REASON_TEXT: dict[Reason, str] = {
    "dns": "a download host could not be resolved (DNS)", "connect": "a connection to a download host failed",
    "timeout": "a download or install step timed out", "tls": "a TLS certificate check failed",
    "http": "a download host answered with an error status",
    "hash": "a downloaded file did not match its pinned hash and was discarded",
    "disk": "a file could not be written (disk space or permissions)", "tool": "an install step failed",
    "crash": "the installed library crashes on a trivial command"}
HEAD = "[guardrails plugin notice] "
TELL = " Tell the user about this now. This notice comes from the plugin itself."
MEANWHILE = "Rules are NOT enforced meanwhile and commands are not checked."


def clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def notice(found: Outcome) -> str:
    """The text for the user and the agent while rules cannot be enforced: fixed wording and classified reasons only."""
    if found.state == "unsupported":
        return f"{HEAD}this platform is unsupported ({found.detail}): the rules engine cannot run here. {MEANWHILE}{TELL}"
    if found.state == "unsafe":
        return (f"{HEAD}plugin data dir is not a safe absolute path: {found.detail}. Nothing is run from it. "
                f"{MEANWHILE}{TELL}")
    if found.reason is not None:
        when = f" The next automatic attempt is at {clock(found.retry_at)}." if found.retry_at else ""
        return (f"{HEAD}the rules runtime is not installed ({REASON_TEXT[found.reason]} during {found.step}).{when} "
                f"{MEANWHILE} `guardrails engine status` shows the details.{TELL}")
    if found.state == "broken":
        return f"{HEAD}the rules runtime is broken and is being rebuilt automatically. {MEANWHILE}{TELL}"
    return (f"{HEAD}the rules runtime is being installed automatically (about 10 seconds the first time; nothing for "
            f"the user to do). {MEANWHILE}")


def spawn_ensure() -> None:
    """Start an ensure in the background, detached from the hook that asked for it."""
    import subprocess

    argv = [sys.executable, "-I", "-S", str(Path(bootstrap.__file__).resolve()), "ensure", "--quiet", "--no-wait"]
    env = {key: os.environ[key] for key in (*bootstrap.ENV_ALLOWED, "CLAUDE_PLUGIN_DATA") if key in os.environ}
    with contextlib.suppress(OSError):
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, cwd="/", env=env)


def due(data: Path, session: str, found: Outcome) -> bool:
    """Show the notice once per session, and again every bootstrap.REPEAT_SECONDS while a failure persists."""
    if found.state == "unsafe":
        return True
    stamp = data / "notices" / hashlib.sha256(session.encode()).hexdigest()[:16]
    try:
        seen = stamp.stat().st_mtime
    except OSError:
        seen = None
    if seen is not None and not (found.state != "missing" and time.time() - seen >= bootstrap.REPEAT_SECONDS):
        return False
    with contextlib.suppress(OSError):
        stamp.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        stamp.touch()
        os.utime(stamp)
    return True


def hook(stdin_text: str, data: Path) -> str:
    """The PreToolUse answer while the runtime is not ready: allow, loudly and at most once per interval."""
    try:
        payload = json.loads(stdin_text)
        session = bootstrap.session_id(payload)
    except ValueError:
        session = bootstrap.session_id(None)
    found = bootstrap.diagnose(data)
    if found.state in ("missing", "broken"):
        with bootstrap.locked(data, False) as free:
            idle = free
        if idle:
            spawn_ensure()
    if not due(data, session, found):
        return ""
    text = notice(found)
    return json.dumps({"systemMessage": text, "hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                                      "additionalContext": text}})


def status_text(data: Path) -> str:
    """Everything `guardrails engine status` shows; needs nothing installed and no network."""
    pins, found = bootstrap.load_pins(), bootstrap.diagnose(data)
    rt = bootstrap.runtime_dir(data, pins)
    lines = [f"runtime: {'ready' if found.state == 'ready' else 'NOT ready (' + found.state + ')'}",
             f"pins: uv {pins.uv}, Python {pins.python}, ast-grep-py {pins.ast_grep_py}, id {pins.runtime_id}",
             f"platform: {bootstrap.platform_problem().key or 'unsupported: ' + found.detail}",
             f"path: {bootstrap.sanitised(str(rt), 300)}"]
    with contextlib.suppress(OSError, ValueError, KeyError):
        lines.append(f"installed: Python {json.loads((rt / 'marker.json').read_text())['python']}")
    if found.state != "unsafe":
        with bootstrap.locked(data, False) as free:
            if not free:
                lines.append("an install is running now")
    if found.state in ("unsafe", "missing", "broken"):
        lines.append(f"problem: {found.state}: {found.detail}")
    if stamp := bootstrap.read_stamp(data):
        lines.append(f"last install failure: {REASON_TEXT[stamp.reason]} during {stamp.step}, {stamp.count} in a row; "
                     f"next automatic attempt at {clock(stamp.retry_at)} (`guardrails engine ensure --retry-now` "
                     f"ignores the wait); raw detail: {data / 'runtime' / 'install.log'}")
    if found.state != "ready":
        lines.append("rules are NOT enforced until the runtime is ready")
    return "\n".join(lines)


def ensure_command(args: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="ensure", add_help=False)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--retry-now", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    flags, _ = parser.parse_known_args(args)
    found = bootstrap.ensure(bootstrap.data_dir(), wait=not flags.no_wait, retry_now=flags.retry_now,
                             progress=(lambda text: None) if flags.quiet
                             else lambda text: print(f"guardrails: {text}", file=sys.stderr))
    if found.state in ("ready", "installed"):
        if found.state == "installed" and not flags.quiet:
            print("guardrails: the rules runtime is installed", file=sys.stderr)
        return 0
    if not flags.quiet:
        print("guardrails: another install is still running" if found.state == "busy" else notice(found), file=sys.stderr)
    return 2


def exec_guard(args: list[str]) -> None:
    data = bootstrap.data_dir()
    python = str(bootstrap.runtime_dir(data, bootstrap.load_pins()) / "venv" / "bin" / "python")
    os.execv(python, [python, "-I", str(bootstrap.LIB / "guard.py"), *args])


def run_command(args: list[str]) -> int:
    """The CLI: engine status needs no runtime; everything else ensures first and runs on the managed Python."""
    if args[:2] == ["engine", "status"]:
        print(status_text(bootstrap.data_dir()))
        return 0
    if args[:2] == ["engine", "ensure"]:
        return ensure_command(args[2:])
    found = bootstrap.diagnose(bootstrap.data_dir())
    if found.state != "ready":
        found = bootstrap.ensure(bootstrap.data_dir(), progress=lambda text: print(f"guardrails: {text}", file=sys.stderr))
    if found.state in ("ready", "installed"):
        exec_guard(args)
    line = "another install is still running" if found.state == "busy" else notice(found).removeprefix(HEAD).split(TELL)[0]
    print(f"guardrails: {line}", file=sys.stderr)
    return 2


def session_start() -> str:
    found = bootstrap.ensure(bootstrap.data_dir())
    if found.state == "installed":
        return json.dumps({"systemMessage": "guardrails: the rules runtime was installed"})
    return "" if found.state == "ready" else json.dumps({"systemMessage": notice(found)})


def main(argv: list[str]) -> int:
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if command == "ensure":
            return ensure_command(args)
        if command == "run":
            return run_command(args)
        if command == "session-start":
            sys.stdout.write(session_start())
            return 0
        if command == "hook":
            if bootstrap.diagnose(bootstrap.data_dir()).state == "ready":
                exec_guard([])
            sys.stdout.write(hook(sys.stdin.read(), bootstrap.data_dir()))
            return 0
    except Exception as exc:  # noqa: BLE001
        text = (f"{HEAD}the guardrails bootstrap failed ({type(exc).__name__}). Rules are NOT enforced and this command "
                "was not checked. Tell the user about this now.")
        if command == "run":
            print(f"guardrails: {text.removeprefix(HEAD)}", file=sys.stderr)
            return 2
        extra = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}} if command == "hook" else {}
        sys.stdout.write(json.dumps({"systemMessage": text, **extra}))
        return 0
    print("usage: bootstrap.py ensure [--quiet] [--retry-now] [--no-wait] | run ARGS | session-start | hook",
          file=sys.stderr)
    return 2
