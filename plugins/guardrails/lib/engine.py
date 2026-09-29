"""Evaluate one Bash command against the effective rules; the body of the PreToolUse hook."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import IO, Any

import policy
import store
from shellwords import SimpleCommand, simple_commands

Session = dict[str, Any]
Output = dict[str, Any]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def remember(session: Session, key: str, value: str) -> bool:
    """Add value to the list session[key]; True if it was not there yet."""
    items = session.get(key)
    if not isinstance(items, list):
        items = session[key] = []
    if value in items:
        return False
    items.append(value)
    return True


def hints(rule: policy.Rule, modes: dict[str, policy.Mode], session_id: str) -> str:
    if rule.get("action") != "deny":
        return ""
    text = ""
    if rule.get("retry") == "same-command":
        text += " If you still need this exact command, re-run it unchanged to proceed."
    for name in policy.modes_of(rule):
        if name not in modes:
            continue
        if modes[name]["agentMayEnable"]:
            text += (f" If this session is genuinely '{name}' work, ask the user; once they confirm, "
                     "enable it with the guardrails:mode skill.")
        else:
            guard_py = os.path.join(store.HERE, "guard.py")
            text += (f" If this is '{name}' work, the user can enable it from their terminal: "
                     f"python3 {guard_py} mode on {name} --session-id {session_id}")
    return text


def compose(items: list[tuple[str, policy.Rule]], modes: dict[str, policy.Mode], session: Session,
            shown_before: set[str], session_id: str, managed_ids: frozenset[str] = frozenset()) -> tuple[str, bool]:
    """Render texts (messageShort once the full message was shown), merging rules that render identically."""
    changed = False
    groups: dict[str, list[str]] = {}
    tails: dict[str, str] = {}
    for rid, rule in items:
        full = policy.render(rule["message"])
        text = full
        short = rule.get("messageShort")
        if isinstance(short, str) and short:
            if digest(full) in shown_before:
                text = policy.render(short)
            elif remember(session, "shown", digest(full)):
                changed = True
        groups.setdefault(text, []).append(f"{rid} (managed)" if rid in managed_ids else rid)
        tails.setdefault(text, hints(rule, modes, session_id))
    paragraphs = [f"[guardrails:{', '.join(ids)}] {text}{tails[text]}" for text, ids in groups.items()]
    return "\n\n".join(paragraphs), changed


def evaluate(command: str, rules: dict[str, policy.Rule], modes: dict[str, policy.Mode],
             session: Session, session_id: str, managed_ids: frozenset[str] = frozenset(),
             warnings: tuple[str, ...] = ()) -> tuple[Output | None, bool]:
    try:
        cmds: list[SimpleCommand] | None = simple_commands(command)
    except ValueError:
        cmds = None
    active = policy.active_modes(modes, session)
    shown = session.get("shown")
    shown_before = {x for x in shown if isinstance(x, str)} if isinstance(shown, list) else set()
    changed = False
    notices: list[str] = []
    for warning in warnings:
        if remember(session, "reported", digest(warning)):
            changed = True
            notices.append(warning)
    denies: list[tuple[str, policy.Rule]] = []
    warns: list[tuple[str, policy.Rule]] = []

    for rid in sorted(rules):
        rule = rules[rid]
        if rule.get("enabled") is not True or rule.get("tool") != "Bash":
            continue
        try:
            policy.validate_rule(rule)
        except policy.Invalid as exc:
            if rid in managed_ids:
                print(f"guardrails: managed rule {rid} is invalid and ignored: {exc}", file=sys.stderr)
            continue
        if not policy.requirements_met(rule) or not policy.rule_matches(rule, command, cmds):
            continue
        suspending = [name for name in policy.modes_of(rule) if name in active]
        if suspending:
            name = suspending[0]
            record = active[name]
            if record.get("by") == "agent" and remember(session, "reported", f"{rid}:{name}"):
                changed = True
                notices.append(f"guardrails: rule {rid} suspended by mode {name} "
                               f"(enabled by agent: {record.get('reason') or 'no reason given'})")
            continue
        if rule["action"] == "warn":
            warns.append((rid, rule))
            continue
        if rule.get("retry") == "same-command":
            if not remember(session, "acknowledged", f"{rid}:{digest(command)}"):
                continue
            changed = True
        denies.append((rid, rule))

    output: Output = {}
    if denies:
        text, composed = compose(denies + warns, modes, session, shown_before, session_id, managed_ids)
        changed = changed or composed
        output["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": text}
    else:
        fresh = [(rid, rule) for rid, rule in warns if remember(session, "warned", rid)]
        if fresh:
            changed = True
            text, _ = compose(fresh, modes, session, shown_before, session_id, managed_ids)
            output["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "additionalContext": text}
    if notices:
        output["systemMessage"] = "\n".join(notices)
    return (output or None), changed


def run_hook(stdin: IO[str], stdout: IO[str]) -> None:
    try:
        payload = json.load(stdin)
    except ValueError:
        return
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return

    gpath = store.global_state_path()
    try:
        gstate, gstate_ok = store.load(gpath), True
    except store.StateError:
        gstate, gstate_ok = {}, False
    managed, managed_error = store.load_managed()
    warnings: tuple[str, ...] = ()
    if managed_error:
        text = f"unreadable managed state, so managed rules are NOT enforced until it is fixed: {managed_error}"
        print(f"guardrails: {text}", file=sys.stderr)
        warnings = (f"guardrails: {text}",)
    managed_ids = frozenset(policy.origins("rules", managed, {}, {}))
    cwd = payload.get("cwd")
    try:
        pstate = store.load(store.project_state_path(cwd if isinstance(cwd, str) else None))
    except store.StateError:
        pstate = {}

    def layers(g: store.State) -> tuple[dict[str, policy.Rule], dict[str, policy.Mode]]:
        killed = not gstate_ok or g.get("enabled", True) is False
        args = (managed, {}, {}) if killed else (managed, g, pstate)
        return policy.effective_rules(*args), policy.effective_modes(*args)

    if not layers(gstate)[0] and not warnings:
        return

    sid = str(payload.get("session_id") or "nosession")
    output: Output | None = None
    stateless = not gstate_ok
    if gstate_ok:
        try:
            with store.locked(gpath):
                gstate = store.load(gpath)
                sessions_raw = gstate.get("sessions")
                sessions = sessions_raw if isinstance(sessions_raw, dict) else {}
                session_raw = sessions.get(sid)
                session = session_raw if isinstance(session_raw, dict) else {}
                rules, modes = layers(gstate)
                output, changed = evaluate(command, rules, modes, session, sid, managed_ids, warnings)
                if changed:
                    session["seenAt"] = store.now()
                    sessions[sid] = session
                    gstate["sessions"] = sessions
                    store.write(gpath, gstate)
        except OSError:
            stateless = True
    if stateless:
        rules, modes = layers(gstate)
        output, _ = evaluate(command, rules, modes, {}, sid, managed_ids, warnings)
    if output:
        json.dump(output, stdout)
