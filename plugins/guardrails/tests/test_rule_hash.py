from __future__ import annotations

import re
import unittest
from typing import Any
from unittest import mock

import policy
from helpers import AstIsolated

BASE: dict[str, Any] = {"match": {"command": "pkill"}, "message": "No pkill."}
MARKER = re.compile(r"\[guardrails:([A-Za-z0-9_.-]+)#([0-9a-f]{8})(?: \(managed\))?(?:, [^\]]*)?\]")


def digest(**fields: Any) -> str:
    return policy.rule_hash(policy.Rule.from_json({**BASE, **fields}))


class Hash(unittest.TestCase):
    def test_it_is_eight_hex_digits_and_independent_of_key_order(self) -> None:
        shuffled = {"message": "No pkill.", "match": {"command": "pkill"}}
        self.assertRegex(digest(), r"^[0-9a-f]{8}$")
        self.assertEqual(policy.rule_hash(policy.Rule.from_json(shuffled)), digest())
        wide = {"match": {"command": "pkill", "args": "x"}, "message": "m"}
        self.assertEqual(policy.rule_hash(policy.Rule.from_json({"message": "m", "match": {"args": "x", "command": "pkill"}})),
                         policy.rule_hash(policy.Rule.from_json(wide)))

    def test_equivalent_spellings_hash_equal(self) -> None:
        self.assertEqual(digest(), digest(retry="none", action="deny", wrappers=True))

    def test_every_field_that_shapes_the_denial_changes_it(self) -> None:
        cases = {"match": {"match": {"command": "killall"}}, "wrappers": {"wrappers": False},
                 "when": {"when": {"bin": "fd"}}, "action": {"action": "warn"}, "retry": {"retry": "same-command"},
                 "message": {"message": "Other."}, "messageShort": {"messageShort": "Short."},
                 "messages": {"messages": [{"when": {"wrapped": True}, "text": "Case."}]}}
        base = digest()
        seen = {base}
        for name, change in cases.items():
            with self.subTest(field=name):
                changed = digest(**change)
                self.assertNotIn(changed, seen)
                seen.add(changed)

    def test_bookkeeping_fields_do_not_change_it(self) -> None:
        for change in ({"description": "why"}, {"enabled": False}, {"modes": ["ops"]}, {"setBy": {"by": "user"}}):
            with self.subTest(change=change):
                rule = {**BASE, **change}
                try:
                    parsed = policy.Rule.from_json(rule)
                except policy.Invalid:
                    parsed = policy.Rule.from_json(BASE)
                self.assertEqual(policy.rule_hash(parsed), digest())

    def test_an_override_that_rewords_changes_the_effective_hash(self) -> None:
        managed = {"rules": {"r": BASE}}
        plain = policy.effective(managed, {}, {}).rules["r"]
        reworded = policy.effective({}, {"rules": {"r": BASE}}, {"rules": {"r": {"message": "Project words."}}}).rules["r"]
        self.assertNotEqual(policy.rule_hash(plain), policy.rule_hash(reworded))
        weakened = policy.effective({}, {"rules": {"r": BASE}}, {"rules": {"r": {"match": {"command": "x"}}}}).rules["r"]
        self.assertEqual(policy.rule_hash(weakened), digest())

    def test_a_rule_id_with_a_hash_sign_is_rejected(self) -> None:
        found = policy.effective({}, {"rules": {"bad#id": BASE, "good": BASE}}, {})
        self.assertEqual(list(found.rules), ["good"])
        self.assertIn("must not contain '#'", found.problems["bad#id"])


class Emission(AstIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.put(self.gpath, {"rules": {"no-pkill": BASE, "warn-ls": {"match": {"command": "ls"}, "action": "warn",
                                                                      "message": "Careful."}}})
        patch = mock.patch.object(policy, "rule_hash", wraps=policy.rule_hash)
        self.calls = patch.start()
        self.addCleanup(patch.stop)

    def test_the_hash_is_computed_only_when_a_text_is_produced(self) -> None:
        self.assertIsNone(self.hook("echo hi"))
        self.assertEqual(self.calls.call_count, 0)
        self.assertIsNotNone(self.hook("pkill node"))
        self.assertEqual(self.calls.call_count, 1)

    def test_deny_text_and_warn_riders_carry_the_hash_of_the_rule(self) -> None:
        out = self.hook("ls; pkill node")
        assert out is not None
        text = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(self.calls.call_count, 2)
        self.assertEqual(MARKER.findall(text), [("no-pkill", digest()), ("warn-ls", digest(match={"command": "ls"}, action="warn", message="Careful."))])

    def test_a_warning_alone_carries_the_hash_and_a_notice_has_none(self) -> None:
        out = self.hook("ls")
        assert out is not None
        self.assertRegex(out["hookSpecificOutput"]["additionalContext"], r"^\[guardrails:warn-ls#[0-9a-f]{8}\] Careful\.")

    def test_a_managed_rule_keeps_its_label_after_the_hash(self) -> None:
        self.put(self.mpath, {"rules": {"m": {"match": {"command": "rm"}, "message": "No rm."}}})
        out = self.hook("rm x")
        assert out is not None
        self.assertRegex(out["hookSpecificOutput"]["permissionDecisionReason"], r"^\[guardrails:m#[0-9a-f]{8} \(managed\)\] No rm\.")


if __name__ == "__main__":
    unittest.main()
