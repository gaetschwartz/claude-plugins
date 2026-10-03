from __future__ import annotations

import json
import os
import subprocess
import unittest
import urllib.request
from unittest import mock

import bootstrap
from helpers import HOOKS, REAL_URLOPEN, Isolated

OFFLINE = "the real runtime could not be installed ({reason}); the install test is skipped"


class RealInstall(Isolated):
    """The real bootstrap into an empty temp data dir: uv from PyPI, Python from uv, the pinned library, the real hook."""

    def test_an_empty_data_dir_becomes_a_working_runtime_and_the_hook_denies_through_it(self) -> None:
        with mock.patch.object(urllib.request, "urlopen", REAL_URLOPEN):
            outcome = bootstrap.ensure(self.data)
        if outcome.state != "installed":
            if os.environ.get("GUARDRAILS_REQUIRE_AST") == "1":
                self.fail(OFFLINE.format(reason=outcome.reason or outcome.state))
            self.skipTest(OFFLINE.format(reason=outcome.reason or outcome.state))
        pins = bootstrap.load_pins()
        rt = bootstrap.runtime_dir(self.data, pins)
        self.assertIsNone(bootstrap.marker_problem(rt, pins))
        self.assertFalse((rt / "uv-cache").exists())
        marker = json.loads((rt / "marker.json").read_text())
        self.assertEqual((marker["runtimeId"], marker["python"].split(".")[:2]), (pins.runtime_id, ["3", "13"]))
        self.assertEqual(bootstrap.ensure(self.data).state, "ready")
        self.put(self.gpath, {"rules": {"no-pkill": {"match": {"program": "pkill"}, "message": "No pkill.",
                                                      "action": "deny"}}})
        payload = json.dumps({"session_id": "real", "cwd": str(self.proj), "tool_name": "Bash",
                              "tool_input": {"command": "sudo -u bob pkill -f x"}})
        proc = subprocess.run(["sh", str(HOOKS / "guardrails.sh")], input=payload, capture_output=True, text=True,
                              check=False, env=dict(os.environ), cwd=self.proj)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
