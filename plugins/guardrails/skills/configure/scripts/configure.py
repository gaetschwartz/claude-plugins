#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""Show or change which shell-guard rules are active.

usage: configure.py [status]
       configure.py enable  <rule>...
       configure.py disable <rule>...
       configure.py reset
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hooks"))
from rules import all_rules, requires
from shell_guard import Config, config_path, load_config, rule_enabled


def save(path: Path, config: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def status(path: Path, config: Config) -> None:
    print(f"config: {path}{'' if path.exists() else ' (absent: defaults)'}")
    print()
    print(f"{'rule':<6} {'state':<9} {'tool':<10} description")
    needs = requires()
    for rule_id, desc in all_rules().items():
        enabled = rule_enabled(config, rule_id)
        tools = needs.get(rule_id, ())
        present = next((t for t in tools if shutil.which(t)), None)
        tool = "-" if not tools else (present or f"{tools[0]} missing")
        state = "enabled" if enabled else "disabled"
        if enabled and tools and not present:
            state = "inactive"
        print(f"{rule_id:<6} {state:<9} {tool:<10} {desc}")


def main(argv: list[str]) -> int:
    usage = (__doc__ or "").strip()
    if argv and argv[0] in ("-h", "--help"):
        print(usage)
        return 0

    path = config_path()
    if path is None:
        print("cannot locate the config: set SHELL_GUARD_CONFIG or CLAUDE_PLUGIN_DATA", file=sys.stderr)
        return 2
    config = load_config(path)
    action, *ids = argv or ["status"]
    known = all_rules()

    if action in ("status", "reset"):
        if ids:
            print(f"{action} takes no arguments", file=sys.stderr)
            return 2
        if action == "reset":
            path.unlink(missing_ok=True)
            config = Config()
        status(path, config)
        return 0
    if action not in ("enable", "disable"):
        print(f"unknown action: {action}\n\n{usage}", file=sys.stderr)
        return 2
    if not ids:
        print(f"{action} needs at least one rule; known: {', '.join(known)}", file=sys.stderr)
        return 2
    unknown = [i for i in ids if i not in known]
    if unknown:
        print(f"unknown rule(s): {', '.join(unknown)}; known: {', '.join(known)}", file=sys.stderr)
        return 2

    rules = config.get("rules")
    if not isinstance(rules, dict):
        rules = config["rules"] = {}
    for rule_id in ids:
        entry = rules.get(rule_id)
        if not isinstance(entry, dict):
            entry = rules[rule_id] = {}
        entry["enabled"] = action == "enable"
    save(path, config)
    status(path, config)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
