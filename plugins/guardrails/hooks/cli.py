"""Command-line interface for guardrails rules, modes and presets."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Callable

import policy
import store
from policy import Invalid, view

PRESETS_DIR = os.path.join(os.path.dirname(store.HERE), "presets")
SETTABLE = ("action", "retry", "enabled", "modes", "message", "messageShort", "description",
            "program", "args", "builtin", "regex", "requires")
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
    """mapping[key] as a dict, created (or replaced if malformed) in place."""
    value = mapping.get(key)
    if not isinstance(value, dict):
        value = mapping[key] = {}
    return value


def project_path() -> str:
    path = store.project_state_path()
    if not path:
        raise Invalid("not inside a project (no CLAUDE_PROJECT_DIR and no git repository)")
    return path


def target_path(args: Args) -> str:
    return project_path() if args.project else store.global_state_path()


def session_id(args: Args) -> str:
    sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise Invalid("no session id: pass --session-id or run inside Claude Code (CLAUDE_CODE_SESSION_ID)")
    return sid


def describe_match(rule: policy.Rule) -> str:
    match = view(rule, "match")
    bits = []
    if policy.programs_of(rule):
        bits.append("program=" + ",".join(policy.programs_of(rule)))
    for key in ("args", "builtin", "regex"):
        if match.get(key):
            bits.append(f"{key}={match[key]}")
    if isinstance(rule.get("requires"), list):
        bits.append("requires=" + "|".join(str(r) for r in rule["requires"]))
    return " ".join(bits) or "(no matcher)"


def cmd_status(args: Args) -> int:
    problems: list[str] = []

    def safe_load(path: str | None) -> store.State:
        try:
            return store.load(path)
        except store.StateError as exc:
            problems.append(f"unreadable state file (the hook fails open until it is fixed): {exc}")
            return {}

    gpath, ppath = store.global_state_path(), store.project_state_path()
    gstate, pstate = safe_load(gpath), safe_load(ppath)
    disabled = f" ({gstate['disabledReason']})" if gstate.get("disabledReason") else ""
    print(f"global state:  {gpath}")
    print(f"project state: {ppath or '(not in a project)'}")
    print(f"hook enabled:  {'yes' if gstate.get('enabled', True) is not False else 'no'}{disabled}")
    if ppath:
        print(f"project rules enabled: {'yes' if pstate.get('enabled', True) is not False else 'no'}")

    rules, modes = policy.effective_rules(gstate, pstate), policy.effective_modes(gstate, pstate)
    sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    session = view(view(gstate, "sessions"), sid) if sid else {}
    active = policy.active_modes(modes, session)
    grules, prules = view(gstate, "rules"), view(pstate, "rules")

    print("\nrules:")
    if not rules:
        print("  (none; the guardrails:setup skill installs recommended presets)")
    for rid in sorted(rules):
        rule = rules[rid]
        source = "global+project" if rid in grules and rid in prules else ("project" if rid in prules else "global")
        flags = [str(rule.get("action"))]
        if rule.get("retry") == "same-command":
            flags.append("retry")
        if rule.get("enabled") is not True:
            flags.append("DISABLED")
        suspended = [m for m in policy.modes_of(rule) if m in active]
        if suspended:
            flags.append("SUSPENDED by " + ",".join(suspended))
        print(f"  {rid} [{source}] {' '.join(flags)}: {describe_match(rule)}")
        if policy.modes_of(rule):
            print(f"      suspended by modes: {', '.join(policy.modes_of(rule))}")
        try:
            policy.validate_rule(rule)
        except Invalid as exc:
            problems.append(f"rule {rid}: {exc} (ignored by the hook)")
        for m in policy.modes_of(rule):
            if m not in modes:
                problems.append(f"rule {rid}: mode '{m}' is not declared (the rule stays enforced)")

    print("\nmodes:")
    if not modes:
        print("  (none declared)")
    for name in sorted(modes):
        mode = modes[name]
        if name in active:
            record = active[name]
            why = f": {record['reason']}" if record.get("reason") else ""
            state = f"ACTIVE (by {record.get('by', 'user')}{why})"
        else:
            state = "inactive"
        print(f"  {name}: {state}; agent may enable: {'yes' if mode['agentMayEnable'] else 'no'}; "
              f"{mode['description']}")

    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {problem}")
    return 0


def cmd_rule_add(args: Args) -> int:
    require_user(args, "rule add")
    check_name("rule", args.id)
    try:
        rule = json.loads(args.json)
    except ValueError as exc:
        raise Invalid(f"--json is not valid JSON: {exc}") from exc
    policy.validate_rule(rule)
    rule["setBy"] = stamp(args.reason)
    path = target_path(args)

    def change(state: store.State) -> bool:
        rules = table(state, "rules")
        existed = args.id in rules
        rules[args.id] = rule
        return existed

    existed = store.mutate(path, change)
    print(f"{'replaced' if existed else 'added'} rule {args.id} in {path}")
    return 0


def apply_assignment(rule: dict[str, Any], key: str, value: str) -> None:
    if key == "enabled":
        if value not in ("true", "false"):
            raise Invalid("enabled must be true or false")
        rule["enabled"] = value == "true"
    elif key == "modes":
        rule["modes"] = [x.strip() for x in value.split(",") if x.strip()]
    elif key == "requires":
        items = [x.strip() for x in value.split(",") if x.strip()]
        if items:
            rule["requires"] = items
        else:
            rule.pop("requires", None)
    elif key in policy.MATCH_KEYS:
        match = dict(view(rule, "match"))
        if not value:
            match.pop(key, None)
        elif key == "program":
            names = [x.strip() for x in value.split(",") if x.strip()]
            match["program"] = names[0] if len(names) == 1 else names
        else:
            match[key] = value
        rule["match"] = match
    elif value:
        rule[key] = value
    else:
        rule.pop(key, None)


def ineffective(pairs: list[tuple[str, str]], base: policy.Rule) -> list[str]:
    notes = []
    for key, value in pairs:
        if (key, value) in (("action", "warn"), ("retry", "same-command"), ("enabled", "false")):
            notes.append(f"{key}={value}")
        elif key == "modes":
            added = [m.strip() for m in value.split(",") if m.strip() and m.strip() not in policy.modes_of(base)]
            if added:
                notes.append(f"modes {','.join(added)} (a project can only remove suspending modes)")
        elif key in policy.MATCH_KEYS or key == "requires":
            notes.append(f"{key}={value} (a project cannot change what a global rule matches)")
    return notes


def cmd_rule_set(args: Args) -> int:
    require_user(args, "rule set")
    path = target_path(args)
    pairs: list[tuple[str, str]] = []
    for item in args.assignments:
        key, sep, value = item.partition("=")
        if not sep or key not in SETTABLE:
            raise Invalid(f"expected key=value with key one of {', '.join(SETTABLE)}; got {item!r}")
        pairs.append((key, value))
    global_rules = view(store.load(store.global_state_path()), "rules") if args.project else {}
    base = global_rules.get(args.id) if isinstance(global_rules.get(args.id), dict) else None

    def change(state: store.State) -> None:
        rules = table(state, "rules")
        if not isinstance(rules.get(args.id), dict):
            if base is None:
                raise Invalid(f"no rule '{args.id}' in {path}")
            rules[args.id] = {}
        rule = rules[args.id]
        for key, value in pairs:
            apply_assignment(rule, key, value)
        policy.validate_rule(policy.merge_rule(policy.with_defaults(base), rule) if base is not None else rule)
        rule["setBy"] = stamp(args.reason)

    store.mutate(path, change)
    print(f"updated rule {args.id} in {path}")
    if base is not None:
        notes = ineffective(pairs, base)
        if notes:
            print("note: a project entry can only tighten a global rule; no effect: " + ", ".join(notes))
    return 0


def cmd_rule_rm(args: Args) -> int:
    require_user(args, "rule rm")
    path = target_path(args)

    def change(state: store.State) -> None:
        rules = table(state, "rules")
        if args.id not in rules:
            raise Invalid(f"no rule '{args.id}' in {path}")
        del rules[args.id]

    store.mutate(path, change)
    print(f"removed rule {args.id} from {path}")
    return 0


def cmd_mode_declare(args: Args) -> int:
    require_user(args, "mode declare")
    check_name("mode", args.name)
    path = target_path(args)

    def change(state: store.State) -> None:
        modes = table(state, "modes")
        previous = view(modes, args.name)
        modes[args.name] = {"description": args.description or str(previous.get("description", "")),
                            "agentMayEnable": bool(args.agent_may_enable),
                            "active": previous.get("active") is True,
                            "setBy": stamp(args.reason)}

    store.mutate(path, change)
    print(f"declared mode {args.name} in {path} (agent may enable: {'yes' if args.agent_may_enable else 'no'})")
    return 0


def cmd_mode_undeclare(args: Args) -> int:
    require_user(args, "mode undeclare")
    path = target_path(args)

    def change(state: store.State) -> None:
        modes = table(state, "modes")
        if args.name not in modes:
            raise Invalid(f"mode '{args.name}' is not declared in {path}")
        del modes[args.name]

    store.mutate(path, change)
    print(f"removed mode {args.name} from {path}")
    return 0


def set_persistent(args: Args, active: bool) -> int:
    require_user(args, f"mode {'on' if active else 'off'} --scope {args.scope}")
    path = store.global_state_path() if args.scope == "global" else project_path()
    declared_elsewhere = args.scope == "project" and args.name in policy.effective_modes(
        store.load(store.global_state_path()), store.load(path))

    def change(state: store.State) -> None:
        modes = table(state, "modes")
        mode = modes.get(args.name)
        if not isinstance(mode, dict):
            if not declared_elsewhere:
                raise Invalid(f"mode '{args.name}' is not declared in {path}; declare it there first")
            mode = modes[args.name] = {}
        mode["active"] = active
        mode["setBy"] = stamp(args.reason)

    store.mutate(path, change)
    print(f"mode {args.name} {'on' if active else 'off'} for every session ({args.scope} scope)")
    return 0


def cmd_mode_on(args: Args) -> int:
    if args.scope != "session":
        return set_persistent(args, True)
    sid = session_id(args)
    modes = policy.effective_modes(store.load(store.global_state_path()), store.load(store.project_state_path()))
    if args.name not in modes:
        raise Invalid(f"mode '{args.name}' is not declared (declared: {', '.join(sorted(modes)) or 'none'})")
    if is_agent():
        if not modes[args.name]["agentMayEnable"]:
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
    path = target_path(args)

    def change(state: store.State) -> None:
        state["enabled"] = enabled
        state["setBy"] = stamp(args.reason)
        if enabled:
            state.pop("disabledReason", None)
        else:
            state["disabledReason"] = args.reason or "disabled by user"

    store.mutate(path, change)
    print(f"{'project rules' if args.project else 'guardrails hook'} {'enabled' if enabled else 'disabled'} ({path})")
    return 0


def preset_names() -> list[str]:
    if not os.path.isdir(PRESETS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PRESETS_DIR) if f.endswith(".json"))


def load_preset(name: str) -> dict[str, Any]:
    if name not in preset_names():
        raise Invalid(f"no preset '{name}' (available: {', '.join(preset_names()) or 'none'})")
    with open(os.path.join(PRESETS_DIR, f"{name}.json")) as fh:
        data = json.load(fh)
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
    wanted = {m for r in chosen.values() for m in policy.modes_of(r)}
    modes = {name: m for name, m in view(preset, "modes").items() if name in wanted}
    path = target_path(args)
    by = stamp(args.reason or f"preset {args.name}")
    report: list[str] = []

    def change(state: store.State) -> None:
        srules = table(state, "rules")
        for rid, rule in sorted(chosen.items()):
            current = srules.get(rid)
            if isinstance(current, dict) and {k: v for k, v in current.items() if k != "setBy"} == rule:
                report.append(f"rule {rid}: unchanged")
                continue
            report.append(f"rule {rid}: {'replaced' if rid in srules else 'added'}")
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

    store.mutate(path, change)
    print(f"installed preset {args.name} into {path}")
    for line in report:
        print(f"  {line}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", action="store_true", help="target this project's state instead of the global one")
    common.add_argument("--as-user", action="store_true", help="agents only: the user explicitly asked for this change")
    common.add_argument("--session-id", help="session to act on (default: $CLAUDE_CODE_SESSION_ID)")
    common.add_argument("--reason", help="recorded with the change")

    parser = argparse.ArgumentParser(prog="guard.py", description="Manage guardrails rules, modes and presets.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("status", parents=[common], help="show effective rules and modes")

    rule = verbs.add_parser("rule", help="add, change or remove rules").add_subparsers(dest="op", required=True)
    add = rule.add_parser("add", parents=[common], help="add or replace a rule")
    add.add_argument("id")
    add.add_argument("--json", required=True, help="the rule as a JSON object")
    set_ = rule.add_parser("set", parents=[common], help="change fields of a rule")
    set_.add_argument("id")
    set_.add_argument("assignments", nargs="+", metavar="key=value")
    rm = rule.add_parser("rm", parents=[common], help="remove a rule")
    rm.add_argument("id")

    mode = verbs.add_parser("mode", help="declare modes and switch them on or off").add_subparsers(dest="op",
                                                                                                required=True)
    declare = mode.add_parser("declare", parents=[common], help="declare (or redeclare) a mode")
    declare.add_argument("name")
    declare.add_argument("--description", default="")
    declare.add_argument("--agent-may-enable", action="store_true")
    undeclare = mode.add_parser("undeclare", parents=[common], help="remove a mode declaration")
    undeclare.add_argument("name")
    for op in ("on", "off"):
        toggle = mode.add_parser(op, parents=[common], help=f"switch a mode {op}")
        toggle.add_argument("name")
        toggle.add_argument("--scope", choices=("session", "project", "global"), default="session")

    preset = verbs.add_parser("preset", help="bundled rule sets").add_subparsers(dest="op", required=True)
    preset.add_parser("list", parents=[common], help="list presets")
    show = preset.add_parser("show", parents=[common], help="print a preset")
    show.add_argument("name")
    install = preset.add_parser("install", parents=[common], help="copy a preset's rules and modes into state")
    install.add_argument("name")
    install.add_argument("--only", help="comma-separated rule ids to install")

    verbs.add_parser("enable", parents=[common], help="turn the hook (with --project: project rules) on")
    verbs.add_parser("disable", parents=[common], help="turn the hook (with --project: project rules) off")
    return parser


HANDLERS: dict[tuple[str, str | None], Callable[[Args], int]] = {
    ("status", None): cmd_status,
    ("rule", "add"): cmd_rule_add,
    ("rule", "set"): cmd_rule_set,
    ("rule", "rm"): cmd_rule_rm,
    ("mode", "declare"): cmd_mode_declare,
    ("mode", "undeclare"): cmd_mode_undeclare,
    ("mode", "on"): cmd_mode_on,
    ("mode", "off"): cmd_mode_off,
    ("preset", "list"): cmd_preset_list,
    ("preset", "show"): cmd_preset_show,
    ("preset", "install"): cmd_preset_install,
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
    except (Invalid, store.StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
