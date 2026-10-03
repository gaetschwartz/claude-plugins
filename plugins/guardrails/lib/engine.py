"""Evaluate one Bash command against the effective rules; the body of the PreToolUse hook."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import IO, Any

import matching
import policy
import store
import wrappers as wrapper_table
from verdict import FAILED_PREFIX, Evaluation

Session = dict[str, Any]
Output = dict[str, Any]


def digest(text: str) -> str:
    import hashlib

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


REPEAT_AFTER = 600.0


def due(session: Session, key: str) -> bool:
    """True when this warning should be shown now: once per session, except engine failures, which repeat."""
    if not key.startswith(FAILED_PREFIX):
        return remember(session, "reported", key)
    stamps = session.get("reportedAt")
    if not isinstance(stamps, dict):
        stamps = session["reportedAt"] = {}
    now = time.time()
    last = stamps.get(key)
    if isinstance(last, (int, float)) and 0 <= now - last < REPEAT_AFTER:
        return False
    stamps[key] = now
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


TOOLS = ("Bash", "Monitor")


def applies(rule: policy.Rule, tool: str) -> bool:
    """A rule for Bash also covers Monitor, whose command the shell runs the same way."""
    return rule.get("tool") == tool or (tool == "Monitor" and rule.get("tool") == "Bash")


def candidates_of(rules: dict[str, policy.Rule], tool: str = "Bash") -> dict[str, policy.Rule]:
    """The rules that could act on this command here: enabled, for the tool, valid, binaries installed."""
    out: dict[str, policy.Rule] = {}
    for rid in sorted(rules):
        rule = rules[rid]
        if rule.get("enabled") is not True or not applies(rule, tool):
            continue
        try:
            policy.validate_rule(rule)
        except policy.Invalid:
            continue
        if policy.requirements_met(rule):
            out[rid] = rule
    return out


def oversized_of(rules: dict[str, policy.Rule], tool: str = "Bash") -> list[str]:
    """Enabled rules for this tool whose match.ast is over the size limit (the hook skips them and says so)."""
    return [rid for rid in sorted(rules) if rules[rid].get("enabled") is True and applies(rules[rid], tool)
            and (ast := policy.ast_of(rules[rid])) and policy.ast_size(ast) > policy.MAX_AST_BYTES]


def evaluate(command: str, rules: dict[str, policy.Rule], modes: dict[str, policy.Mode],
             session: Session, session_id: str, managed_ids: frozenset[str] = frozenset(),
             warnings: tuple[str, ...] = (), wrappers: wrapper_table.Names | None = None,
             pre: Evaluation | None = None,
             tool: str = "Bash") -> tuple[Output | None, bool]:
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

    for rid in oversized_of(rules, tool):
        text = (f"guardrails: match.ast rule {rid} is larger than {policy.MAX_AST_BYTES // 1024} KiB and is skipped; "
                "split it into several rules")
        if remember(session, "reported", digest(text)):
            changed = True
            notices.append(text)
    candidates = candidates_of(rules, tool)
    evaluation = pre if pre is not None and all(rid in pre.kinds for rid in candidates) \
        else matching.evaluate(command, candidates, wrappers)
    agent_notes: list[str] = []
    for key, warning in evaluation.warnings(managed_ids):
        if due(session, key):
            changed = True
            notices.append(warning)
            agent_notes.append(warning)
            if key.startswith(FAILED_PREFIX):
                session["engineFailure"] = {"kind": evaluation.failure_kind(), "at": store.now(),
                                            "reason": str(evaluation.failure)[:200]}

    def unsuspended(rule: policy.Rule) -> bool:
        return not any(name in active for name in policy.modes_of(rule))

    refused = bool(evaluation.refusal) and any(
        candidates[rid].get("action") == "deny" and unsuspended(candidates[rid])
        for rid in evaluation.unevaluated if rid in candidates)
    if evaluation.refusal and not refused:
        key = FAILED_PREFIX + evaluation.failure_kind()
        if due(session, key):
            changed = True
            text = (f"guardrails: {evaluation.refusal}; the command was allowed because only warn rules could not "
                    "be checked.")
            notices.append(text)
            agent_notes.append(text)

    for rid, rule in candidates.items():
        if evaluation.kinds.get(rid) is None:
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
    extra = "\n\n" + "\n".join(agent_notes) if agent_notes else ""
    if denies or refused:
        text, composed = compose(denies + warns, modes, session, shown_before, session_id, managed_ids)
        changed = changed or composed
        if refused:
            refusal = (f"[guardrails] Denied: {evaluation.refusal}. Rules that use program, args, builtin or match.ast "
                       "cannot be evaluated on it. Split it up or put the content in a file.")
            text = "\n\n".join(filter(None, [refusal, text]))
        output["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": text + extra}
    else:
        fresh = [(rid, rule) for rid, rule in warns if remember(session, "warned", rid)]
        if fresh:
            changed = True
            text, _ = compose(fresh, modes, session, shown_before, session_id, managed_ids)
            output["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "additionalContext": text + extra}
        elif agent_notes:
            output["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "additionalContext": extra.strip()}
    if notices:
        output["systemMessage"] = "\n".join(notices)
    return (output or None), changed


def run_hook(stdin: IO[str], stdout: IO[str]) -> None:
    try:
        payload = json.load(stdin)
    except ValueError:
        return
    tool = payload.get("tool_name") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or tool not in TOOLS:
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
    managed, managed_problems = store.load_managed()
    warnings = tuple(f"guardrails: {problem}" for problem in managed_problems)
    for warning in warnings:
        print(warning, file=sys.stderr)
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

    def wrapper_layers(g: store.State) -> wrapper_table.Names:
        killed = not gstate_ok or g.get("enabled", True) is False
        return policy.effective_wrappers(*((managed, {}, {}) if killed else (managed, g, pstate)))

    if gstate_ok and gstate.get("enabled", True) is not False:
        warnings += tuple(f"guardrails: {problem}" for problem in policy.wrapper_problems(managed, gstate, pstate))
    if not layers(gstate)[0] and not warnings:
        return

    sid = str(payload.get("session_id") or "nosession")
    output: Output | None = None
    pre = matching.evaluate(command, candidates_of(layers(gstate)[0], tool), wrapper_layers(gstate))
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
                output, changed = evaluate(command, rules, modes, session, sid, managed_ids, warnings,
                                           wrapper_layers(gstate), pre, tool)
                if changed:
                    session["seenAt"] = store.now()
                    sessions[sid] = session
                    gstate["sessions"] = sessions
                    store.write(gpath, gstate)
        except OSError:
            stateless = True
    if stateless:
        rules, modes = layers(gstate)
        output, _ = evaluate(command, rules, modes, {}, sid, managed_ids, warnings, wrapper_layers(gstate), pre, tool)
    if output:
        json.dump(output, stdout)


def run_safe(text: str, stdout: IO[str], failure: BaseException) -> None:
    """After run_hook failed: allow, loudly. No rule is applied: every matcher runs in the bounded checker."""
    payload = json.loads(text)
    tool = payload.get("tool_name") if isinstance(payload, dict) else None
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if tool not in TOOLS or not isinstance(command, str) or not command.strip():
        return
    notice = (f"[guardrails plugin notice] the guardrails hook failed internally ({type(failure).__name__}), so no rule "
              "was applied and this command was not checked. Tell the user about this now.")
    output: Output = {"systemMessage": notice, "hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                                       "additionalContext": notice}}
    json.dump(output, stdout)
