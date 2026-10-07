from __future__ import annotations

import json
import os

from helpers import RealRuntime

DENY = [
    "pkill foo",
    "sudo pkill -f foo",
    "/usr/bin/pkill x",
    "bash -c 'pkill x'",
    "sleep 1; pkill x",
    "x=$(pkill -f y)",
    "{ pkill foo; }",
    "ssh host pkill x",
    "watch 'pkill x'",
    "killall Finder",
    "strings /bin/ls",
    "strings -a libfoo.dylib | grep version",
]

ALLOW = [
    "fd -e py",
    "rg foo",
    "grep foo file.txt",
    "grep -n foo file.txt",
    "grep -e r file.txt",
    "grep -A3 foo file.txt",
    "grep -d skip foo *",
    "git grep -n foo",
    "echo find",
    "findmnt /mnt/media",
    "ls | grep find",
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
    "pgrep foo",
    "kill 123",
    "echo pkill",
    "man pkill",
    "man strings",
    "cat <<'EOF' > notes.md\nstrings are fun\nEOF",
]

WARN = [
    "watch -n 5 'find .'",
    "su -c 'find .'",
    "kill -9 123",
    "kill -s KILL 42",
    "nm -g libfoo.dylib",
    "otool -L /bin/ls",
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
    "FIND_OK=1 find . -perm 0644",
    "grep -r foo .",
    "grep -rn foo .",
    "grep -rniE 'foo|bar' src/",
    "grep --recursive foo .",
    "grep -d recurse foo .",
    "grep -d skip -r foo .",
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
]

# Known limits of matching by the real tree: each is documented in references/matching.md.
OVERBROAD = ["command -v find", "command -V find", "sudo grep find file"]
UNSEEN = ["$'fi\\x6ed' . -name x"]
UNPARSED = ["echo 'find . -name x"]


class Corpus(RealRuntime):
    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for tool in ("fd", "rg"):
            path = bin_dir / tool
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        for preset in ("modern-cli", "process-safety", "docs-first"):
            self.assertEqual(self.cli("preset", "install", preset)[0], 0)
        self.count = 0

    def decide(self, command: str) -> str:
        self.count += 1
        payload = json.dumps({"session_id": f"corpus-{self.count}", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": command}})
        proc = self.run_guard(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout.strip():
            return "allow"
        hs = json.loads(proc.stdout).get("hookSpecificOutput", {})
        return hs.get("permissionDecision") or ("warn" if "additionalContext" in hs else "other")

    def test_corpus(self) -> None:
        for expected, commands in (("deny", DENY), ("allow", ALLOW), ("warn", WARN)):
            for command in commands:
                with self.subTest(expected=expected, command=command):
                    self.assertEqual(self.decide(command), expected)

    def test_known_limits(self) -> None:
        for command in OVERBROAD:
            with self.subTest(overbroad=command):
                self.assertNotEqual(self.decide(command), "allow")
        for command in UNSEEN:
            with self.subTest(unseen=command):
                self.assertEqual(self.decide(command), "allow")
        for command in UNPARSED:
            with self.subTest(unparsed=command):
                self.assertEqual(self.decide(command), "warn")
