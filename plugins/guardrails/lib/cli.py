"""Command-line interface for guardrails rules, modes and presets."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import bootstrap
import hostcli
import matching
import policy
import render
import store
from policy import Invalid, view
from verdict import Evaluation

PRESETS_DIR = store.HERE.parent / "presets"
SETTABLE = ("action", "retry", "enabled", "modes", "message", "messageShort", "description",
            "program", "args", "regex", "ast", "requires")
SCOPES = ("global", "project", "managed")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

Args = argparse.Namespace


class Refused(Exception):
    """An agent tried something only the user may do."""


def is_agent() -> bool:
    return bool(os.environ.get("CLAUDECODE"))


def stamp(reason: str | None) -> dict[str, str]:
    out = {"by": "agent" if is_agent() else "user", "at": store.now()}
    if reason:
        out["reason"] = reason
    return out


def require_user(args: Args, what: str) -> None:
    if is_agent() and not args.as_user:
        raise Refused(f"{what} changes guardrails configuration. Only do it when the user explicitly asked for "
                      "this change, and then pass --as-user. Never use it to get past a denial.")


def forbid_agent(what: str) -> None:
    if is_agent():
        raise Refused(f"{what} can only be done by the user from their own terminal, not by an agent.")


def check_name(kind: str, name: str) -> None:
    if not NAME.fullmatch(name):
        raise Invalid(f"{kind} name {name!r} must match {NAME.pattern}")


def table(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    """mapping[key] as a dict, created in place when absent; a value of another type is a corrupt state file."""
    value = mapping.setdefault(key, {})
    if not isinstance(value, dict):
        raise store.StateError(f"'{key}' must be an object, not {type(value).__name__}; fix the file by hand, the CLI "
                               "never overwrites a corrupt state file")
    return value


def project_path() -> Path:
    path = store.project_state_path()
    if not path:
        raise Invalid("no project state here: not inside a project (no CLAUDE_PROJECT_DIR and no git repository), or "
                      "the project's state file is the global one (a project at the home directory)")
    return path


def resolve_scope(args: Args) -> str:
    return getattr(args, "scope", None) or "global"


def scope_path(scope: str) -> Path:
    if scope == "managed":
        return store.MANAGED_PATH
    return project_path() if scope == "project" else store.global_state_path()


def change_state(scope: str, path: Path, fn: Callable[[store.State], Any]) -> Any:
    if scope != "managed":
        return store.mutate(path, fn)
    os.umask(0o022)
    try:
        return store.mutate(path, fn, public=True)
    except store.StateError as exc:
        raise store.StateError(f"{exc}; fix or remove the managed file by hand, the CLI never overwrites a corrupt "
                               "state file") from exc


def managed_state() -> store.State:
    return store.load_managed()[0]


def states() -> tuple[store.State, store.State, store.State]:
    """The managed, global and project state; an unreadable global or project file raises."""
    return managed_state(), store.load(store.global_state_path()), store.load(store.project_state_path())


def managed_rule_ids() -> set[str]:
    return set(policy.origins("rules", managed_state(), {}, {}))


def refuse_managed_rule(scope: str, rid: str, path: Path) -> None:
    if scope == "managed" or rid not in managed_rule_ids():
        return
    try:
        own = rid in view(store.load(path), "rules")
    except store.StateError:
        own = False
    if not own:
        raise Refused(f"rule '{rid}' is a managed rule. Change it with --scope managed (needs sudo and the "
                      "user's explicit request); other scopes cannot alter it.")


def always_enforced(scope: str, rid: str, rule: policy.Rule) -> str:
    if scope == "managed" and not rule.modes:
        return f"note: managed rule {rid} lists no modes, so it is always enforced and cannot be suspended"
    return ""


def session_id(args: Args) -> str:
    sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise Invalid("no session id: pass --session-id or run inside Claude Code (CLAUDE_CODE_SESSION_ID)")
    return sid


def read_arg(value: str, flag: str) -> str:
    """The text of a flag value: literal, @<file> (~ expanded), or - for stdin."""
    if value == "-":
        return sys.stdin.read()
    if not value.startswith("@"):
        return value
    path = Path(value[1:]).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Invalid(f"{flag}: cannot read {path}: {exc.strerror or exc}") from exc


def load_json(value: str, flag: str) -> Any:
    try:
        return json.loads(read_arg(value, flag))
    except ValueError as exc:
        where = f" in {Path(value[1:]).expanduser()}" if value.startswith("@") else ""
        raise Invalid(f"{flag} is not valid JSON{where}: {exc}") from exc


def check_stdin(*pairs: tuple[str | None, str]) -> None:
    if sum(value == "-" for value, _ in pairs) > 1:
        raise Invalid(f"only one of {' and '.join(flag for _, flag in pairs)} can read stdin")


@dataclass
class Snapshot:
    mstate: store.State
    gstate: store.State
    pstate: store.State
    gpath: Path
    ppath: Path | None
    hook_on: bool
    rules: dict[str, policy.Rule]
    modes: dict[str, policy.Mode]
    active: dict[str, policy.Activation]
    rule_origins: dict[str, list[str]]
    mode_origins: dict[str, list[str]]
    problems: list[str]
    problem_layers: list[frozenset[str]]
    blind: frozenset[str] = frozenset()

    def problems_in(self, scope: str | None) -> list[str]:
        return [p for p, layers in zip(self.problems, self.problem_layers) if scope is None or scope in layers]


def snapshot(args: Args) -> Snapshot:
    mstate, problems = store.load_managed()
    layers: list[frozenset[str]] = [frozenset({"managed"})] * len(problems)

    def report(text: str, *where: str) -> None:
        problems.append(text)
        layers.append(frozenset(where))

    def safe_load(path: Path | None, what: str) -> store.State:
        try:
            return store.load(path)
        except store.StateError as exc:
            if what == "global":
                consequence = ("global and project rules are not enforced, managed rules still are"
                               if view(mstate, "rules") else "the hook fails open")
            else:
                consequence = "project rules are not enforced"
            report(f"unreadable {what} state file ({consequence}) until it is fixed: {exc}", what)
            return {}

    gpath, ppath = store.global_state_path(), store.project_state_path()
    gstate, pstate = safe_load(gpath, "global"), safe_load(ppath, "project")
    for text in store.trust_problems(store.MANAGED_PATH):
        report(text, "managed")
    for name, m in view(pstate, "modes").items():
        if name in view(mstate, "modes") and isinstance(m, dict) and m.get("active") is True:
            report(f"project state switches on mode '{name}', which the managed file declares (ignored)", "project")
    found = policy.effective(mstate, gstate, pstate)
    rules, modes = found.rules, policy.effective_modes(mstate, gstate, pstate)
    sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    session = policy.Session.from_json(view(view(gstate, "sessions"), sid) if sid else {})
    rule_origins = policy.origins("rules", mstate, gstate, pstate)
    mode_origins = policy.origins("modes", mstate, gstate, pstate)
    for rid, why in sorted(found.problems.items()):
        report(f"rule {rid}: {why} (ignored by the hook)", *rule_origins.get(rid, []))
    for rid in sorted(rules):
        for m in rules[rid].modes:
            if m not in modes:
                report(f"rule {rid}: mode '{m}' is not declared (the rule stays enforced)",
                       *rule_origins.get(rid, []))
    for label, state in (("global", gstate), ("project", pstate)):
        for text in policy.removed_key_problems((label, state)):
            report(text, label)
    parsed = {rid: rule for rid, rule in rules.items() if rule.enabled
              and not any(p.startswith(f"rule {rid}:") for p in problems)}
    failures = [f for f in (view(v, "engineFailure") for v in view(gstate, "sessions").values()) if f.get("at")]
    if failures:
        last = max(failures, key=lambda f: str(f["at"]))
        report(f"last engine failure: {last.get('kind')} at {last['at']} ({last.get('reason')}); commands that hit it were "
               "allowed with a warning", "global")
    blind: set[str] = set()
    if parsed:
        where = sorted({layer for rid in parsed for layer in rule_origins.get(rid, [])})
        try:
            for rid, why in matching.check(parsed).items():
                report(f"rule {rid}: does not compile ({why}) (skipped by the hook)", *rule_origins.get(rid, []))
        except matching.EngineError as exc:
            blind = set(parsed)
            managed_blind = sorted(rid for rid in parsed if "managed" in rule_origins.get(rid, []))
            fails_open = f" MANAGED rules fail open too: {', '.join(managed_blind)}." if managed_blind else ""
            report(f"rules {', '.join(sorted(parsed))} use the ast-grep engine and are NOT enforced "
                   f"while the engine cannot run ({exc}).{fails_open}", *where)
    return Snapshot(mstate, gstate, pstate, gpath, ppath, gstate.get("enabled", True) is not False,
                    rules, modes, policy.active_modes(modes, session), rule_origins, mode_origins, problems, layers,
                    frozenset(blind))


def rule_state(rule: policy.Rule, layers: list[str], active: dict[str, policy.Activation], blind: bool = False) -> str:
    if not rule.enabled:
        return "disabled"
    suspended = [m for m in rule.modes if m in active]
    if suspended:
        return "suspended by " + ", ".join(suspended)
    if blind:
        return "NOT enforced: engine unavailable"
    if "managed" in layers and not rule.modes:
        return "always enforced"
    return "enabled"


def managed_line() -> str:
    where, state = render.span(str(store.MANAGED_PATH)), store.presence(store.MANAGED_PATH)
    return f"**Managed** platform file {where} {state}" + (
        " · no managed file is present, so there are no managed rules" if state == "absent" else "")


def status_view(snap: Snapshot, scope: str | None) -> render.Status:
    def keep(origins: dict[str, list[str]], name: str) -> bool:
        return scope is None or scope in origins.get(name, [])

    rules = [render.RuleRow(rid, str(snap.rules[rid].action), snap.rule_origins.get(rid, []),
                            rule_state(snap.rules[rid], snap.rule_origins.get(rid, []), snap.active, rid in snap.blind))
             for rid in sorted(snap.rules) if keep(snap.rule_origins, rid)]
    modes = []
    for name in sorted(snap.modes):
        if not keep(snap.mode_origins, name):
            continue
        if name in snap.active:
            record = snap.active[name]
            why = f": {record.reason}" if record.reason else ""
            on = "on (persistent)" if snap.modes[name].active else f"on (by {record.by}{why})"
        else:
            on = "off"
        modes.append(render.ModeRow(name, on, snap.modes[name].agent_may_enable, snap.mode_origins.get(name, [])))
    notes = []
    if not snap.hook_on:
        reason = f" ({snap.gstate['disabledReason']})" if snap.gstate.get("disabledReason") else ""
        kept = "; managed rules stay enforced" if view(snap.mstate, "rules") else ""
        notes.append(f"the global hook is disabled{reason}{kept}")
    status = render.Status(managed_line(), snap.hook_on, snap.ppath is not None
                           and snap.pstate.get("enabled", True) is False, rules, modes, snap.problems_in(scope),
                           notes=notes)
    if scope:
        status.no_rules = f"No rules with a {scope} entry."
    return status


def print_status(args: Args, snap: Snapshot) -> None:
    if args.problems:
        print(render.problems_listing(snap.problems_in(args.scope)))
        return
    status = status_view(snap, args.scope)
    if args.rule:
        rows = [r for r in status.rules if r.id == args.rule]
        if not rows:
            raise Invalid(f"no rule '{args.rule}'" + (f" with a {args.scope} entry" if args.scope else ""))
        print(render.rule_row(rows[0]))
        return
    print(render.status_listing(status))


def cmd_status(args: Args) -> int:
    print_status(args, snapshot(args))
    return 0


def check_ast_rule(rule: policy.Rule) -> str:
    """Raise Invalid when the rule does not compile; a note when it could not be checked."""
    try:
        errors = matching.check({"rule": rule})
    except matching.EngineError as exc:
        return (f"note: the rule was not compile-checked ({exc}); the hook skips a rule that does not compile and "
                "warns once per session")
    if errors:
        raise Invalid(f"the rule does not compile: {errors['rule']}")
    return ""


def cmd_rule_add(args: Args) -> int:
    require_user(args, "rule add")
    check_name("rule", args.id)
    rule, _ = split_envelope(load_json(args.json, "--json"))
    parsed = policy.Rule.from_json(rule)
    unchecked = check_ast_rule(parsed)
    rule["setBy"] = stamp(args.reason)
    scope = resolve_scope(args)
    path = scope_path(scope)

    def change(state: store.State) -> bool:
        rules = table(state, "rules")
        existed = args.id in rules
        rules[args.id] = rule
        return existed

    existed = change_state(scope, path, change)
    print(f"{'replaced' if existed else 'added'} rule {args.id} in {path}")
    if unchecked:
        print(unchecked)
    if scope != "managed" and args.id in managed_rule_ids():
        print(f"note: {args.id} is also a managed rule; this entry can only tighten it, not reword it")
    note = always_enforced(scope, args.id, parsed)
    if note:
        print(note)
    return 0


def apply_fields(rule: dict[str, Any], fields: dict[str, Any]) -> None:
    """Set each field (null removes it); match keys go under 'match'."""
    match = dict(view(rule, "match"))
    for key, value in fields.items():
        holder = match if key in policy.MATCH_KEYS else rule
        if value is None:
            holder.pop(key, None)
        else:
            holder[key] = value
    if any(key in policy.MATCH_KEYS for key in fields):
        rule["match"] = match


def ineffective(fields: dict[str, Any], base: policy.Rule) -> list[str]:
    notes = []
    for key, value in fields.items():
        shown = f"{key}={json.dumps(value)}"
        if (key, value) in (("action", "warn"), ("retry", "same-command"), ("enabled", False)):
            notes.append(shown)
        elif key == "modes":
            added = [m for m in value if m not in base.modes] if isinstance(value, list) else []
            if added:
                notes.append(f"modes {','.join(map(str, added))} (a project can only remove suspending modes)")
        elif key in policy.MATCH_KEYS or key == "requires":
            notes.append(f"{shown} (a project cannot change what a global rule matches)")
    return notes


def cmd_rule_set(args: Args) -> int:
    require_user(args, "rule set")
    scope = resolve_scope(args)
    path = scope_path(scope)
    fields = load_json(args.json, "--json")
    if not isinstance(fields, dict):
        raise Invalid("--json must be a JSON object of the fields to change")
    for key in fields:
        if key not in SETTABLE:
            raise Invalid(f"--json key {key!r} must be one of {', '.join(SETTABLE)}")
    refuse_managed_rule(scope, args.id, path)
    base = policy.effective_rules({}, store.load(store.global_state_path()), {}).get(args.id) \
        if scope == "project" else None

    unchecked: list[str] = []
    candidate: list[policy.Rule] = []

    def change(state: store.State) -> None:
        rules = table(state, "rules")
        if not isinstance(rules.get(args.id), dict):
            if base is None:
                raise Invalid(f"no rule '{args.id}' in {path}")
            rules[args.id] = {}
        rule = rules[args.id]
        apply_fields(rule, fields)
        candidate.append(policy.merge_rule(base, rule) if base is not None else policy.Rule.from_json(rule))
        if base is None:
            unchecked.append(check_ast_rule(candidate[-1]))
        rule["setBy"] = stamp(args.reason)

    change_state(scope, path, change)
    print(f"updated rule {args.id} in {path}")
    if unchecked and unchecked[-1]:
        print(unchecked[-1])
    if base is not None:
        notes = ineffective(fields, base)
        if notes:
            print("note: a project entry can only tighten a global rule; no effect: " + ", ".join(notes))
    note = always_enforced(scope, args.id, candidate[-1])
    if note:
        print(note)
    return 0


def cmd_rule_rm(args: Args) -> int:
    require_user(args, "rule rm")
    scope = resolve_scope(args)
    path = scope_path(scope)
    refuse_managed_rule(scope, args.id, path)

    def change(state: store.State) -> None:
        rules = table(state, "rules")
        if args.id not in rules:
            raise Invalid(f"no rule '{args.id}' in {path}")
        del rules[args.id]

    change_state(scope, path, change)
    print(f"removed rule {args.id} from {path}")
    return 0


def effect_notes(args: Args, rule: policy.Rule, layers: list[str], mstate: store.State,
                 gstate: store.State, pstate: store.State) -> list[str]:
    notes = []
    if not rule.enabled:
        notes.append("rule is disabled")
    if rule.requires and not policy.requirements_met(rule):
        notes.append(f"none of {'|'.join(rule.requires)} is installed here, so the hook skips this rule")
    if "managed" not in layers and gstate.get("enabled", True) is False:
        notes.append("the global hook is disabled, so the hook does not enforce this rule")
    listed = list(rule.modes)
    if listed:
        sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
        session = policy.Session.from_json(view(view(gstate, "sessions"), sid) if sid else {})
        active = policy.active_modes(policy.effective_modes(mstate, gstate, pstate), session)
        on = [m for m in listed if m in active]
        if on:
            notes.append(f"mode {', '.join(on)} is active, so the hook suspends this rule right now")
        else:
            notes.append(f"the hook suspends this rule while mode {' or '.join(listed)} is active (none is now)")
    return notes




class Example(NamedTuple):
    cmd: str
    source: render.Source
    expect: render.Expect | None


def split_envelope(document: Any) -> tuple[Any, Any]:
    """A {"rule": {...}, "examples": [...]} document carries both inputs through one stdin."""
    if isinstance(document, dict) and isinstance(document.get("rule"), dict):
        return document["rule"], document.get("examples")
    return document, None


def parse_examples(value: str) -> list[Example]:
    return example_list(load_json(value, "--examples"))


def example_list(data: Any) -> list[Example]:
    if not isinstance(data, list):
        raise Invalid('--examples must be a JSON list of {"cmd": "...", "source": "..."} objects')
    out: list[Example] = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            item = {"cmd": item}
        if not isinstance(item, dict) or not isinstance(item.get("cmd"), str) or not item["cmd"].strip():
            raise Invalid(f"--examples[{i}] needs a non-empty string 'cmd'")
        unknown = set(item) - {"cmd", "source", "expect"}
        if unknown:
            raise Invalid(f"--examples[{i}] has unknown keys: {', '.join(sorted(unknown))}")
        source, expect = item.get("source", "inferred"), item.get("expect")
        try:
            parsed_source = render.Source(source)
        except ValueError:
            raise Invalid(f"--examples[{i}] source must be one of {', '.join(render.Source)}") from None
        try:
            parsed_expect = None if expect is None else render.Expect(expect)
        except ValueError:
            raise Invalid(f"--examples[{i}] expect must be one of {', '.join(render.Expect)}") from None
        out.append(Example(item["cmd"], parsed_source, parsed_expect))
    return out


def outcome_of(ev: Evaluation, rid: str) -> render.Outcome:
    kind = ev.kinds[rid]
    if rid in ev.unevaluated:
        return render.Outcome.UNEVALUATED
    return render.Outcome(kind) if kind else render.Outcome.ALLOWED


def cannot_evaluate_note(ev: Evaluation) -> str:
    """Why this rule's program/args/regex/ast part could not be judged, when it could not."""
    if ev.refusal:
        return f"cannot evaluate the parsing part of this rule: {ev.refusal}; the hook denies such a command"
    return (f"cannot evaluate the parsing part of this rule (program, args, regex, match.ast): the engine failed "
            f"({ev.failure}); the hook allows the command and warns the session")


def cmd_rule_test(args: Args) -> int:
    check_stdin((args.json, "--json"), (args.examples, "--examples"))
    if args.id_name:
        check_name("rule", args.id_name)
    if any(not c.strip() for c in args.commands):
        raise Invalid("a command must not be blank")
    if args.json is None and (args.scope or args.id_name):
        raise Invalid("--scope and --id-name label a draft given with --json; --id reads them from the rule")
    examples: list[Example] = [Example(c, render.Source(args.source), None) for c in args.commands]
    if args.examples is not None:
        examples += parse_examples(args.examples)
    mstate, gstate, pstate = states()
    if args.json is not None:
        draft, carried = split_envelope(load_json(args.json, "--json"))
        if carried is not None:
            examples += example_list(carried)
        rule = policy.Rule.from_json(draft)
        layers: list[str] = []
        named = args.id_name or draft.get("id")
        rid = named if isinstance(named, str) and named else "new-rule"
        scope = args.scope or "global"
    else:
        found = policy.effective(mstate, gstate, pstate)
        rules = found.rules
        if args.id in found.problems:
            raise Invalid(f"rule '{args.id}' is invalid and ignored by the hook: {found.problems[args.id]}")
        if args.id not in rules:
            hidden = pstate.get("enabled", True) is False and args.id in view(pstate, "rules")
            raise Invalid(f"no rule '{args.id}'" + (" (project rules are disabled, so project entries are not "
                                                     "loaded)" if hidden else ""))
        rule = rules[args.id]
        layers = policy.origins("rules", mstate, gstate, pstate)[args.id]
        rid, scope = args.id, "+".join(layers)

    if not examples:
        raise Invalid("give at least one command or --examples")
    notes = effect_notes(args, rule, layers, mstate, gstate, pstate)
    evaluations = [matching.evaluate(cmd, {rid: rule}) for cmd, _, _ in examples]
    broken = {rid_: why for ev in evaluations for rid_, why in ev.invalid.items()}
    if broken:
        raise Invalid(f"the rule does not compile: {broken[rid]}")
    unjudged = next((ev for ev in evaluations if rid in ev.unevaluated), None)
    if unjudged:
        notes.append(cannot_evaluate_note(unjudged))
    results = [render.Result(cmd, source, outcome_of(ev, rid), expect)
               for (cmd, source, expect), ev in zip(examples, evaluations)]
    print(render.rule_card(rid, rule, policy.render(rule.message), scope,
                           args.intent or "", results, notes))
    return 0


def cmd_rule_ast(args: Args) -> int:
    if not args.command.strip():
        raise Invalid("the command must not be blank")
    try:
        units, limit = matching.tree(args.command)
    except matching.EngineError as exc:
        raise Invalid(f"the AST engine is unavailable: {exc}") from exc
    print(render.clean(f"command: {args.command}"))
    print(f"units: {len(units)} (1 as written, {len(units) - 1} from shell strings)")
    if limit:
        print(f"note: the command {limit}; the hook denies it and deeper units are not shown")
    for unit in units:
        print()
        if unit.label:
            print(render.clean(f"tree: {unit.label}, source: {unit.src}"))
        else:
            print("tree: command as written")
        for depth, kind, text in unit.rows:
            leaf = f" \u00ab{render.clean(text)}\u00bb" if text is not None else ""
            print(f"{'  ' * (depth + 1)}{render.clean(kind)}{leaf}")
        if unit.broken:
            print("note: the parser reported errors here (ERROR or MISSING nodes), so rules see a partial tree")
    return 0


def cmd_engine_ensure(args: Args) -> int:
    return hostcli.ensure_command(["--retry-now"] if args.retry_now else [])


def cmd_engine_status(args: Args) -> int:
    print(hostcli.status_text(bootstrap.data_dir()))
    return 0


def cmd_mode_declare(args: Args) -> int:
    require_user(args, "mode declare")
    check_name("mode", args.name)
    scope = resolve_scope(args)
    path = scope_path(scope)

    def change(state: store.State) -> None:
        modes = table(state, "modes")
        previous = view(modes, args.name)
        modes[args.name] = {"description": args.description or str(previous.get("description", "")),
                            "agentMayEnable": bool(args.agent_may_enable),
                            "active": previous.get("active") is True,
                            "setBy": stamp(args.reason)}

    change_state(scope, path, change)
    print(f"declared mode {args.name} in {path} (agent may enable: {'yes' if args.agent_may_enable else 'no'})")
    if scope != "managed" and args.name in view(managed_state(), "modes"):
        print(f"note: mode {args.name} is also declared in the managed file, which wins: this declaration can "
              "only tighten it")
    return 0


def cmd_mode_undeclare(args: Args) -> int:
    require_user(args, "mode undeclare")
    scope = resolve_scope(args)
    path = scope_path(scope)

    def change(state: store.State) -> None:
        modes = table(state, "modes")
        if args.name not in modes:
            raise Invalid(f"mode '{args.name}' is not declared in {path}")
        del modes[args.name]

    change_state(scope, path, change)
    print(f"removed mode {args.name} from {path}")
    return 0


def set_persistent(args: Args, active: bool) -> int:
    require_user(args, f"mode {'on' if active else 'off'} --scope {args.scope}")
    path = scope_path(args.scope)
    managed, gstate = managed_state(), store.load(store.global_state_path())
    above = {"managed": ({}, {}), "global": (managed, {}), "project": (managed, gstate)}[args.scope]
    declared_elsewhere = args.name in policy.effective_modes(*above, {})

    def change(state: store.State) -> None:
        modes = table(state, "modes")
        mode = modes.get(args.name)
        if not isinstance(mode, dict):
            if not declared_elsewhere:
                raise Invalid(f"mode '{args.name}' is not declared in {path}; declare it there first")
            mode = modes[args.name] = {}
        mode["active"] = active
        mode["setBy"] = stamp(args.reason)

    change_state(args.scope, path, change)
    print(f"mode {args.name} {'on' if active else 'off'} for every session ({args.scope} scope)")
    if not active and args.scope != "managed" and getattr(
            policy.effective_modes(managed, {}, {}).get(args.name), "active", False):
        print(f"note: the managed scope keeps mode {args.name} on")
    if active and args.scope == "project" and args.name in view(managed, "modes"):
        print(f"note: mode {args.name} is declared in the managed file, so a project cannot switch it on")
    return 0


def cmd_mode_on(args: Args) -> int:
    if args.scope != "session":
        return set_persistent(args, True)
    sid = session_id(args)
    modes = policy.effective_modes(*states())
    if args.name not in modes:
        raise Invalid(f"mode '{args.name}' is not declared (declared: {', '.join(sorted(modes)) or 'none'})")
    if is_agent():
        if not modes[args.name].agent_may_enable:
            forbid_agent(f"Enabling mode '{args.name}' (it does not allow agents to enable it)")
        if not args.reason:
            raise Invalid("--reason is required: quote what the user said about this session's work")

    def change(state: store.State) -> None:
        session = table(table(state, "sessions"), sid)
        table(session, "modes")[args.name] = stamp(args.reason)
        session["seenAt"] = store.now()

    store.mutate(store.global_state_path(), change)
    print(f"mode {args.name} enabled for session {sid}")
    return 0


def cmd_mode_off(args: Args) -> int:
    if args.scope != "session":
        return set_persistent(args, False)
    sid = session_id(args)

    def change(state: store.State) -> bool:
        return view(view(view(state, "sessions"), sid), "modes").pop(args.name, None) is not None

    removed = store.mutate(store.global_state_path(), change)
    print(f"mode {args.name} {'disabled' if removed else 'was not on'} for session {sid}")
    return 0


def set_enabled(args: Args, enabled: bool) -> int:
    forbid_agent("enable" if enabled else "disable")
    path = scope_path(resolve_scope(args))

    def change(state: store.State) -> None:
        state["enabled"] = enabled
        state["setBy"] = stamp(args.reason)
        if enabled:
            state.pop("disabledReason", None)
        else:
            state["disabledReason"] = args.reason or "disabled by user"

    store.mutate(path, change)
    print(f"{'project rules' if args.scope == 'project' else 'guardrails hook'} {'enabled' if enabled else 'disabled'} ({path})")
    return 0


def preset_names() -> list[str]:
    return sorted(path.stem for path in PRESETS_DIR.glob("*.json"))


def load_preset(name: str) -> dict[str, Any]:
    if name not in preset_names():
        raise Invalid(f"no preset '{name}' (available: {', '.join(preset_names()) or 'none'})")
    data = json.loads((PRESETS_DIR / f"{name}.json").read_text())
    if not isinstance(data, dict):
        raise Invalid(f"preset {name} is malformed")
    return data


def cmd_preset_list(args: Args) -> int:
    for name in preset_names():
        print(f"{name}: {load_preset(name).get('description', '')}")
    return 0


def cmd_preset_show(args: Args) -> int:
    print(json.dumps(load_preset(args.name), indent=2))
    return 0


def cmd_preset_install(args: Args) -> int:
    require_user(args, "preset install")
    preset = load_preset(args.name)
    rules = view(preset, "rules")
    only = [x.strip() for x in (args.only or "").split(",") if x.strip()]
    unknown = [x for x in only if x not in rules]
    if unknown:
        raise Invalid(f"preset {args.name} has no rule {', '.join(unknown)} (it has {', '.join(sorted(rules))})")
    chosen = {rid: r for rid, r in rules.items() if not only or rid in only}
    wanted = {m for r in chosen.values() for m in policy.json_modes(r)}
    modes = {name: m for name, m in view(preset, "modes").items() if name in wanted}
    scope = resolve_scope(args)
    path = scope_path(scope)
    by = stamp(args.reason or f"preset {args.name}")
    report: list[str] = []

    def change(state: store.State) -> None:
        srules = table(state, "rules")
        for rid, rule in sorted(chosen.items()):
            current = srules.get(rid)
            if isinstance(current, dict) and {k: v for k, v in current.items() if k != "setBy"} == rule:
                report.append(f"rule {rid}: unchanged")
                continue
            always = " (no modes: always enforced)" if scope == "managed" and not policy.json_modes(rule) else ""
            report.append(f"rule {rid}: {'replaced' if rid in srules else 'added'}{always}")
            srules[rid] = {**rule, "setBy": by}
        smodes = table(state, "modes")
        for name, mode in sorted(modes.items()):
            if name in smodes:
                report.append(f"mode {name}: kept existing declaration")
                continue
            agent = mode.get("agentMayEnable") is True
            smodes[name] = {"description": str(mode.get("description", "")), "agentMayEnable": agent,
                            "active": False, "setBy": by}
            report.append(f"mode {name}: added (agent may enable: {'yes' if agent else 'no'})")

    change_state(scope, path, change)
    print(f"installed preset {args.name} into {path}")
    for line in report:
        print(f"  {line}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as-user", action="store_true", help="agents only: the user explicitly asked for this change")
    common.add_argument("--session-id", help="session to act on (default: $CLAUDE_CODE_SESSION_ID)")
    common.add_argument("--reason", help="recorded with the change")
    scoped = argparse.ArgumentParser(add_help=False)
    scoped.add_argument("--scope", choices=SCOPES, help="which state file to change (default: global); "
                        "managed needs root: sudo guardrails --scope managed ...")

    parser = argparse.ArgumentParser(prog="guardrails", description="Manage guardrails rules, modes and presets.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    status = verbs.add_parser("status", parents=[common], help="show effective rules and modes")
    status.add_argument("--scope", choices=SCOPES, help="list only rules and modes with an entry in this layer")
    status.add_argument("--problems", action="store_true", help="print only the problems")
    status.add_argument("--rule", metavar="ID", help="print only this rule's row")

    rule = verbs.add_parser("rule", help="add, change or remove rules").add_subparsers(dest="op", required=True)
    add = rule.add_parser("add", parents=[common, scoped], help="add or replace a rule")
    add.add_argument("id")
    add.add_argument("--json", required=True, help="the rule as a JSON object, @<file> or - for stdin")
    set_ = rule.add_parser("set", parents=[common, scoped], help="change fields of a rule")
    set_.add_argument("id")
    set_.add_argument("--json", required=True, help="fields to change as a JSON object (null removes one), @<file> or - for stdin")
    rm = rule.add_parser("rm", parents=[common, scoped], help="remove a rule")
    rm.add_argument("id")
    test = rule.add_parser("test", parents=[common], help="dry-run a rule against sample commands")
    source = test.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", help="a draft rule as a JSON object, @<file> or - for stdin")
    source.add_argument("--id", help="an installed rule's id")
    test.add_argument("commands", nargs="*", metavar="CMD")
    test.add_argument("--examples", help='JSON list of {"cmd", "source", "expect"} objects, @<file> or - for stdin')
    test.add_argument("--source", choices=[s.value for s in render.Source], default="inferred",
                      help="source tag of the positional commands (default: inferred)")
    test.add_argument("--intent", help="the rule's intent line")
    test.add_argument("--id-name", help="with --json: the id shown in the title")
    test.add_argument("--scope", choices=SCOPES, help="with --json: the scope shown in the title (default: global)")
    ast = rule.add_parser("ast", parents=[common], help="print the parse tree of a command, with the units "
                          "its wrappers and shell strings expose")
    ast.add_argument("command", metavar="CMD")

    mode = verbs.add_parser("mode", help="declare modes and switch them on or off").add_subparsers(dest="op",
                                                                                                required=True)
    declare = mode.add_parser("declare", parents=[common, scoped], help="declare (or redeclare) a mode")
    declare.add_argument("name")
    declare.add_argument("--description", default="")
    declare.add_argument("--agent-may-enable", action="store_true")
    undeclare = mode.add_parser("undeclare", parents=[common, scoped], help="remove a mode declaration")
    undeclare.add_argument("name")
    for op in ("on", "off"):
        toggle = mode.add_parser(op, parents=[common], help=f"switch a mode {op}")
        toggle.add_argument("name")
        toggle.add_argument("--scope", choices=("session", *SCOPES), default="session")

    preset = verbs.add_parser("preset", help="bundled rule sets").add_subparsers(dest="op", required=True)
    preset.add_parser("list", parents=[common], help="list presets")
    show = preset.add_parser("show", parents=[common], help="print a preset")
    show.add_argument("name")
    install = preset.add_parser("install", parents=[common, scoped], help="copy a preset's rules and modes into state")
    install.add_argument("name")
    install.add_argument("--only", help="comma-separated rule ids to install")

    engine = verbs.add_parser("engine", help="the managed runtime (uv, Python, ast-grep-py) behind rules "
                              "and match.ast; installed automatically, so this is for diagnosis (not a "
                              "configuration change, so --as-user does not apply)").add_subparsers(dest="op",
                                                                                                     required=True)
    engine.add_parser("status", help="the runtime's pins, platform, paths, state and last install failure")
    ensure = engine.add_parser("ensure", help="install the runtime now if it is missing (normally automatic)")
    ensure.add_argument("--retry-now", action="store_true", help="ignore the wait after a failed install")

    for name, state in (("enable", "on"), ("disable", "off")):
        toggle = verbs.add_parser(name, parents=[common], help=f"turn the hook (with --scope project: project rules) {state}")
        toggle.add_argument("--scope", choices=("global", "project"), default="global")
    return parser


HANDLERS: dict[tuple[str, str | None], Callable[[Args], int]] = {
    ("status", None): cmd_status,
    ("rule", "add"): cmd_rule_add,
    ("rule", "set"): cmd_rule_set,
    ("rule", "rm"): cmd_rule_rm,
    ("rule", "test"): cmd_rule_test,
    ("rule", "ast"): cmd_rule_ast,
    ("mode", "declare"): cmd_mode_declare,
    ("mode", "undeclare"): cmd_mode_undeclare,
    ("mode", "on"): cmd_mode_on,
    ("mode", "off"): cmd_mode_off,
    ("preset", "list"): cmd_preset_list,
    ("preset", "show"): cmd_preset_show,
    ("preset", "install"): cmd_preset_install,
    ("engine", "ensure"): cmd_engine_ensure,
    ("engine", "status"): cmd_engine_status,
    ("enable", None): lambda args: set_enabled(args, True),
    ("disable", None): lambda args: set_enabled(args, False),
}


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[(args.verb, getattr(args, "op", None))](args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except PermissionError as exc:
        hint = f" Re-run with sudo: sudo {store.CLI} {shlex.join(argv)}" if getattr(args, "scope", None) == "managed" else ""
        print(f"error: {exc}.{hint}", file=sys.stderr)
        return 2
    except (Invalid, store.StateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
