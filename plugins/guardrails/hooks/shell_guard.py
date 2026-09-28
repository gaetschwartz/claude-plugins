# /// script
# requires-python = ">=3.9"
# ///
"""PreToolUse entry point: parse the Bash command once, run every enabled rule.

Rules live in rules/ and expose RULES ({id: description}), KEYWORDS (cheap
substring pre-filter) and check(ctx) -> Decision | None. The first deny wins;
warnings from all rules are collected.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from context import Context
from rules import MODULES


class RuleConfig(TypedDict, total=False):
    enabled: bool


class Config(TypedDict, total=False):
    rules: dict[str, RuleConfig]


def config_path() -> Path | None:
    explicit = os.environ.get("SHELL_GUARD_CONFIG")
    if explicit:
        return Path(explicit)
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        return Path(data) / "config.json"
    hits = sorted(Path.home().glob(".claude/plugins/data/shell-guard-*/"))
    return hits[0] / "config.json" if len(hits) == 1 else None


def load_config(path: Path | None) -> Config:
    if path is None:
        return {}
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def rule_enabled(config: Config, rule_id: str) -> bool:
    rules = config.get("rules")
    entry = rules.get(rule_id) if isinstance(rules, dict) else None
    return not isinstance(entry, dict) or entry.get("enabled", True) is not False


def enabled_ids(config: Config) -> frozenset[str]:
    return frozenset(rule_id for mod in MODULES for rule_id in mod.RULES if rule_enabled(config, rule_id))


def command_of(payload: object) -> tuple[str, str | None]:
    if not isinstance(payload, dict):
        return "", None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    transcript = payload.get("transcript_path")
    return (command if isinstance(command, str) else "",
            transcript if isinstance(transcript, str) else None)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return

    command, transcript_path = command_of(payload)
    if not command:
        return
    ctx = Context(command, transcript_path, enabled_ids(load_config(config_path())))

    warnings: list[str] = []
    for mod in MODULES:
        if not (set(mod.RULES) & ctx.enabled):
            continue
        if not any(k in ctx.command for k in mod.KEYWORDS):
            continue
        result = mod.check(ctx)
        if not result:
            continue
        kind, message = result
        if kind == "deny":
            json.dump({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message,
                }
            }, sys.stdout)
            return
        warnings.append(message)

    if warnings:
        json.dump({"systemMessage": "\n".join(warnings)}, sys.stdout)


if __name__ == "__main__":
    main()
