from __future__ import annotations  # noqa: I001

import json
import os
import subprocess
import sys
import time
import unittest
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import LIB, AstIsolated

import audit
import policy

BASE = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC).timestamp()
PKILL = {"match": {"command": "pkill"}, "message": "m"}
HASH = policy.rule_hash(policy.Rule.from_json(PKILL))
DENIAL = f"[guardrails:no-pkill#{HASH}] Killing by name can hit the wrong process."
LEGACY = "[guardrails:no-pkill] Killing by name can hit the wrong process."


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


class Fixture(AstIsolated):
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
        self.assertTrue(hits[1]["message"].startswith("PreToolUse:Bash hook error: [guardrails:no-pkill#"))

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
        text = ("[guardrails:a-rule#0123abcd, b-rule#89ef4567 (managed)] first\n\n[guardrails:warn-rule] second\n\n"
                "plain [guardrails:not-a-marker]")
        parsed = [audit.Marked("a-rule", "0123abcd"), audit.Marked("b-rule", "89ef4567"), audit.Marked("warn-rule", None)]
        self.assertEqual(audit.denial_ids(text, "Bash"), parsed)
        self.assertEqual(audit.denial_ids(f"PreToolUse:Bash hook error: {text}", "Bash"), parsed)
        for bad in ("[guardrails:a#] m", "[guardrails:a#XYZ12345] m", "[guardrails:a#0123abc] m", "[guardrails:#0123abcd] m"):
            self.assertIsNone(audit.denial_ids(bad, "Bash"), bad)
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
        self.current = {"no-pkill": HASH, "no-curl": "c0c0c0c0"}
        self.write("p/a.jsonl", [*denied("t1", BASE, text=f"[guardrails:no-pkill#{HASH}] m"),
                                 *denied("t2", BASE + 10, text="[guardrails:old-dev-rule#aaaaaaaa] m"),
                                 *denied("t3", BASE + 20, text="[guardrails:no-curl#bbbbbbbb] m"),
                                 *denied("t4", BASE + 30, text="[guardrails:no-pkill] m"),
                                 *denied("t5", BASE + 40, text="[guardrails:no-curl#c0c0c0c0] m")])

    def kept(self, **query: Any) -> tuple[list[str], dict[str, int]]:
        report = self.find(current=self.current, **query)
        return [h["tool_use_id"] for h in report.hits], {why.value: report.skipped_for(why) for why in audit.Skip}

    def test_only_denials_from_the_current_rule_version_are_kept_and_the_rest_counted_by_reason(self) -> None:
        self.assertEqual(self.kept(), (["t5", "t1"], {"other_version": 1, "unhashed": 1, "unknown": 1}))

    def test_all_rules_adds_unknown_ids_but_still_filters_known_ones(self) -> None:
        self.assertEqual(self.kept(keep_unknown=True), (["t5", "t2", "t1"], {"other_version": 1, "unhashed": 1, "unknown": 0}))

    def test_rule_filter_counts_only_that_rules_denials(self) -> None:
        self.assertEqual(self.kept(rule="no-curl"), (["t5"], {"other_version": 1, "unhashed": 0, "unknown": 0}))
        self.assertEqual(self.kept(rule="old-dev-rule"), ([], {"other_version": 0, "unhashed": 0, "unknown": 1}))

    def test_hit_carries_the_hash_from_its_marker(self) -> None:
        hits = self.find(current=self.current, keep_unknown=True).hits
        self.assertEqual([(h["rule"], h["rule_hash"]) for h in hits], [("no-curl", "c0c0c0c0"), ("old-dev-rule", "aaaaaaaa"),
                                                                     ("no-pkill", HASH)])

    def test_legacy_markers_are_kept_when_no_current_config_is_given(self) -> None:
        self.assertEqual([h["rule_hash"] for h in self.find().hits], ["c0c0c0c0", None, "bbbbbbbb", "aaaaaaaa", HASH])

    def test_a_multi_rule_denial_counts_when_any_considered_rule_is_current(self) -> None:
        text = f"[guardrails:no-curl#bbbbbbbb, no-pkill#{HASH}] m"
        self.write("q/m.jsonl", denied("mm", BASE + 50, text=text), mtime=BASE + 20_000)
        hits = self.find(current=self.current).hits
        first = hits[0]
        self.assertEqual((first["tool_use_id"], first["rule"], first["rules"]), ("mm", "no-pkill", ["no-curl", "no-pkill"]))
        self.assertEqual(self.find(current=self.current, rule="no-curl").skipped_for(audit.Skip.OTHER_VERSION), 2)

    def test_skip_counts_do_not_double_count_copies_in_resumed_files(self) -> None:
        for name in ("r1", "r2", "r3"):
            self.write(f"q/{name}.jsonl", denied("same", BASE + 60, text="[guardrails:no-pkill#99999999] m"), mtime=BASE + 20_000)
        self.assertEqual(self.find(current=self.current).skipped_for(audit.Skip.OTHER_VERSION), 2)

    def test_skipped_denials_do_not_count_toward_the_limit_and_the_pool_agrees(self) -> None:
        for i in range(10):
            skipped = denied(f"old{i}", BASE + 1000 + i * 10, text="[guardrails:no-pkill#99999999] m")
            self.write(f"s/n{i:02d}.jsonl", skipped, mtime=BASE + 2000 + i)
        for i in range(3):
            self.write(f"s/k{i}.jsonl", denied(f"ok{i}", BASE + 100 + i * 10), mtime=BASE + 105 + i * 10)
        for workers in (1, 3):
            report = self.find(limit=2, workers=workers, current=self.current)
            self.assertEqual([h["tool_use_id"] for h in report.hits], ["ok2", "ok1"])
            self.assertEqual(report.stopped_early, workers == 1)
        serial, pooled = (self.find(limit=20, workers=w, current=self.current) for w in (1, 3))
        self.assertEqual((serial.hits, serial.skipped), (pooled.hits, pooled.skipped))


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
        self.put(self.gpath, {"rules": {"no-pkill": PKILL}})
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
                 *denied("t1", BASE + 10, command="pkill " + "y" * 5000, text=f"[guardrails:no-pkill#{HASH}] " + "z" * 5000),
                 chat("assistant", long_text, BASE + 20)]
        self.write("p/a.jsonl", lines)
        hit = self.find(before=4, after=1).hits[0]
        self.assertEqual((len(hit["command"]), hit["command_truncated"]), (audit.COMMAND_CAP, True))
        self.assertEqual((len(hit["message"]), hit["message_truncated"]), (audit.MESSAGE_CAP, True))
        blocks = [b for m in (*hit["before"], *hit["after"]) for b in m["blocks"]]
        self.assertEqual([(b["type"], len(b["text"]), b["truncated"]) for b in blocks],
                         [("text", audit.TEXT_CAP, True), ("tool_use", audit.LABEL_CAP, True),
                          ("tool_result", audit.TEXT_CAP, True), ("text", audit.TEXT_CAP, True)])

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
        self.put(self.gpath, {"rules": {"no-pkill": PKILL}})
        self.write("proj/s.jsonl", [chat("user", "kill it\nnow", BASE), *denied("t1", BASE + 10, "pkill `node`\nx",
                                                                               agentId="ag1", isSidechain=True),
                                    chat("assistant", "ok", BASE + 20)])
        self.write("proj/u.jsonl", denied("t2", BASE + 100, text="[guardrails:dev-only#aaaaaaaa] m"),
                   mtime=BASE + 20_000)
        self.write("proj/v.jsonl", denied("t3", BASE + 200, text=f"[guardrails:no-pkill#{'9' * 8}] m"), mtime=BASE + 20_001)
        self.write("proj/w.jsonl", denied("t4", BASE + 300, text="[guardrails:no-pkill] m"), mtime=BASE + 20_002)

    def test_json_schema_is_stable(self) -> None:
        code, out, _ = self.cli("audit", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(list(data), ["projects", "rule", "files_total", "files_scanned", "stopped_early",
                                      "skipped_other_version", "skipped_unhashed", "dropped_unknown_rules",
                                      "corrupt_lines", "unreadable_files", "hits"])
        self.assertEqual((data["files_total"], data["skipped_other_version"], data["skipped_unhashed"],
                          data["dropped_unknown_rules"], len(data["hits"])), (4, 1, 1, 1, 1))
        hit = data["hits"][0]
        self.assertEqual(list(hit), ["rule", "rule_hash", "rules", "tool", "tool_use_id", "timestamp", "local",
                                     "session_id", "file", "line", "is_sidechain", "agent_id", "cwd", "command",
                                     "command_truncated", "matched", "matched_truncated", "matched_note", "message",
                                     "message_truncated", "before", "after", "also_in"])
        self.assertEqual(hit["rule_hash"], HASH)
        self.assertEqual((hit["file"], hit["line"], hit["is_sidechain"], hit["agent_id"], hit["cwd"], hit["session_id"]),
                         ("proj/s.jsonl", 3, True, "ag1", "/work/app", "sess-1"))
        self.assertEqual(list(hit["before"][0]), ["role", "timestamp", "line", "blocks"])

    def test_all_rules_keeps_unknown_ids(self) -> None:
        data = json.loads(self.cli("audit", "--json", "--all-rules")[1])
        self.assertEqual(([h["rule"] for h in data["hits"]], data["dropped_unknown_rules"]), (["dev-only", "no-pkill"], 0))
        self.assertEqual(data["skipped_other_version"], 1)

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
        self.assertTrue(out.startswith("### Guardrails audit · 1 denial · newest first\n\n**Scope** scanned 4 of 4 transcript files"))
        for note in ("1 denial skipped: other version of the rule", "1 denial skipped: recorded before rule hashing",
                     "1 denial skipped: rule no longer exists"):
            self.assertIn(note, out)
        self.assertIn(f"#### no-pkill#{HASH} · Bash · ", out)
        self.assertIn("**Command** `` pkill `node`⏎x ``", out)
        self.assertIn("· subagent `ag1`", out)
        self.assertIn("**Before**\n- user · text: kill it now", out)
        self.assertIn("**After**\n- assistant · text: ok", out)
        self.assertEqual(out.count("\n\n\n"), 0)

    def test_no_denials_is_one_plain_line_with_the_scope(self) -> None:
        code, out, _ = self.cli("audit", "ghost")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("No guardrails denials found for ghost: scanned 4 of 4 transcript files under"))
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_the_command_never_writes_anything(self) -> None:
        before = self.tree()
        for argv in (("audit",), ("audit", "--json", "--all-rules"), ("audit", "no-pkill", "-n", "3", "-C", "9")):
            self.cli(*argv)
        self.assertEqual(self.tree(), before)
        self.assertEqual(list(self.data.iterdir()), [])


class Compact(Fixture):
    def test_non_shell_tool_calls_show_the_name_and_one_short_key_field(self) -> None:
        calls = [("Write", {"file_path": "/a/b.py", "content": "x" * 9000}, "/a/b.py"),
                 ("Grep", {"pattern": "foo", "path": "/src"}, "/src"), ("Grep", {"pattern": "foo"}, "foo"),
                 ("WebFetch", {"url": "https://example.com/" + "u" * 300, "prompt": "p"}, ("https://example.com/" + "u" * 300)[:120]),
                 ("Agent", {"description": "do it", "prompt": "long " * 500}, "do it"),
                 ("Task", {"other": 1}, ""), ("Bash", {"command": "ls -la"}, "ls -la")]
        lines = [entry("assistant", [{"type": "tool_use", "id": f"c{i}", "name": name, "input": args}], BASE + i)
                 for i, (name, args, _) in enumerate(calls)]
        self.write("p/a.jsonl", [*lines, *denied("t1", BASE + 50)])
        blocks = [b for m in self.find(before=9).hits[0]["before"] for b in m["blocks"]]
        self.assertEqual([(b["name"], b["text"]) for b in blocks], [(name, shown) for name, _, shown in calls])


class Matched(Fixture):
    def matched(self, command: str, rule: dict[str, Any] = PKILL) -> tuple[str | None, str | None]:
        import matching

        found = matching.matched_statement(command, policy.Rule.from_json(rule))
        return (found.matched.text if found.matched else None), found.note

    def test_a_plain_command_is_its_own_statement(self) -> None:
        self.assertEqual(self.matched("pkill x"), ("pkill x", None))

    def test_the_statement_inside_a_longer_script(self) -> None:
        self.assertEqual(self.matched("echo a\ncd /tmp\npkill x\necho done")[0], "pkill x")
        self.assertEqual(self.matched("cd /tmp && echo a; pkill x && echo done; ls")[0], "pkill x && echo done")

    def test_a_wrapped_command_matches_through_the_wrapper(self) -> None:
        found = self.matched("sudo pkill x")[0]
        self.assertIsNotNone(found)
        self.assertIn("pkill x", found or "")
        self.assertEqual(self.matched("echo ok; bash -c 'ls; pkill y; ls'")[0], "pkill y")

    def test_a_script_with_one_matching_statement(self) -> None:
        script = "cat <<EOF\npkill inside heredoc text\nEOF\nls\npkill x\necho done"
        self.assertEqual(self.matched(script)[0], "pkill x")

    def test_message_cases_do_not_change_which_statement_is_reported(self) -> None:
        cased = {**PKILL, "messages": [{"when": {"wrapped": True}, "text": "wrapped one"}]}
        for command in ("echo a; pkill x", "sudo pkill x; ls"):
            with self.subTest(command=command):
                self.assertEqual(self.matched(command, cased), self.matched(command))

    def test_a_replay_that_cannot_reproduce_the_hit_is_null_with_a_note(self) -> None:
        matched, note = self.matched("echo hi")
        self.assertIsNone(matched)
        self.assertIn("does not match", note or "")
        direct = {**PKILL, "wrappers": False}
        self.assertEqual(self.matched("sudo pkill x", direct)[0], None)
        self.assertIn("too large", self.matched("x" * (300 << 10))[1] or "")

    def test_when_modes_and_enabled_do_not_gate_the_replay(self) -> None:
        gated = {**PKILL, "when": {"bin": "definitely-not-installed"}, "enabled": False}
        self.assertEqual(self.matched("pkill x", gated)[0], "pkill x")

    def test_matched_is_filled_from_the_full_command_and_capped(self) -> None:
        self.put(self.gpath, {"rules": {"no-pkill": PKILL}})
        long_tail = "pkill " + "y" * 3000
        self.write("p/a.jsonl", [*denied("t1", BASE, command=f"echo {'z' * 3000}; {long_tail}")])
        hit = json.loads(self.cli("audit", "--json")[1])["hits"][0]
        self.assertEqual(len(hit["command"]), audit.COMMAND_CAP)
        self.assertNotIn("pkill", hit["command"][audit.COMMAND_CAP - 20:])
        self.assertEqual((len(hit["matched"]), hit["matched_truncated"], hit["matched_note"]), (audit.MATCHED_CAP, True, None))
        self.assertTrue(hit["matched"].startswith("pkill yyyy"))
        self.assertIn("**Matched** `pkill yyyy", self.cli("audit")[1])

    def test_an_unreproducible_hit_prints_a_note_and_null(self) -> None:
        self.put(self.gpath, {"rules": {"no-pkill": PKILL}})
        self.write("p/a.jsonl", denied("t1", BASE, command="echo nothing here"))
        hit = json.loads(self.cli("audit", "--json")[1])["hits"][0]
        self.assertEqual((hit["matched"], hit["matched_truncated"]), (None, False))
        self.assertIn("does not match", hit["matched_note"])
        self.assertIn("**Matched** none: ", self.cli("audit")[1])

    def test_an_all_rules_hit_for_an_unknown_rule_has_a_note(self) -> None:
        self.put(self.gpath, {"rules": {"no-pkill": PKILL}})
        self.write("p/a.jsonl", denied("t1", BASE, text="[guardrails:gone#aaaaaaaa] m"))
        hit = json.loads(self.cli("audit", "--json", "--all-rules")[1])["hits"][0]
        self.assertEqual((hit["matched"], hit["rule"]), (None, "gone"))
        self.assertIn("not in the current config", hit["matched_note"])


class EngineWeight(Fixture):
    HEAVY = ("matching", "scanner", "ast_grep_py")

    def run_python(self, body: str) -> dict[str, Any]:
        code = f"import sys, json\nsys.path.insert(0, {str(LIB)!r})\nimport audit, policy\n{body}"
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        return json.loads(done.stdout)

    def loaded(self) -> str:
        return f"[m for m in {self.HEAVY!r} if m in sys.modules]"

    def test_importing_audit_and_scanning_without_a_surviving_hit_never_loads_the_engine(self) -> None:
        self.write("p/a.jsonl", denied("t1", BASE, text="[guardrails:no-pkill#99999999] m"))
        out = self.run_python(f"""
before = {self.loaded()}
q = audit.Query(current={{"no-pkill": {HASH!r}}})
report = audit.find_denials(audit.Path({str(self.root)!r}), 5, q, workers=2)
audit.attach_matched(report, {{}})
print(json.dumps({{"before": before, "hits": len(report.hits), "after": {self.loaded()}}}))""")
        self.assertEqual(out, {"before": [], "hits": 0, "after": []})

    def test_workers_never_load_the_engine_and_the_main_process_loads_only_the_matcher_for_surviving_hits(self) -> None:
        self.write("p/a.jsonl", denied("t1", BASE))
        self.write("p/b.jsonl", denied("t2", BASE + 5), mtime=BASE + 20_000)
        out = self.run_python(f"""
real = audit.scan_file
def probe(path, rel, query):
    scan = real(path, rel, query)
    scan.corrupt = len({self.loaded()})
    return scan
audit.scan_file = probe
q = audit.Query(current={{"no-pkill": {HASH!r}}})
report = audit.find_denials(audit.Path({str(self.root)!r}), 5, q, workers=2)
rules = {{"no-pkill": policy.Rule.from_json({PKILL!r})}}
mid = {self.loaded()}
audit.attach_matched(report, rules)
print(json.dumps({{"hits": len(report.hits), "in_workers": report.corrupt, "mid": mid, "after": {self.loaded()},
                  "matched": [h["matched"] for h in report.hits]}}))""")
        self.assertEqual((out["hits"], out["in_workers"], out["mid"]), (2, 0, []))
        self.assertEqual(out["matched"], ["pkill node", "pkill node"])
        self.assertIn("matching", out["after"])
        self.assertNotIn("ast_grep_py", out["after"])


if __name__ == "__main__":
    unittest.main()
