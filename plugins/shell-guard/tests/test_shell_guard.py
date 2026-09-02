#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""Run: python3 tests/test_shell_guard.py  (exit 1 on any failure)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TypedDict, cast

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "shell-guard.sh"
CONFIGURE = ROOT / "skills" / "configure" / "scripts" / "configure.py"
sys.path.insert(0, str(ROOT / "hooks"))
from shellwords import simple_commands


class HookSpecific(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookOutput(TypedDict, total=False):
    hookSpecificOutput: HookSpecific
    systemMessage: str


def run(command: str, transcript: str = "", config: str = "/nonexistent/config.json") -> HookOutput | None:
    payload = {"tool_name": "Bash", "tool_input": {"command": command},
               "transcript_path": transcript, "session_id": "t"}
    env = {**os.environ, "SHELL_GUARD_CONFIG": config}
    out = subprocess.run(["bash", str(HOOK)], input=json.dumps(payload), env=env,
                         capture_output=True, text=True, check=True).stdout
    return cast(HookOutput, json.loads(out)) if out.strip() else None


def decision(command: str, **kw: str) -> str:
    out = run(command, **kw)
    if out is None:
        return "allow"
    if "hookSpecificOutput" in out:
        return out["hookSpecificOutput"]["permissionDecision"]
    return "warn"


def reason(command: str, **kw: str) -> str:
    out = run(command, **kw)
    assert out is not None
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def configure(*args: str, config: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SHELL_GUARD_CONFIG": config}
    return subprocess.run([sys.executable, str(CONFIGURE), *args], env=env, capture_output=True, text=True, check=False)


DENY = [
    "find . -name '*.py'",
    "find / -name foo 2>/dev/null",
    "/usr/bin/find . -type f",
    "sudo find /root -name x",
    "sudo -u core find /home -name x",
    "timeout 5 find . -name x",
    "nice -n 10 find .",
    "cd /tmp && find . -newer ref",
    "ls | grep foo; find . -name bar",
    "echo hi\nfind . -name x",
    "bash -c 'find . -name x'",
    "eval \"find . -name x\"",
    "for f in $(find . -name '*.log'); do echo $f; done",
    "count=$(find . -type f | wc -l)",
    "n=`find . -type f | wc -l`",
    "diff <(find a) <(find b)",
    "cat <<EOF > script.sh\necho hi\nEOF\nfind . -name x",
    "{ find . -name x; }",
    "echo $((1 << 3))\nfind . -name x",
    "command -p find .",
    "echo \"it's $(find . -name x) don't\"",
    "echo $'don\\'t' && find . -name x",
    "watch -n 5 'find .'",
    "script -c 'find .' out",
    "FIND_OK=0 find .",
    "FIND_OK= find .",
    "grep -r foo .",
    "grep -rn foo .",
    "grep -rniE 'foo|bar' src/",
    "grep --recursive foo .",
    "grep -d recurse foo .",
    "grep -d skip -r foo .",
    "grep --directories=recurse foo .",
    "grep -R foo .",
    "egrep -r foo .",
    "fgrep -rl foo .",
    "find . -type f | xargs grep -l foo",
    "find . -print0 | xargs -0 grep -rl foo",
    "xargs -I {} grep -r foo {} < list",
    "grep -e foo -r .",
    "grep -A3 -r foo .",
    "grep -m1 -r foo .",
    "grep -r -- foo",
    "ps aux | grep -r foo",
    "{ grep -r foo .; }",
    "FIND_OK=1 find . ; grep -r foo .",
    "pkill foo",
    "sudo pkill -f foo",
    "/usr/bin/pkill x",
    "bash -c 'pkill x'",
    "sleep 1; pkill x",
    "x=$(pkill -f y)",
    "{ pkill foo; }",
    "echo 'pkill x",  # unbalanced quotes: pkill falls back to the blunt pattern
]

ALLOW = [
    "fd -e py",
    "rg foo",
    "grep foo file.txt",
    "grep -n foo file.txt",
    "grep -e r file.txt",
    "grep -er file.txt",
    "grep -A3 foo file.txt",
    "grep -d skip foo *",
    "git grep -n foo",
    "echo find",
    "findmnt /mnt/media",
    "ls | grep find",
    "command -v find",
    "command -V find",
    "GREP_OK=1 grep -rn foo . > /dev/null; true",
    "FIND_OK=1 find . -perm 0644",
    "FIND_OK=yes find . -perm 0644",
    "env FIND_OK=1 find . -perm 0644",
    "sudo FIND_OK=1 find /root -perm 0644",
    "podman exec ctr find / -name x",
    "toolbox run find . -name x",
    "ssh host 'find . -name x'",
    "cat <<'EOF'\nfind . -name x\ngrep -r foo .\nEOF",
    "cat <<-EOF\n\tfind . -name x\n\tEOF",
    "python3 - <<EOF\nprint('find')\nEOF",
    "cat <<'EOF' > README.md\nUse `find`, `grep -r` or $(find x) here\nEOF",
    "echo 'run `find .` or $(grep -r x .) later'",
    "python3 - <<'PY'\ncmd = '''cd x && python3 - <<'EOF'\nprint(`find`)\nEOF\n'''\nPY",
    "echo 'grep -r foo' > /tmp/find",
    "ls > find",
    "cmd 2>&1 | tee find",
    "man find",
    "which find grep",
    "type -a find",
    "rg foo | grep -v bar",
    "echo 'find . -name x",  # unbalanced quotes: fd/rg rule stands down
    "pgrep foo",
    "kill 123",
]

WARN = ["echo pkill", "man pkill"]


def main() -> None:
    failures: list[str] = []
    for c in DENY:
        if (d := decision(c)) != "deny":
            failures.append(f"expected deny, got {d}: {c!r}")
    for c in ALLOW:
        if (d := decision(c)) != "allow":
            failures.append(f"expected allow, got {d}: {c!r}")
    for c in WARN:
        if (d := decision(c)) != "warn":
            failures.append(f"expected warn, got {d}: {c!r}")

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty.jsonl"
        empty.touch()
        r1 = reason("find . -name x", transcript=str(empty))
        leftovers = r1.replace("{}", "").replace("{/}", "").replace("{//}", "").replace("{.}", "")
        if "cheat sheet" not in r1 or "find . -name '*.py'" not in r1 or "{" in leftovers:
            failures.append("first offense should carry the full, fully-substituted sheet")
        seen = Path(tmp) / "seen.jsonl"
        seen.write_text(json.dumps({"type": "user", "message": {"content": r1}}) + "\n")
        r2 = reason("grep -r foo .", transcript=str(seen))
        if "find . -name '*.py'" in r2 or "shown earlier" not in r2:
            failures.append("second offense in the same transcript should be terse")
        if "FIND_OK=1" not in r1 or "GREP_OK=1" not in r2:
            failures.append("escape hatch must be named in the reason")
        r3 = reason("find .", transcript=str(Path(tmp) / "nope.jsonl"))
        if "cheat sheet" not in r3:
            failures.append("missing transcript should fall back to the full sheet")

        both = reason("find . | xargs grep -r foo")
        if "`find` →" not in both or "`grep -r` →" not in both:
            failures.append("both offenses should be named when both occur")

        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"rules": {"pkill": {"enabled": False}, "find": {"enabled": False}}}))
        if decision("pkill x", config=str(cfg)) != "allow":
            failures.append("config should disable the pkill rule")
        if decision("find .", config=str(cfg)) != "allow":
            failures.append("config should disable the find rule")
        if decision("grep -r x .", config=str(cfg)) != "deny":
            failures.append("grep rule should stay enabled when not mentioned in config")
        if decision("find . | xargs grep -r x", config=str(cfg)) != "deny":
            failures.append("disabled find must not mask an enabled grep in the same command")
        for bad in ("{not json", '{"rules": ["pkill"]}', '{"rules": {"pkill": "disabled"}}',
                    '{"rules": {"pkill": {"enabled": null}}}', '{"rules": {"pkill": {"enabled": "false"}}}', "[]"):
            cfg.write_text(bad)
            if decision("pkill x", config=str(cfg)) != "deny":
                failures.append(f"malformed config {bad!r} should fall back to enabled")

        assert configure("reset", config=str(cfg)).returncode == 0
        out = configure("disable", "grep", "pkill", config=str(cfg))
        if json.loads(cfg.read_text()) != {"rules": {"grep": {"enabled": False}, "pkill": {"enabled": False}}}:
            failures.append("configure disable should write exactly the toggled rules")
        if "grep   disabled" not in out.stdout or ("find   enabled" not in out.stdout and "find   inactive" not in out.stdout):
            failures.append(f"configure status table unexpected:\n{out.stdout}")
        if decision("grep -r x .", config=str(cfg)) != "allow" or decision("find .", config=str(cfg)) != "deny":
            failures.append("hook should honour the file configure.py wrote")
        assert configure("enable", "grep", config=str(cfg)).returncode == 0
        if decision("grep -r x .", config=str(cfg)) != "deny":
            failures.append("configure enable should re-enable the rule")
        for args in (("disable", "nope"), ("status", "extra"), ("reset", "find"), ("enable",), ("bogus",)):
            if configure(*args, config=str(cfg)).returncode != 2:
                failures.append(f"configure {' '.join(args)} should exit 2")
        if configure("--help", config=str(cfg)).returncode != 0:
            failures.append("configure --help should exit 0")
        if list(Path(tmp).glob("*.tmp")):
            failures.append("configure must not leave temp files behind")

    no_tools = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "shell_guard.py")],
        input=json.dumps({"tool_input": {"command": "find . | grep -r x ."}, "transcript_path": ""}),
        capture_output=True, text=True, check=False,
        env={**os.environ, "SHELL_GUARD_CONFIG": "/nonexistent", "PATH": "/nonexistent"}).stdout
    if no_tools.strip():
        failures.append("find/grep rules must stand down when neither fd nor rg is installed")

    for odd in ('{"tool_input": "notadict"}', '{"tool_input": {"command": ["find", "."]}}', "[]", "null"):
        r = subprocess.run([sys.executable, str(ROOT / "hooks" / "shell_guard.py")], input=odd,
                           capture_output=True, text=True, check=False,
                           env={**os.environ, "SHELL_GUARD_CONFIG": "/nonexistent"})
        if r.returncode != 0 or r.stdout.strip():
            failures.append(f"odd payload {odd!r} should be ignored cleanly, got rc={r.returncode} {r.stderr[-200:]}")

    names = [c.name for c in simple_commands("sudo -u core find /x -name y | xargs -I {} grep -r z {}")]
    if names != ["find", "grep"]:
        failures.append(f"parser: expected ['find','grep'], got {names}")

    for f in failures:
        print("FAIL:", f)
    print(f"{len(DENY) + len(ALLOW) + len(WARN)} command cases + scenario checks, {len(failures)} failures")
    sys.exit(1 if failures else 0)


main()
