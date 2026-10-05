from __future__ import annotations

import json
import os
import time
import unittest
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import audit
from helpers import Isolated

BASE = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC).timestamp()
DENIAL = "[guardrails:no-pkill] Killing by name can hit the wrong process."


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def entry(role: str, content: Any, epoch: float, **extra: Any) -> str:
    base = {"type": role, "message": {"role": role, "content": content}, "timestamp": stamp(epoch), "sessionId": "sess-1",
            "cwd": "/work/app", "isSidechain": False}
    return json.dumps({**base, **extra})


def use(tid: str, command: str, epoch: float, tool: str = "Bash", **extra: Any) -> str:
    return entry("assistant", [{"type": "tool_use", "id": tid, "name": tool, "input": {"command": command}}], epoch, **extra)


def result(tid: str, text: Any, epoch: float, error: bool = True, **extra: Any) -> str:
    return entry("user", [{"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": text}], epoch, **extra)


def chat(role: str, text: str, epoch: float) -> str:
    return entry(role, [{"type": "text", "text": text}], epoch)


def denied(tid: str, epoch: float, command: str = "pkill node", text: str = DENIAL, tool: str = "Bash",
           prefix: bool = True, **extra: Any) -> list[str]:
    shown = f"PreToolUse:{tool} hook error: {text}" if prefix else text
    return [use(tid, command, epoch, tool, **extra), result(tid, shown, epoch + 1, **extra)]


class Fixture(Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(warnings.catch_warnings(action="ignore", category=DeprecationWarning))
        self.config = self.tmp / "claude"
        self.root = self.config / "projects"
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)}))

    def write(self, rel: str, lines: list[str], mtime: float | None = None) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        last = mtime if mtime is not None else BASE + 10_000
        os.utime(path, (last, last))
        return path

    def find(self, limit: int = 10, workers: int = 1, **query: Any) -> audit.Report:
        return audit.find_denials(self.root, limit, audit.Query(**query), workers)

    def tree(self) -> dict[str, tuple[int, float]]:
        return {str(p): (p.stat().st_size, p.stat().st_mtime) for base in (self.tmp,) for p in base.rglob("*") if p.is_file()}


class Extraction(Fixture):
    def test_denial_in_string_content_with_and_without_the_hook_error_prefix(self) -> None:
        self.write("p/a.jsonl", [*denied("t1", BASE), *denied("t2", BASE + 10, prefix=False)])
        hits = self.find().hits
        self.assertEqual([(h["tool_use_id"], h["rule"], h["tool"]) for h in hits],
                         [("t2", "no-pkill", "Bash"), ("t1", "no-pkill", "Bash")])
        self.assertEqual(hits[1]["command"], "pkill node")
        self.assertTrue(hits[1]["message"].startswith("PreToolUse:Bash hook error: [guardrails:no-pkill]"))

    def test_denial_in_list_content(self) -> None:
        text = [{"type": "text", "text": f"PreToolUse:Bash hook error: {DENIAL}"}]
        self.write("p/a.jsonl", [use("t1", "pkill x", BASE), result("t1", text, BASE + 1)])
        self.assertEqual([h["tool_use_id"] for h in self.find().hits], ["t1"])

    def test_monitor_denials_in_both_shapes(self) -> None:
        self.write("p/a.jsonl", [*denied("m1", BASE, "until pgrep x; do sleep 1; done", tool="Monitor"),
                                 *denied("m2", BASE + 10, "while pgrep y; do :; done", tool="Monitor", prefix=False)])
        self.assertEqual([(h["tool_use_id"], h["tool"]) for h in self.find().hits], [("m2", "Monitor"), ("m1", "Monitor")])

    def test_a_marker_quoted_in_other_output_never_matches(self) -> None:
        quoted = [use("r1", "cat notes.md", BASE, tool="Read"), result("r1", DENIAL, BASE + 1),
                  use("c1", "cat rules.md", BASE + 2), result("c1", DENIAL, BASE + 3, error=False),
                  use("c2", "cat rules.md", BASE + 4), result("c2", f"Exit code 1\n{DENIAL}", BASE + 5),
                  use("c3", "cat rules.md", BASE + 6), result("c3", f"see {DENIAL} above", BASE + 7),
                  use("c4", "cat rules.md", BASE + 8), result("c4", f"x PreToolUse:Bash hook error: {DENIAL}", BASE + 9),
                  chat("assistant", f"PreToolUse:Bash hook error: {DENIAL}", BASE + 10)]
        self.write("p/a.jsonl", quoted)
        self.assertEqual(self.find().hits, [])

    def test_other_hook_errors_and_a_mismatched_tool_are_not_guardrails_denials(self) -> None:
        cases = [use("a", "x", BASE), result("a", "PreToolUse:Bash hook error: [other-plugin:x] nope", BASE + 1),
                 use("b", "x", BASE + 2), result("b", f"PreToolUse:Write hook error: {DENIAL}", BASE + 3),
                 use("c", "x", BASE + 4), result("c", "[guardrails] Denied: command too large to check", BASE + 5),
                 use("d", "x", BASE + 6), result("d", "PreToolUse:Bash hook error: [guardrails:Bad Id] m", BASE + 7),
                 use("e", "x", BASE + 8), result("e", "PreToolUse:Bash hook error: [guardrails:] m", BASE + 9)]
        self.write("p/a.jsonl", cases)
        self.assertEqual(self.find().hits, [])

    def test_a_result_without_a_shell_tool_use_in_the_same_transcript_is_ignored(self) -> None:
        self.write("p/a.jsonl", [result("orphan", DENIAL, BASE + 1)])
        self.write("p/b.jsonl", [use("u", "x", BASE, tool="mcp__serena__execute_shell_command"), result("u", DENIAL, BASE + 1)])
        self.assertEqual(self.find().hits, [])

    def test_ids_are_parsed_from_the_marker_and_later_paragraphs_with_the_managed_suffix_removed(self) -> None:
        text = "[guardrails:a-rule, b-rule (managed)] first\n\n[guardrails:warn-rule] second\n\nplain [guardrails:not-a-marker]"
        self.assertEqual(audit.denial_ids(text, "Bash"), ["a-rule", "b-rule", "warn-rule"])
        self.assertEqual(audit.denial_ids(f"PreToolUse:Bash hook error: {text}", "Bash"), ["a-rule", "b-rule", "warn-rule"])
        self.assertIsNone(audit.denial_ids(f"PreToolUse:Bash hook error: {text}", "Monitor"))
        self.assertIsNone(audit.denial_ids(" [guardrails:a] m", "Bash"))

    def test_warn_rules_that_ride_in_a_denial_do_not_count_as_the_denier(self) -> None:
        text = "[guardrails:no-pkill] deny text\n\n[guardrails:warn-pgrep] warn text"
        self.write("p/a.jsonl", denied("t1", BASE, text=text))
        self.assertEqual(self.find(warn=frozenset({"warn-pgrep"})).hits[0]["rules"], ["no-pkill"])
        self.assertEqual(len(self.find(rule="no-pkill", warn=frozenset({"warn-pgrep"})).hits), 1)
        self.assertEqual(self.find(rule="warn-pgrep", warn=frozenset({"warn-pgrep"})).hits, [])

    def test_the_config_dir_override_is_honoured(self) -> None:
        self.assertEqual(audit.projects_dir(), self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR")
            self.assertEqual(audit.projects_dir(), Path.home() / ".claude" / "projects")


class Filtering(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.write("p/a.jsonl", [*denied("t1", BASE, text="[guardrails:no-pkill] m"),
                                 *denied("t2", BASE + 10, text="[guardrails:old-dev-rule] m"),
                                 *denied("t3", BASE + 20, text="[guardrails:no-curl] m")])

    def test_rule_filter(self) -> None:
        report = self.find(rule="no-curl")
        self.assertEqual([h["tool_use_id"] for h in report.hits], ["t3"])
        self.assertEqual(report.dropped, 0)

    def test_unknown_ids_are_dropped_and_counted_unless_all_rules(self) -> None:
        report = self.find(known=frozenset({"no-pkill", "no-curl"}))
        self.assertEqual(([h["rule"] for h in report.hits], report.dropped), (["no-curl", "no-pkill"], 1))
        report = self.find()
        self.assertEqual(([h["rule"] for h in report.hits], report.dropped), (["no-curl", "old-dev-rule", "no-pkill"], 0))


class Ordering(Fixture):
    def test_newest_first_across_files_and_limit(self) -> None:
        self.write("p/old.jsonl", denied("t1", BASE), mtime=BASE + 100)
        self.write("p/new.jsonl", denied("t3", BASE + 5000), mtime=BASE + 6000)
        self.write("p/mid.jsonl", [*denied("t2", BASE + 2000), *denied("t2b", BASE + 7000)], mtime=BASE + 7100)
        self.assertEqual([h["tool_use_id"] for h in self.find().hits], ["t2b", "t3", "t2", "t1"])
        self.assertEqual([h["tool_use_id"] for h in self.find(limit=2).hits], ["t2b", "t3"])

    def build_chain(self, count: int = 12) -> list[str]:
        names = []
        for i in range(count):
            rel = f"p/f{i:02d}.jsonl"
            when = BASE + (count - i) * 1000
            self.write(rel, denied(f"t{i:02d}", when), mtime=when + 5)
            names.append(rel)
        return names

    def test_early_stop_does_not_open_files_beyond_the_stopping_point(self) -> None:
        names = self.build_chain()
        straggler = self.write("p/zz-old.jsonl", denied("late", BASE - 5000), mtime=BASE - 4000)
        straggler.chmod(0)
        self.addCleanup(straggler.chmod, 0o600)
        report = self.find(limit=2)
        self.assertEqual([h["tool_use_id"] for h in report.hits], ["t00", "t01"])
        self.assertEqual(report.opened, names[:2])
        self.assertTrue(report.stopped_early)
        self.assertEqual(report.unreadable, 0)

    def test_early_stop_with_the_pool_opens_only_whole_windows(self) -> None:
        names = self.build_chain()
        report = self.find(limit=2, workers=2)
        self.assertEqual([h["tool_use_id"] for h in report.hits], ["t00", "t01"])
        self.assertEqual(report.opened, names[:4])
        self.assertTrue(report.stopped_early)

    def test_an_unfilled_limit_scans_everything(self) -> None:
        names = self.build_chain(5)
        report = self.find(limit=50)
        self.assertEqual((len(report.hits), report.opened, report.stopped_early), (5, names, False))

    def test_the_pool_returns_the_same_hits_in_the_same_order_as_the_serial_path(self) -> None:
        for i in range(30):
            lines = [chat("user", f"hello {i}", BASE + i * 100)]
            for j in range(i % 3):
                lines += denied(f"t{i}-{j}", BASE + i * 100 + 10 + j * 20, command=f"pkill n{i}{j}")
            self.write(f"p{i % 4}/f{i:02d}.jsonl", lines, mtime=BASE + i * 100 + 90)
        serial = self.find(limit=1000, workers=1)
        pooled = self.find(limit=1000, workers=3)
        self.assertEqual(len(serial.hits), 30)
        self.assertEqual(serial.hits, pooled.hits)
        self.assertEqual(self.find(limit=7, workers=1).hits, self.find(limit=7, workers=3).hits)
        self.assertEqual(serial.opened, pooled.opened)

    def test_duplicates_across_resumed_files_are_one_hit_with_also_in(self) -> None:
        history = [chat("user", "go", BASE), *denied("dup", BASE + 10)]
        self.write("p/orig.jsonl", history, mtime=BASE + 100)
        self.write("p/resumed.jsonl", [*history, chat("assistant", "more", BASE + 200)], mtime=BASE + 300)
        self.write("q/other.jsonl", history, mtime=BASE + 200)
        for workers in (1, 3):
            hits = self.find(workers=workers).hits
            self.assertEqual(len(hits), 1)
            self.assertEqual((hits[0]["file"], hits[0]["also_in"]), ("p/resumed.jsonl", ["q/other.jsonl", "p/orig.jsonl"]))

    def test_the_same_id_twice_in_one_file_is_one_hit(self) -> None:
        self.write("p/a.jsonl", [*denied("t", BASE), result("t", DENIAL, BASE + 5)])
        self.assertEqual(len(self.find().hits), 1)


class Context(Fixture):
    def setUp(self) -> None:
        super().setUp()
        lines = [chat("user" if i % 2 == 0 else "assistant", f"m{i}", BASE + i) for i in range(10)]
        lines += [json.dumps({"type": "progress", "timestamp": stamp(BASE + 50)}),
                  json.dumps({"type": "system", "message": {"role": "user", "content": "skip"}})]
        lines += denied("t1", BASE + 100)
        lines += [json.dumps({"type": "queue-operation"})]
        lines += [chat("user", f"a{i}", BASE + 200 + i) for i in range(10)]
        self.write("p/a.jsonl", lines)

    def window(self, **query: Any) -> tuple[list[str], list[str]]:
        hit = self.find(**query).hits[0]
        words = lambda ms: [b["text"] for m in ms for b in m["blocks"]]
        return words(hit["before"]), words(hit["after"])

    def test_defaults_are_four_messages_each_way_skipping_non_messages(self) -> None:
        self.assertEqual(self.window(), (["m6", "m7", "m8", "m9"], ["a0", "a1", "a2", "a3"]))

    def test_before_and_after_are_independent_and_zero_is_empty(self) -> None:
        self.assertEqual(self.window(before=2, after=1), (["m8", "m9"], ["a0"]))
        self.assertEqual(self.window(before=0, after=0), ([], []))
        self.assertEqual(self.window(before=0, after=2), ([], ["a0", "a1"]))

    def test_windows_stop_at_the_edges_of_the_file(self) -> None:
        before, after = self.window(before=50, after=50)
        self.assertEqual((len(before), after[-1]), (10, "a9"))
        self.assertEqual(len(after), 10)

    def test_the_before_window_ends_at_the_denied_call_and_the_after_window_starts_after_the_denial(self) -> None:
        hit = self.find(before=1, after=1).hits[0]
        self.assertEqual((hit["before"][0]["line"], hit["line"], hit["after"][0]["line"]), (10, 14, 16))

    def test_messages_with_nothing_to_show_do_not_use_up_the_window(self) -> None:
        thinking = entry("assistant", [{"type": "thinking", "thinking": "hmm"}], BASE + 60)
        self.write("p/c.jsonl", [chat("user", "first", BASE), thinking, thinking, *denied("th", BASE + 70),
                                 thinking, chat("user", "next", BASE + 90)], mtime=BASE + 30_000)
        hit = {h["tool_use_id"]: h for h in self.find(before=2, after=1).hits}["th"]
        self.assertEqual(([b["text"] for m in hit["before"] for b in m["blocks"]],
                          [b["text"] for m in hit["after"] for b in m["blocks"]]), (["first"], ["next"]))

    def test_a_window_reaching_the_start_of_the_file_is_short(self) -> None:
        self.write("p/b.jsonl", [*denied("early", BASE), chat("user", "x", BASE + 5)], mtime=BASE + 20_000)
        hit = {h["tool_use_id"]: h for h in self.find(rule="no-pkill", before=3, after=3).hits}["early"]
        self.assertEqual((hit["before"], len(hit["after"])), ([], 1))

    def test_cli_context_sets_both_sides_and_explicit_a_and_b_win(self) -> None:
        self.put(self.gpath, {"rules": {"no-pkill": {"match": {"command": "pkill"}, "message": "m"}}})
        sides = lambda *argv: [(len(h["before"]), len(h["after"])) for h in self.hits(*argv)]
        self.assertEqual(sides(), [(4, 4)])
        self.assertEqual(sides("-C", "2"), [(2, 2)])
        self.assertEqual(sides("-C", "2", "-A", "5"), [(2, 5)])
        self.assertEqual(sides("-C", "2", "-B", "0"), [(0, 2)])
        self.assertEqual(sides("-C", "0"), [(0, 0)])
        self.assertEqual(sides("-B", "1", "-A", "3"), [(1, 3)])

    def hits(self, *argv: str) -> list[dict[str, Any]]:
        code, out, err = self.cli("audit", "--json", *argv)
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)["hits"]


class Robustness(Fixture):
    def test_corrupt_lines_are_tolerated_and_counted(self) -> None:
        good = denied("t1", BASE)
        self.write("p/a.jsonl", [good[0], "{not json", good[1], '{"type": "user", "message": {"content": [', "[1, 2]"])
        report = self.find()
        self.assertEqual((len(report.hits), report.corrupt, report.unreadable), (1, 2, 0))

    def test_a_truncated_last_line_without_a_newline_is_counted(self) -> None:
        path = self.write("p/a.jsonl", denied("t1", BASE))
        with path.open("a") as handle:
            handle.write('{"type": "user", "mess')
        report = self.find()
        self.assertEqual((len(report.hits), report.corrupt), (1, 1))

    def test_unreadable_and_empty_files_are_skipped(self) -> None:
        self.write("p/a.jsonl", denied("t1", BASE))
        self.write("p/empty.jsonl", [], mtime=BASE + 20_000).write_text("")
        locked = self.write("p/locked.jsonl", denied("t2", BASE), mtime=BASE + 30_000)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        for workers in (1, 2):
            report = self.find(workers=workers)
            self.assertEqual(([h["tool_use_id"] for h in report.hits], report.unreadable), (["t1"], 1))

    def test_no_projects_dir_is_an_empty_report(self) -> None:
        report = self.find()
        self.assertEqual((report.hits, report.files_total), ([], 0))

    def test_truncation_caps(self) -> None:
        long_text = "x" * 5000
        lines = [chat("user", long_text, BASE), entry("assistant", [{"type": "tool_use", "id": "o", "name": "Read",
                                                                     "input": {"file_path": long_text}}], BASE + 1),
                 entry("user", [{"type": "tool_result", "tool_use_id": "o", "content": long_text}], BASE + 2),
                 *denied("t1", BASE + 10, command="pkill " + "y" * 5000, text="[guardrails:no-pkill] " + "z" * 5000),
                 chat("assistant", long_text, BASE + 20)]
        self.write("p/a.jsonl", lines)
        hit = self.find(before=4, after=1).hits[0]
        self.assertEqual((len(hit["command"]), hit["command_truncated"]), (audit.COMMAND_CAP, True))
        self.assertEqual((len(hit["message"]), hit["message_truncated"]), (audit.MESSAGE_CAP, True))
        blocks = [b for m in (*hit["before"], *hit["after"]) for b in m["blocks"]]
        self.assertEqual(len(blocks), 4)
        self.assertTrue(all(len(b["text"]) == audit.TEXT_CAP and b["truncated"] for b in blocks))

    def test_block_kinds_are_rendered_without_thinking_and_with_uniform_keys(self) -> None:
        content = [{"type": "thinking", "thinking": "hmm", "signature": "s"}, {"type": "text", "text": "hi"},
                   {"type": "tool_use", "id": "k", "name": "Bash", "input": {"command": "ls"}},
                   {"type": "image", "source": {}}]
        self.write("p/a.jsonl", [entry("assistant", content, BASE), *denied("t1", BASE + 10)])
        blocks = self.find(before=1).hits[0]["before"][0]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["text", "tool_use", "image"])
        self.assertEqual({tuple(sorted(b)) for b in blocks}, {("is_error", "name", "text", "tool_use_id", "truncated", "type")})


class Rendering(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"no-pkill": {"match": {"command": "pkill"}, "message": "m"}}})
        self.write("proj/s.jsonl", [chat("user", "kill it\nnow", BASE), *denied("t1", BASE + 10, "pkill `node`\nx",
                                                                               agentId="ag1", isSidechain=True),
                                    chat("assistant", "ok", BASE + 20)])
        self.write("proj/u.jsonl", denied("t2", BASE + 100, text="[guardrails:dev-only] m"), mtime=BASE + 20_000)

    def test_json_schema_is_stable(self) -> None:
        code, out, _ = self.cli("audit", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(list(data), ["projects", "rule", "files_total", "files_scanned", "stopped_early",
                                      "dropped_unknown_rules", "corrupt_lines", "unreadable_files", "hits"])
        self.assertEqual((data["files_total"], data["dropped_unknown_rules"], len(data["hits"])), (2, 1, 1))
        hit = data["hits"][0]
        self.assertEqual(list(hit), ["rule", "rules", "tool", "tool_use_id", "timestamp", "local", "session_id", "file",
                                     "line", "is_sidechain", "agent_id", "cwd", "command", "command_truncated", "message",
                                     "message_truncated", "before", "after", "also_in"])
        self.assertEqual((hit["file"], hit["line"], hit["is_sidechain"], hit["agent_id"], hit["cwd"], hit["session_id"]),
                         ("proj/s.jsonl", 3, True, "ag1", "/work/app", "sess-1"))
        self.assertEqual(list(hit["before"][0]), ["role", "timestamp", "line", "blocks"])

    def test_all_rules_keeps_unknown_ids(self) -> None:
        data = json.loads(self.cli("audit", "--json", "--all-rules")[1])
        self.assertEqual(([h["rule"] for h in data["hits"]], data["dropped_unknown_rules"]), (["dev-only", "no-pkill"], 0))

    def test_rule_argument_and_limit(self) -> None:
        self.assertEqual([h["rule"] for h in json.loads(self.cli("audit", "no-pkill", "--json")[1])["hits"]], ["no-pkill"])
        self.assertEqual(len(json.loads(self.cli("audit", "-n", "1", "--all-rules", "--json")[1])["hits"]), 1)

    def test_local_time_is_rendered_in_the_machine_zone_and_json_carries_utc_too(self) -> None:
        if not hasattr(time, "tzset"):
            self.skipTest("no tzset")
        self.enterContext(mock.patch.dict(os.environ, {"TZ": "Europe/Zurich"}))
        time.tzset()
        self.addCleanup(time.tzset)
        self.write("proj/s.jsonl", denied("z", datetime(2026, 7, 1, 10, 0, 0, tzinfo=UTC).timestamp()))
        hit = json.loads(self.cli("audit", "--json", "-n", "5")[1])["hits"][-1]
        self.assertEqual((hit["timestamp"], hit["local"]), ("2026-07-01T10:00:01.000Z", "2026-07-01 12:00:01"))
        self.assertIn("2026-07-01 12:00:01", self.cli("audit")[1])

    def test_markdown_card(self) -> None:
        code, out, _ = self.cli("audit", "-C", "1")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### Guardrails audit · 1 denial · newest first\n\n**Scope** scanned 2 of 2 transcript files"))
        self.assertIn("1 denial for rules not in the current config left out", out)
        self.assertIn("#### no-pkill · Bash · ", out)
        self.assertIn("**Command** `` pkill `node`⏎x ``", out)
        self.assertIn("· subagent `ag1`", out)
        self.assertIn("**Before**\n- user · text: kill it now", out)
        self.assertIn("**After**\n- assistant · text: ok", out)
        self.assertEqual(out.count("\n\n\n"), 0)

    def test_no_denials_is_one_plain_line_with_the_scope(self) -> None:
        code, out, _ = self.cli("audit", "ghost")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("No guardrails denials found for ghost: scanned 2 of 2 transcript files under"))
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_the_command_never_writes_anything(self) -> None:
        before = self.tree()
        for argv in (("audit",), ("audit", "--json", "--all-rules"), ("audit", "no-pkill", "-n", "3", "-C", "9")):
            self.cli(*argv)
        self.assertEqual(self.tree(), before)
        self.assertEqual(list(self.data.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
