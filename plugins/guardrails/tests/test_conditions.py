from __future__ import annotations  # noqa: I001

import json
import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import AstIsolated, caught, Isolated, plain

import conditions
import engine
import matching
import messages
import policy
from verdict import Detail


def rule(**fields: Any) -> dict[str, Any]:
    return {"match": {"command": "strings"}, "message": "use docs", **fields}


def stub_path(folder: Path, *names: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("#!/bin/sh\n")
        (folder / name).chmod(0o755)
    return str(folder)


class Atoms(Isolated):
    def holds(self, node: dict[str, Any], env: conditions.Env = conditions.DEFAULT) -> bool:
        conditions.check(node, "when")
        return conditions.holds(node, env)

    def test_bin_is_any_of_its_names_on_path(self) -> None:
        os.environ["PATH"] = stub_path(self.tmp / "bin", "fdfind")
        self.assertTrue(self.holds({"bin": "fdfind"}))
        self.assertFalse(self.holds({"bin": "fd"}))
        self.assertTrue(self.holds({"bin": ["fd", "fdfind"]}))
        self.assertFalse(self.holds({"bin": ["fd", "rg"]}))
        os.environ["PATH"] = str(self.tmp / "empty")
        self.assertFalse(self.holds({"bin": ["fd", "fdfind"]}))

    def test_bin_lookups_are_memoised_per_name_and_path(self) -> None:
        import shutil

        conditions.which.cache_clear()
        os.environ["PATH"] = stub_path(self.tmp / "memo", "zz-memo")
        with mock.patch.object(shutil, "which", wraps=shutil.which) as spy:
            for _ in range(3):
                self.assertTrue(self.holds({"bin": "zz-memo"}))
            self.assertEqual(spy.call_count, 1)
            os.environ["PATH"] = str(self.tmp / "empty")
            self.assertFalse(self.holds({"bin": "zz-memo"}))
            self.assertEqual(spy.call_count, 2)

    def test_os_and_arch_map_the_platform_names(self) -> None:
        for platform_name, expected in (("darwin", "macos"), ("linux", "linux")):
            with mock.patch.object(conditions.sys, "platform", platform_name):
                self.assertTrue(self.holds({"os": expected}))
                self.assertFalse(self.holds({"os": "linux" if expected == "macos" else "macos"}))
        import platform

        for machine, expected in (("arm64", "arm64"), ("aarch64", "arm64"), ("x86_64", "x86_64"), ("AMD64", "x86_64")):
            with mock.patch.object(platform, "machine", return_value=machine):
                self.assertTrue(self.holds({"arch": expected}))
                self.assertFalse(self.holds({"arch": "x86_64" if expected == "arm64" else "arm64"}))
        with mock.patch.object(platform, "machine", return_value="riscv64"):
            self.assertFalse(self.holds({"arch": "arm64"}) or self.holds({"arch": "x86_64"}))

    def test_host_matches_the_full_or_the_short_name(self) -> None:
        import socket

        conditions.hostname.cache_clear()
        self.addCleanup(conditions.hostname.cache_clear)
        with mock.patch.object(socket, "gethostname", return_value="box.example.org"):
            self.assertTrue(self.holds({"host": "box.example.org"}))
            self.assertTrue(self.holds({"host": "box"}))
            self.assertFalse(self.holds({"host": "example"}))
            self.assertFalse(self.holds({"host": "bo"}))

    def test_env_is_set_and_non_empty_or_equal(self) -> None:
        os.environ["GR_FLAG"] = "on"
        os.environ["GR_EMPTY"] = ""
        self.assertTrue(self.holds({"env": "GR_FLAG"}))
        self.assertFalse(self.holds({"env": "GR_EMPTY"}))
        self.assertFalse(self.holds({"env": "GR_UNSET"}))
        self.assertTrue(self.holds({"env": {"GR_FLAG": "on"}}))
        self.assertFalse(self.holds({"env": {"GR_FLAG": "off"}}))
        self.assertTrue(self.holds({"env": {"GR_EMPTY": ""}}))

    def test_file_is_relative_to_the_project_root_and_false_without_one(self) -> None:
        (self.proj / "sub").mkdir()
        (self.proj / "sub" / "Cargo.toml").write_text("")
        env = conditions.Env(root=self.proj)
        self.assertTrue(self.holds({"file": "sub/Cargo.toml"}, env))
        self.assertTrue(self.holds({"file": "sub"}, env))
        self.assertFalse(self.holds({"file": "Cargo.toml"}, env))
        self.assertFalse(self.holds({"file": "sub/Cargo.toml"}))

    def test_tool_is_the_calling_tool(self) -> None:
        self.assertTrue(self.holds({"tool": "Bash"}))
        self.assertFalse(self.holds({"tool": "Monitor"}))
        self.assertTrue(self.holds({"tool": "Monitor"}, conditions.Env("Monitor")))

    def test_combinators(self) -> None:
        os.environ["GR_A"] = "1"
        yes, no = {"env": "GR_A"}, {"env": "GR_B"}
        for node, expected in (({"all": [yes, yes]}, True), ({"all": [yes, no]}, False), ({"any": [no, yes]}, True),
                               ({"any": [no, no]}, False), ({"not": no}, True), ({"not": yes}, False),
                               ({"not": {"all": [yes, {"not": no}]}}, False), ({"any": [{"not": yes}, {"all": [yes]}]}, True)):
            with self.subTest(node=node):
                self.assertEqual(self.holds(node), expected)
        self.assertTrue(conditions.holds(None, conditions.Env()))


class Validation(unittest.TestCase):
    def invalid(self, node: object, *, hit: bool = False) -> str:
        with self.assertRaises(policy.Invalid) as raised:
            conditions.check(node, "when", hit=hit)
        return str(raised.exception)

    def test_each_atom_rejects_a_wrong_value_naming_the_path(self) -> None:
        bad: list[tuple[object, str]] = [
            ({"bin": ""}, "when.bin"), ({"bin": []}, "when.bin"), ({"bin": "a/b"}, "when.bin"),
            ({"bin": "a b"}, "when.bin"), ({"bin": ["fd", 3]}, "when.bin"), ({"bin": True}, "when.bin"),
            ({"os": "windows"}, "when.os"), ({"os": "darwin"}, "when.os"), ({"arch": "aarch64"}, "when.arch"),
            ({"arch": 64}, "when.arch"), ({"host": ""}, "when.host"), ({"host": "a b"}, "when.host"),
            ({"host": ["a"]}, "when.host"), ({"env": "1X"}, "when.env"), ({"env": "A-B"}, "when.env"),
            ({"env": {"A": 1}}, "when.env"), ({"env": {"A": "1", "B": "2"}}, "when.env"), ({"env": {}}, "when.env"),
            ({"file": "/etc/passwd"}, "when.file"), ({"file": "../x"}, "when.file"), ({"file": "a/../../x"}, "when.file"),
            ({"file": ""}, "when.file"), ({"file": 3}, "when.file"), ({"tool": "Read"}, "when.tool"),
            ({"tool": "bash"}, "when.tool"),
            ({"all": []}, "when.all"), ({"any": {"bin": "x"}}, "when.any"), ({"not": []}, "when.not"),
            ({"any": [{"bin": "x"}, {"oss": "linux"}]}, "when.any[1].oss"), ({}, "when"),
            ({"bin": "x", "os": "linux"}, "when"), ("bin", "when"), ({"all": [{"not": {"tool": 1}}]}, "when.all[0].not.tool"),
        ]
        for node, where in bad:
            with self.subTest(node=node):
                self.assertIn(f"'{where}", self.invalid(node))

    def test_hit_atoms_are_message_only_and_checked(self) -> None:
        self.assertIn("only works in a message case", self.invalid({"wrapped": True}))
        self.assertIn("only works in a message case", self.invalid({"any": [{"matches": {"kind": "command"}}]}))
        conditions.check({"all": [{"wrapped": False}, {"matches": {"command": "x", "has": {"regex": "^-f$"}}}]}, "c",
                         hit=True)
        for node in ({"wrapped": "yes"}, {"matches": {}}, {"matches": {"bogus": 1}}, {"matches": {"program": "x"}},
                     {"matches": "find"}):
            with self.subTest(node=node):
                self.invalid(node, hit=True)
        with self.assertRaises(policy.Invalid) as raised:
            policy.Rule.from_json(rule(when={"wrapped": True}))
        self.assertIn("'when.wrapped' only works in a message case", str(raised.exception))

    def test_message_cases_are_checked(self) -> None:
        case = {"when": {"wrapped": True}, "text": "t"}
        policy.Rule.from_json(rule(messages=[case, {**case, "messageShort": "s"}]))
        for messages_value, needle in (([], "non-empty list"), (case, "non-empty list"), ([{"when": {"wrapped": True}}],
                                       "messages[0]"), ([case, {"text": "t"}], "messages[1]"),
                                       ([{**case, "message": "m"}], "unknown field messages[0].message"),
                                       ([{**case, "text": " "}], "messages[0].text"),
                                       ([{**case, "messageShort": 1}], "messages[0].messageShort"),
                                       ([{**case, "when": {"bogus": 1}}], "messages[0].when.bogus"),
                                       ([{**case, "text": "{x.y}"}], "messages[0].text"), ([case] * 17, "at most 16")):
            with self.subTest(messages=messages_value), self.assertRaises(policy.Invalid) as raised:
                policy.Rule.from_json(rule(messages=messages_value))
            self.assertIn(needle, str(raised.exception))

    def test_depth_and_node_caps(self) -> None:
        node: dict[str, Any] = {"bin": "x"}
        for _ in range(conditions.MAX_DEPTH - 1):
            node = {"not": node}
        conditions.check(node, "when")
        self.assertIn("deeper than 8", self.invalid({"not": node}))
        conditions.check({"any": [{"bin": "x"}] * (conditions.MAX_NODES - 1)}, "when")
        self.assertIn("more than 64 nodes", self.invalid({"any": [{"bin": "x"}] * conditions.MAX_NODES}))

    def test_a_stored_requires_fails_loudly_naming_when(self) -> None:
        with self.assertRaises(policy.Invalid) as raised:
            policy.Rule.from_json(rule(requires=["rg"]))
        self.assertIn("'requires' was replaced by 'when'", str(raised.exception))
        self.assertIn('"when": {"bin"', str(raised.exception))
        found = policy.effective({}, {"rules": {"r": rule(requires=["rg"])}}, {})
        self.assertEqual((found.rules, list(found.problems)), ({}, ["r"]))
        layer, problems = policy.managed_layer({"rules": {"m": rule(requires=["rg"])}}, "/m")
        self.assertEqual(layer["rules"], {})
        self.assertIn("managed rule m is invalid and ignored: 'requires' was replaced by 'when'", problems[0])


class Placeholders(unittest.TestCase):
    def test_simple_fields_are_filled_and_braces_escape(self) -> None:
        values = {"found": "fd", "ARG": "x"}
        self.assertEqual(messages.fill("use {found} on {ARG}; {{}} and {{/}} stay", values),
                         "use fd on x; {} and {/} stay")
        self.assertEqual(messages.fill("missing {NOPE}!", values), "missing !")
        self.assertEqual(messages.fields("a {found} b {ARG_2} {_X}", "m"), ["found", "ARG_2", "_X"])

    def test_anything_but_a_simple_name_is_rejected(self) -> None:
        for text in ("{x.y}", "{ARG.y}", "{x[0]}", "{ARG[0]}", "{ARG!r}", "{ARG:>5}", "{found!s}", "{}", "{0}",
                     "{arg}", "{1A}", "{", "}", "a { b", "{which:fd|fdfind}"):
            with self.subTest(text=text), self.assertRaises(policy.Invalid):
                messages.fields(text, "message")

    def test_found_needs_a_bin_atom_and_captures_are_capped(self) -> None:
        policy.Rule.from_json(rule(message="use {found}", when={"any": [{"env": "X"}, {"bin": "fd"}]}))
        for fields in ({"message": "use {found}"}, {"message": "use {found}", "when": {"env": "X"}},
                       {"message": "use {found}", "when": {"not": {"bin": "fd"}}},
                       {"messageShort": "{found}", "when": {"os": "linux"}},
                       {"messages": [{"when": {"wrapped": True}, "text": "{found}"}]}):
            with self.subTest(fields=fields), self.assertRaises(policy.Invalid) as raised:
                policy.Rule.from_json(rule(**fields))
            self.assertIn("{found}", str(raised.exception))
        eight = " ".join(f"{{A{i}}}" for i in range(8))
        policy.Rule.from_json(rule(message=eight))
        with self.assertRaises(policy.Invalid):
            policy.Rule.from_json(rule(message=eight, messageShort="{A8}"))

    def test_found_is_the_first_bin_name_on_path_in_document_order(self) -> None:
        tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        when = {"any": [{"not": {"bin": "zz-b"}}, {"bin": ["zz-a", "zz-b"]}, {"bin": "zz-c"}]}
        with mock.patch.dict(os.environ, {"PATH": stub_path(tmp / "1", "zz-b", "zz-c")}):
            self.assertEqual(conditions.found(when), "zz-b")
        with mock.patch.dict(os.environ, {"PATH": stub_path(tmp / "2", "zz-a", "zz-b")}):
            self.assertEqual(conditions.found(when), "zz-a")
        with mock.patch.dict(os.environ, {"PATH": str(tmp / "none")}):
            self.assertIsNone(conditions.found(when))

    def test_override_texts_are_checked_against_the_base_when(self) -> None:
        base = {"rules": {"r": rule(when={"bin": "fd"}, message="m")}}
        self.assertEqual(policy.effective_rules({}, base, {"rules": {"r": {"message": "use {found}"}}})["r"].message,
                         "use {found}")
        for bad in ({"message": "{x.y}"}, {"messageShort": "{"}):
            self.assertEqual(policy.effective_rules({}, base, {"rules": {"r": bad}})["r"].message, "m")
        plain = {"rules": {"r": rule()}}
        self.assertEqual(policy.effective_rules({}, plain, {"rules": {"r": {"message": "{found}"}}})["r"].message,
                         "use docs")

    def test_the_checker_detail_is_validated_at_the_boundary(self) -> None:
        self.assertEqual(matching.detail_of([1, {"A": "x"}]), Detail(1, {"A": "x"}))
        self.assertEqual(matching.detail_of([None, {}]), Detail(None, {}))
        for raw in ([True, {}], ["1", {}], [1], [1, []], [1, {"A": 3}], [1, {"A": "x" * 300}],
                    [1, {f"A{i}": "x" for i in range(9)}], {"case": 1}):
            with self.subTest(raw=raw), self.assertRaises((TypeError, ValueError)):
                matching.detail_of(raw)


CASES: dict[str, Any] = {
    "match": {"pattern": "rm -rf $DIR $$$REST"},
    "message": "default for {DIR} then {REST} ({{literal}}) {UNBOUND}.",
    "messageShort": "short {DIR}",
    "messages": [
        {"when": {"matches": {"has": {"regex": "^/$"}}}, "text": "never the root: {DIR}", "messageShort": "root!"},
        {"when": {"wrapped": True}, "text": "wrapped rm of {DIR}"},
        {"when": {"all": [{"wrapped": False}, {"matches": {"has": {"regex": "^-v$"}}}]}, "text": "verbose rm"},
    ],
}


class Cases(AstIsolated):
    def texts(self, raw: dict[str, Any], command: str, env: conditions.Env = conditions.DEFAULT) -> tuple[str, str | None]:
        parsed = policy.Rule.from_json(raw)
        ev = matching.evaluate(command, {"r": parsed}, env)
        self.assertIsNotNone(ev.kinds["r"], command)
        texts = engine.texts_of(parsed, ev.details.get("r"))
        return texts.full, texts.short

    def test_first_matching_case_wins_and_the_default_is_the_message(self) -> None:
        for command, expected in (("rm -rf / x", ("never the root: /", "root!")),
                                  ("rm -rf / -v", ("never the root: /", "root!")),
                                  ("sudo rm -rf /tmp/a b", ("wrapped rm of /tmp/a", None)),
                                  ("bash -c 'rm -rf / x'", ("never the root: /", "root!")),
                                  ("rm -rf /tmp/a -v", ("verbose rm", None)),
                                  ("rm -rf /tmp/a b  c", ("default for /tmp/a then b c ({literal}) .", "short /tmp/a"))):
            with self.subTest(command=command):
                self.assertEqual(self.texts(CASES, command), expected)

    def test_captures_are_truncated(self) -> None:
        full, _ = self.texts({"match": {"pattern": "echo $A $$$"}, "message": "[{A}]"}, "echo " + "x" * 300)
        self.assertEqual(full, "[" + "x" * messages.MAX_CAPTURE_CHARS + "…]")

    def test_environment_atoms_work_in_cases(self) -> None:
        raw = rule(messages=[{"when": {"tool": "Monitor"}, "text": "monitor"}, {"when": {"file": "go.mod"}, "text": "go"}])
        self.assertEqual(self.texts(raw, "strings x", conditions.Env("Monitor"))[0], "monitor")
        self.assertEqual(self.texts(raw, "strings x", conditions.Env(root=self.proj))[0], "use docs")
        (self.proj / "go.mod").write_text("")
        self.assertEqual(self.texts(raw, "strings x", conditions.Env(root=self.proj))[0], "go")

    def test_a_case_whose_matches_does_not_compile_is_a_compile_error(self) -> None:
        bad = rule(messages=[{"when": {"matches": {"kind": "no_such_kind"}}, "text": "t"}])
        self.assertIn("no_such_kind", matching.check({"r": policy.Rule.from_json(bad)})["r"])
        ev = matching.evaluate("strings x", {"r": policy.Rule.from_json(bad)})
        self.assertEqual((ev.kinds["r"], sorted(ev.invalid)), (None, ["r"]))
        code, _, err = self.cli("rule", "add", "r", "--json", json.dumps(bad))
        self.assertEqual(code, 2)
        self.assertIn("does not compile", err)


class Hook(AstIsolated):
    def reason(self, command: str, tool: str = "Bash", session: str | None = None) -> str | None:
        out = self.hook(command, session=session or f"{tool}-{command}", tool=tool)
        if out is None or "permissionDecisionReason" not in out.get("hookSpecificOutput", {}):
            return None
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_the_hook_says_the_case_and_captures(self) -> None:
        self.put(self.gpath, {"rules": {"rm": CASES}})
        self.assertIn("[guardrails:rm] never the root: /", plain(self.reason("rm -rf / x") or ""))
        self.assertIn("[guardrails:rm] wrapped rm of /tmp/a", plain(self.reason("sudo rm -rf /tmp/a b") or ""))
        first = self.reason("rm -rf /tmp/a b", session="s")
        second = self.reason("rm -rf /tmp/z q", session="s")
        self.assertIn("default for /tmp/a then b ({literal}) .", first or "")
        self.assertIn("short /tmp/z", second or "")

    def test_rule_level_when_tool_and_file(self) -> None:
        self.put(self.gpath, {"rules": {"mon": rule(match={"command": "tail"}, when={"tool": "Monitor"}),
                                        "cargo": rule(match={"command": "make"}, when={"file": "Cargo.toml"},
                                                      message="use cargo")}})
        self.assertIsNone(self.reason("tail -f log"))
        self.assertIn("[guardrails:mon]", plain(self.reason("tail -f log", tool="Monitor") or ""))
        self.assertIsNone(self.reason("make all"))
        (self.proj / "Cargo.toml").write_text("")
        self.assertIn("[guardrails:cargo] use cargo", plain(self.reason("make all", session="later") or ""))

    def test_found_names_the_binary_on_path(self) -> None:
        sheet = rule(match={"command": "find"}, when={"bin": ["zz-fd", "zz-fdfind"]}, message="use {found} -e py")
        self.put(self.gpath, {"rules": {"find-fd": sheet}})
        os.environ["PATH"] = stub_path(self.tmp / "a", "zz-fd", "zz-fdfind")
        self.assertIn("use zz-fd -e py", self.reason("find . -name x") or "")
        os.environ["PATH"] = stub_path(self.tmp / "b", "zz-fdfind")
        self.assertIn("use zz-fdfind -e py", self.reason("find . -name x", session="b") or "")
        os.environ["PATH"] = str(self.tmp / "none")
        self.assertIsNone(self.reason("find . -name x", session="c"))

    def test_a_stored_requires_is_skipped_loudly_everywhere(self) -> None:
        self.put(self.gpath, {"rules": {"old": rule(requires=["strings"]), "ok": rule(match={"command": "nm"})}})
        out = self.hook("strings x; nm y")
        assert out is not None
        self.assertIn("[guardrails:ok]", plain(out["hookSpecificOutput"]["permissionDecisionReason"]))
        self.assertNotIn("[guardrails:old]", plain(out["hookSpecificOutput"]["permissionDecisionReason"]))
        self.assertIn("rule old is invalid ('requires' was replaced by 'when'", out["systemMessage"])
        problems = self.cli("status", "--problems")[1]
        self.assertIn("rule old: 'requires' was replaced by 'when'", problems)
        code, _, err = self.cli("rule", "add", "new", "--json", json.dumps(rule(requires=["fd"])))
        self.assertEqual(code, 2)
        self.assertIn("'requires' was replaced by 'when'", err)
        code, _, err = self.cli("rule", "test", "--json", json.dumps(rule(requires=["fd"])), "strings x")
        self.assertEqual(code, 2)


class Shown(AstIsolated):
    def test_status_shows_when_cases_and_inactive_rules(self) -> None:
        self.put(self.gpath, {"rules": {"off-here": rule(when={"bin": "no-such-bin-xyz"}),
                                        "on-here": rule(when={"not": {"bin": "no-such-bin-xyz"}}, messages=[
                                            {"when": {"wrapped": True}, "text": "w"}])}})
        out = self.cli("status")[1]
        self.assertIn('- `off-here` deny · global · inactive here: its when does not hold · when '
                      '`{"bin":"no-such-bin-xyz"}`', out)
        self.assertIn('- `on-here ` deny · global · enabled · when `{"not":{"bin":"no-such-bin-xyz"}}` · 1 message case',
                      out)

    def test_rule_test_shows_the_condition_the_cases_and_the_case_per_command(self) -> None:
        out = self.cli("rule", "test", "--json", json.dumps(CASES), "rm -rf / a", "sudo rm -rf /x y", "rm -rf /x y",
                       "ls")[1]
        self.assertIn("\n**Message** default for {DIR} then {REST} ({literal}) {UNBOUND}.\n", out)
        self.assertIn('\n**Case 1** when `{"matches":{"has":{"regex":"^/$"}}}` · never the root: {DIR}\n', out)
        self.assertIn('\n**Case 2** when `{"wrapped":true}` · wrapped rm of {DIR}\n', out)
        self.assertEqual(caught(out), {"rm -rf / a": True, "sudo rm -rf /x y": True, "rm -rf /x y": True, "ls": False})
        rows = {line.split("`")[1].strip(): line for line in out.splitlines() if line.startswith("- ")}
        self.assertTrue(rows["rm -rf / a"].endswith("inferred · case 1"))
        self.assertTrue(rows["sudo rm -rf /x y"].endswith("inferred · wrapped · case 2"))
        self.assertTrue(rows["rm -rf /x y"].endswith("inferred · default message"))
        self.assertTrue(rows["ls"].endswith("inferred"))
        when = self.cli("rule", "test", "--json", json.dumps(rule(when={"tool": "Bash"})), "strings x")[1]
        self.assertIn('\n**When** `{"tool":"Bash"}` · holds here\n', when)
        self.assertNotIn("**Note**", when)


if __name__ == "__main__":
    unittest.main()
