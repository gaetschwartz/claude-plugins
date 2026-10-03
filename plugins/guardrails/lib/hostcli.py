"""What a hook or CLI call says and does while the runtime is not ready; runs on the host's Python (3.9, stdlib only)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import bootstrap
import installer
import telemetry
from bootstrap import Outcome

HEAD = "[guardrails plugin notice] "
TELL = " Tell the user about this now. This notice comes from the plugin itself."
MEANWHILE = "Rules are NOT enforced meanwhile and commands are not checked."
TEXTS = {
    "unsupported": "this platform is unsupported ({detail}): the rules engine cannot run here. " + MEANWHILE + TELL,
    "unsafe": ("the plugin data directory is not a safe absolute path ({detail}). No plugin code is run from it. " + MEANWHILE +
               TELL),
    "backoff": ("the rules runtime is not installed ({detail}). The next automatic attempt is at {when}. " + MEANWHILE +
                " `guardrails engine status` shows the details." + TELL),
    "missing": ("the rules runtime is being installed automatically (about 10 seconds the first time; nothing for the "
                "user to do). " + MEANWHILE),
}
TEXTS["failed"] = TEXTS["backoff"]


def clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def notice(found: Outcome) -> str:
    """The text for the user and the agent while rules cannot be enforced: fixed wording only."""
    return HEAD + TEXTS.get(found.state, TEXTS["missing"]).format(detail=found.detail, when=clock(found.retry_at))


def spawn_ensure() -> None:
    """Start an ensure in the background, detached from the hook that asked for it."""
    argv = [sys.executable, "-I", "-S", str(Path(bootstrap.__file__).resolve()), "ensure", "--quiet", "--no-wait"]
    env = {key: os.environ[key] for key in (*installer.ENV_ALLOWED, "CLAUDE_PLUGIN_DATA") if key in os.environ}
    with contextlib.suppress(OSError):
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, cwd="/", env=env)


def due(data: Path, session: str, found: Outcome) -> bool:
    """Show the notice once per session, and again every REPEAT_SECONDS while a failure persists."""
    if found.state == "unsafe":
        return True
    stamp = data / "notices" / hashlib.sha256(session.encode()).hexdigest()[:16]
    try:
        age = time.time() - stamp.stat().st_mtime
    except OSError:
        age = None
    if age is not None and (found.state == "missing" or age < bootstrap.REPEAT_SECONDS):
        return False
    with contextlib.suppress(OSError):
        stamp.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        stamp.touch()
        os.utime(stamp)
    return True


def hook(stdin_text: str, data: Path) -> str:
    """The PreToolUse answer while the runtime is not ready: allow, loudly and at most once per interval."""
    try:
        session = bootstrap.session_id(json.loads(stdin_text))
    except ValueError:
        session = bootstrap.session_id(None)
    found = bootstrap.diagnose(data)
    if found.state == "missing":
        with bootstrap.locked(data, False) as idle:
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
    lines = [f"runtime: {'ready' if found.state == 'ready' else 'NOT ready (' + found.state + ')'}",
             f"pins: uv {pins.uv}, Python {pins.python}, ast-grep-py {pins.ast_grep_py}, id {pins.runtime_id}",
             f"platform: {bootstrap.platform_problem().key or 'unsupported: ' + found.detail}"]
    with contextlib.suppress(OSError, ValueError, KeyError):
        lines.append(f"installed: Python {json.loads((bootstrap.runtime_dir(data, pins) / 'marker.json').read_text())['python']}")
    if found.state != "unsafe":
        with bootstrap.locked(data, False) as idle:
            if not idle:
                lines.append("an install is running now")
    if found.state in ("unsafe", "missing"):
        lines.append(f"problem: {found.state}: {found.detail}")
    if stamp := bootstrap.read_stamp(data):
        lines.append(f"last install failure: {stamp.why}, {stamp.count} in a row; next automatic attempt at "
                     f"{clock(stamp.retry_at)} (`guardrails engine ensure --retry-now` ignores the wait); raw detail: "
                     "runtime/install.log in the plugin data directory")
    if found.state != "ready":
        lines.append("rules are NOT enforced until the runtime is ready")
    return "\n".join(lines)


def exec_guard(args: list[str]) -> None:
    data = bootstrap.data_dir()
    python = str(bootstrap.runtime_dir(data, bootstrap.load_pins()) / "venv" / "bin" / "python")
    os.execv(python, [python, "-I", str(bootstrap.LIB / "guard.py"), *args])


def say(text: str) -> None:
    print(f"guardrails: {text}", file=sys.stderr)


def ensure_command(args: list[str]) -> int:
    quiet = "--quiet" in args
    data = bootstrap.data_dir()
    if not quiet and bootstrap.diagnose(data).state == "missing":
        say("installing the rules runtime (about 10 seconds the first time)")
    found = bootstrap.ensure(data, wait="--no-wait" not in args, retry_now="--retry-now" in args)
    if found.state in ("ready", "installed"):
        return 0
    if not quiet:
        say("another install is still running" if found.state == "busy" else notice(found))
    return 2


def run_command(args: list[str]) -> int:
    """The CLI: engine status needs no runtime; everything else ensures first and runs on the managed Python."""
    data = bootstrap.data_dir()
    if args[:2] == ["engine", "status"]:
        print(status_text(data))
        say(f"runtime {bootstrap.sanitised(str(bootstrap.runtime_dir(data, bootstrap.load_pins())), 300)}, install log "
            f"{bootstrap.sanitised(str(data / 'runtime' / 'install.log'), 300)}")
        return 0
    if args[:2] == ["engine", "ensure"]:
        return ensure_command(args[2:])
    found = bootstrap.diagnose(data)
    if found.state == "missing":
        say("installing the rules runtime (about 10 seconds the first time)")
    if found.state != "ready":
        found = bootstrap.ensure(data)
    if found.state in ("ready", "installed"):
        exec_guard(args)
    say("another install is still running" if found.state == "busy" else notice(found).removeprefix(HEAD).split(TELL)[0])
    return 2


def session_start() -> str:
    found = bootstrap.ensure(bootstrap.data_dir())
    telemetry.prune(bootstrap.data_dir())
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
            say(text.removeprefix(HEAD))
            return 2
        extra = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}} if command == "hook" else {}
        sys.stdout.write(json.dumps({"systemMessage": text, **extra}))
        return 0
    say("usage: bootstrap.py ensure [--quiet] [--retry-now] [--no-wait] | run ARGS | session-start | hook")
    return 2
