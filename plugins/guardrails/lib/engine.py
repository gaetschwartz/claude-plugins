"""Evaluate one Bash command against the effective rules; the body of the PreToolUse hook."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import IO, TypedDict

import bootstrap
import matching
import policy
import store
import wrappers as wrapper_table
from verdict import FAILED_PREFIX, Evaluation


class HookDecision(TypedDict, total=False):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str
    additionalContext: str


class Output(TypedDict, total=False):
    systemMessage: str
    hookSpecificOutput: HookDecision


def digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


def remember(items: list[str], value: str) -> bool:
    """Add value to the list; True if it was not there yet."""
    if value in items:
        return False
    items.append(value)
    return True


REPEAT_AFTER = float(bootstrap.REPEAT_SECONDS)


def due(session: policy.Session, key: str) -> bool:
    """True when this warning should be shown now: once per session, except engine failures, which repeat."""
    if not key.startswith(FAILED_PREFIX):
        return remember(session.reported, key)
    now = time.time()
    last = session.reported_at.get(key)
    if last is not None and 0 <= now - last < REPEAT_AFTER:
        return False
    session.reported_at[key] = now
    return True


def hints(rule: policy.Rule, modes: dict[str, policy.Mode], session_id: str) -> str:
    if rule.action is not policy.Action.DENY:
        return ""
    text = ""
    if rule.retry is policy.Retry.SAME_COMMAND:
        text += " If you still need this exact command, re-run it unchanged to proceed."
    for name in rule.modes:
        if name not in modes:
            continue
        if modes[name].agent_may_enable:
            text += (f" If this session is genuinely '{name}' work, ask the user; once they confirm, "
                     "enable it with the guardrails:mode skill.")
        else:
            text += (f" If this is '{name}' work, the user can enable it from their terminal: "
                     f"{store.CLI} mode on {name} --session-id {session_id}")
    return text


def compose(items: list[tuple[str, policy.Rule]], modes: dict[str, policy.Mode], session: policy.Session,
            shown_before: set[str], session_id: str, managed_ids: frozenset[str] = frozenset()) -> tuple[str, bool]:
    """Render texts (messageShort once the full message was shown), merging rules that render identically."""
    changed = False
    groups: dict[str, list[str]] = {}
    tails: dict[str, str] = {}
    for rid, rule in items:
        full = policy.render(rule.message)
        text = full
        if rule.message_short:
            if digest(full) in shown_before:
                text = policy.render(rule.message_short)
            elif remember(session.shown, digest(full)):
                changed = True
        groups.setdefault(text, []).append(f"{rid} (managed)" if rid in managed_ids else rid)
        tails.setdefault(text, hints(rule, modes, session_id))
    paragraphs = [f"[guardrails:{', '.join(ids)}] {text}{tails[text]}" for text, ids in groups.items()]
    return "\n\n".join(paragraphs), changed


TOOLS = ("Bash", "Monitor")


def candidates_of(rules: dict[str, policy.Rule]) -> dict[str, policy.Rule]:
    """The rules that could act on this command here: enabled and with their binaries installed."""
    return {rid: rules[rid] for rid in sorted(rules) if rules[rid].enabled and policy.requirements_met(rules[rid])}


def report_broken() -> None:
    """The library crashes even on a trivial command: mark the runtime broken and rebuild it in the background."""
    import bootstrap
    import hostcli

    bootstrap.mark_broken(bootstrap.data_dir())
    hostcli.spawn_ensure()


def judge(command: str, rules: dict[str, policy.Rule], wrappers: wrapper_table.Names | None) -> Evaluation:
    evaluation = matching.evaluate(command, rules, wrappers)
    if evaluation.runtime_broken:
        report_broken()
    return evaluation


def evaluate(command: str, rules: dict[str, policy.Rule], modes: dict[str, policy.Mode],
             session: policy.Session, session_id: str, managed_ids: frozenset[str] = frozenset(),
             warnings: tuple[str, ...] = (), wrappers: wrapper_table.Names | None = None,
             pre: Evaluation | None = None,
) -> tuple[Output | None, bool]:
    active = policy.active_modes(modes, session)
    shown_before = set(session.shown)
    changed = False
    notices: list[str] = []
    for warning in warnings:
        if remember(session.reported, digest(warning)):
            changed = True
            notices.append(warning)
    denies: list[tuple[str, policy.Rule]] = []
    warns: list[tuple[str, policy.Rule]] = []

    candidates = candidates_of(rules)
    evaluation = pre if pre is not None and all(rid in pre.kinds for rid in candidates) \
        else judge(command, candidates, wrappers)
    agent_notes: list[str] = []
    for key, warning in evaluation.warnings(managed_ids):
        if due(session, key):
            changed = True
            notices.append(warning)
            agent_notes.append(warning)
            if key.startswith(FAILED_PREFIX):
                session.engine_failure = policy.EngineFailure(evaluation.failure_kind(), store.now(),
                                                              str(evaluation.failure)[:200])

    def unsuspended(rule: policy.Rule) -> bool:
        return not any(name in active for name in rule.modes)

    refused = bool(evaluation.refusal) and any(
        candidates[rid].action is policy.Action.DENY and unsuspended(candidates[rid])
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
        suspending = [name for name in rule.modes if name in active]
        if suspending:
            name = suspending[0]
            record = active[name]
            if record.by is policy.Actor.AGENT and remember(session.reported, f"{rid}:{name}"):
                changed = True
                notices.append(f"guardrails: rule {rid} suspended by mode {name} "
                               f"(enabled by agent: {record.reason or 'no reason given'})")
            continue
        if rule.action is policy.Action.WARN:
            warns.append((rid, rule))
            continue
        if rule.retry is policy.Retry.SAME_COMMAND:
            if not remember(session.acknowledged, f"{rid}:{digest(command)}"):
                continue
            changed = True
        denies.append((rid, rule))

    output: Output = {}
    extra = "\n\n" + "\n".join(agent_notes) if agent_notes else ""
    if denies or refused:
        text, composed = compose(denies + warns, modes, session, shown_before, session_id, managed_ids)
        changed = changed or composed
        if refused:
            refusal = (f"[guardrails] Denied: {evaluation.refusal}. Rules cannot be evaluated on it. Split it up or put "
                       "the content in a file.")
            text = "\n\n".join(filter(None, [refusal, text]))
        output["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": text + extra}
    else:
        fresh = [(rid, rule) for rid, rule in warns if remember(session.warned, rid)]
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
        pstate = store.load(store.project_state_path(Path(cwd) if isinstance(cwd, str) else None))
    except store.StateError:
        pstate = {}

    def layers(g: store.State) -> policy.Effective:
        killed = not gstate_ok or g.get("enabled", True) is False
        return policy.effective(*((managed, {}, {}) if killed else (managed, g, pstate)))

    def modes_of(g: store.State) -> dict[str, policy.Mode]:
        killed = not gstate_ok or g.get("enabled", True) is False
        return policy.effective_modes(*((managed, {}, {}) if killed else (managed, g, pstate)))

    def wrapper_layers(g: store.State) -> wrapper_table.Names:
        killed = not gstate_ok or g.get("enabled", True) is False
        return policy.effective_wrappers(*((managed, {}, {}) if killed else (managed, g, pstate)))

    if gstate_ok and gstate.get("enabled", True) is not False:
        warnings += tuple(f"guardrails: {problem}" for problem in policy.wrapper_problems(managed, gstate, pstate))
    found = layers(gstate)
    warnings += tuple(f"guardrails: rule {rid} is invalid ({why}) and is skipped" for rid, why in found.problems.items())
    if not found.rules and not warnings:
        return

    sid = bootstrap.session_id(payload)
    output: Output | None = None
    pre = judge(command, candidates_of(found.rules), wrapper_layers(gstate))
    stateless = not gstate_ok
    if gstate_ok:
        try:
            with store.locked(gpath):
                gstate = store.load(gpath)
                sessions = policy.view(gstate, "sessions")
                session = policy.Session.from_json(sessions.get(sid))
                output, changed = evaluate(command, layers(gstate).rules, modes_of(gstate), session, sid, managed_ids,
                                           warnings, wrapper_layers(gstate), pre)
                if changed:
                    sessions[sid] = session.to_json(store.now())
                    gstate["sessions"] = sessions
                    store.write(gpath, gstate)
        except OSError:
            stateless = True
    if stateless:
        output, _ = evaluate(command, layers(gstate).rules, modes_of(gstate), policy.Session(), sid, managed_ids,
                             warnings, wrapper_layers(gstate), pre)
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
