"""Command-line interface for guardrails rules, modes and presets."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

import astbin
import astrun
import matching
import policy
import render
import store
import wrappers as wrapper_table
from policy import Invalid, view

PRESETS_DIR = os.path.join(os.path.dirname(store.HERE), "presets")
SETTABLE = ("action", "retry", "enabled", "modes", "message", "messageShort", "description",
            "program", "args", "builtin", "regex", "ast", "requires")
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


def resolve_scope(args: Args) -> str:
    scope = getattr(args, "scope", None) or "global"
    check_path_scope(args, scope)
    return scope


def extra_path(args: Args) -> str | None:
    path = getattr(args, "path", None)
    return os.path.abspath(os.path.expanduser(path)) if path else None


def scope_path(scope: str, args: Args | None = None) -> str:
    if scope == "managed":
        return store.managed_write_path(extra_path(args) if args else None)
    return project_path() if scope == "project" else store.global_state_path()


def target_path(args: Args) -> str:
    return scope_path(resolve_scope(args), args)


def check_path_scope(args: Args, scope: str) -> None:
    if extra_path(args) and scope != "managed":
        raise Invalid("--path names a managed-format file and needs --scope managed")


def change_state(scope: str, path: str, fn: Callable[[store.State], Any]) -> Any:
    if scope != "managed":
        return store.mutate(path, fn)
    store.ensure_writable(path)
    try:
        return store.mutate(path, fn, store.MANAGED_MODE)
    except store.StateError as exc:
        raise store.StateError(f"{exc}; fix or remove the managed file by hand, the CLI never overwrites a corrupt "
                               "state file") from exc


def managed_state(args: Args) -> store.State:
    return store.load_managed(extra_path(args))[0]


def managed_rule_ids(args: Args) -> set[str]:
    return set(policy.origins("rules", managed_state(args), {}, {}))


def refuse_managed_rule(args: Args, scope: str, rid: str, path: str) -> None:
    if scope == "managed" or rid not in managed_rule_ids(args):
        return
    try:
        own = rid in view(store.load(path), "rules")
    except store.StateError:
        own = False
    if not own:
        raise Refused(f"rule '{rid}' is a managed rule. Change it with --scope managed (needs sudo and the "
                      "user's explicit request); other scopes cannot alter it.")


def always_enforced(scope: str, rid: str, rule: policy.Rule) -> str:
    if scope == "managed" and not policy.modes_of(rule):
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
    path = os.path.expanduser(value[1:])
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise Invalid(f"{flag}: cannot read {path}: {exc.strerror or exc}") from exc


def load_json(value: str, flag: str) -> Any:
    try:
        return json.loads(read_arg(value, flag))
    except ValueError as exc:
        where = f" in {os.path.expanduser(value[1:])}" if value.startswith("@") else ""
        raise Invalid(f"{flag} is not valid JSON{where}: {exc}") from exc


def check_stdin(*pairs: tuple[str | None, str]) -> None:
    if sum(value == "-" for value, _ in pairs) > 1:
        raise Invalid(f"only one of {' and '.join(flag for _, flag in pairs)} can read stdin")


def describe_match(rule: policy.Rule) -> str:
    match = view(rule, "match")
    bits = []
    if policy.programs_of(rule):
        bits.append("program=" + ",".join(policy.programs_of(rule)))
    for key in ("args", "builtin", "regex"):
        if match.get(key):
            bits.append(f"{key}={match[key]}")
    if match.get("ast"):
        bits.append("ast=" + render.compact(match["ast"]))
    if isinstance(rule.get("requires"), list):
        bits.append("requires=" + "|".join(str(r) for r in rule["requires"]))
    return " ".join(bits) or "(no matcher)"


@dataclass
class Snapshot:
    extra: str | None
    sources: list[str]
    mstate: store.State
    gstate: store.State
    pstate: store.State
    gpath: str
    ppath: str | None
    hook_on: bool
    rules: dict[str, policy.Rule]
    modes: dict[str, policy.Mode]
    active: dict[str, dict[str, Any]]
    rule_origins: dict[str, list[str]]
    mode_origins: dict[str, list[str]]
    problems: list[str]
    problem_layers: list[frozenset[str]]
    blind: frozenset[str] = frozenset()

    def problems_in(self, scope: str | None) -> list[str]:
        return [p for p, layers in zip(self.problems, self.problem_layers) if scope is None or scope in layers]


def snapshot(args: Args) -> Snapshot:
    extra = extra_path(args)
    mstate, problems = store.load_managed(extra)
    layers: list[frozenset[str]] = [frozenset({"managed"})] * len(problems)

    def report(text: str, *where: str) -> None:
        problems.append(text)
        layers.append(frozenset(where))

    def safe_load(path: str | None, what: str) -> store.State:
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
    sources = store.managed_paths(extra)
    for path in sources:
        for text in store.trust_problems(path):
            report(text, "managed")
    for name, m in view(pstate, "modes").items():
        if name in view(mstate, "modes") and isinstance(m, dict) and m.get("active") is True:
            report(f"project state switches on mode '{name}', which the managed file declares (ignored)", "project")
    rules, modes = policy.effective_rules(mstate, gstate, pstate), policy.effective_modes(mstate, gstate, pstate)
    sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    session = view(view(gstate, "sessions"), sid) if sid else {}
    rule_origins = policy.origins("rules", mstate, gstate, pstate)
    mode_origins = policy.origins("modes", mstate, gstate, pstate)
    for rid in sorted(rules):
        try:
            policy.validate_rule(rules[rid])
        except Invalid as exc:
            if "managed" not in rule_origins.get(rid, []):
                report(f"rule {rid}: {exc} (ignored by the hook)", *rule_origins.get(rid, []))
        for m in policy.modes_of(rules[rid]):
            if m not in modes:
                report(f"rule {rid}: mode '{m}' is not declared (the rule stays enforced)",
                       *rule_origins.get(rid, []))
    for text in policy.wrapper_problems(mstate, gstate, pstate):
        report(text, "managed", "global", "project")
    parsed = {rid: rule for rid, rule in rules.items() if rule.get("enabled") is True and policy.needs_parse(rule)
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
            for rid, why in matching.check(parsed, policy.effective_wrappers(mstate, gstate, pstate), state_dir()).items():
                report(f"rule {rid}: does not compile ({why}) (skipped by the hook)", *rule_origins.get(rid, []))
        except astrun.Unavailable as exc:
            blind = set(parsed)
            managed_blind = sorted(rid for rid in parsed if "managed" in rule_origins.get(rid, []))
            fails_open = f" MANAGED rules fail open too: {', '.join(managed_blind)}." if managed_blind else ""
            report(f"rules {', '.join(sorted(parsed))} use program, args, builtin or match.ast and are NOT enforced "
                   f"while the engine cannot run ({exc}); regex rules still are.{fails_open}{engine_fix(exc)}", *where)
    return Snapshot(extra, sources, mstate, gstate, pstate, gpath, ppath, gstate.get("enabled", True) is not False,
                    rules, modes, policy.active_modes(modes, session), rule_origins, mode_origins, problems, layers,
                    frozenset(blind))


def rule_state(rule: policy.Rule, layers: list[str], active: dict[str, dict[str, Any]], blind: bool = False) -> str:
    if rule.get("enabled") is not True:
        return "disabled"
    suspended = [m for m in policy.modes_of(rule) if m in active]
    if suspended:
        return "suspended by " + ", ".join(suspended)
    if blind:
        return "NOT enforced: engine unavailable"
    if "managed" in layers and not policy.modes_of(rule):
        return "always enforced"
    return "enabled"


def presence_word(path: str) -> str:
    return store.presence(path).strip(" ()") or "present"


def managed_line(snap: Snapshot) -> str:
    default, *override = snap.sources
    env = os.environ.get(store.MANAGED_ENV)
    parts = [f"platform file {render.span(default)} {presence_word(default)}"]
    for path in override:
        label = "--path" if snap.extra and path == snap.extra and not (env and os.path.abspath(env) == path) \
            else "override"
        parts.append(f"{label} {render.span(path)} {presence_word(path)}")
    in_use = [path for path in override if not store.presence(path)]
    if store.presence(default) == " (absent)":
        if in_use:
            parts.append("managed rules come only from " + ", ".join(render.span(p) for p in in_use))
        else:
            parts.append("no managed file is present, so there are no managed rules")
    if snap.extra and not store.hook_enforces(snap.extra):
        parts.append(f"the --path file is read for this status only; the hook enforces it only if "
                     f"{store.MANAGED_ENV} points at it")
    return "**Managed** " + " · ".join(parts)


def status_view(snap: Snapshot, scope: str | None) -> render.Status:
    def keep(origins: dict[str, list[str]], name: str) -> bool:
        return scope is None or scope in origins.get(name, [])

    rules = [render.RuleRow(rid, str(snap.rules[rid].get("action")), snap.rule_origins.get(rid, []),
                            rule_state(snap.rules[rid], snap.rule_origins.get(rid, []), snap.active, rid in snap.blind))
             for rid in sorted(snap.rules) if keep(snap.rule_origins, rid)]
    modes = []
    for name in sorted(snap.modes):
        if not keep(snap.mode_origins, name):
            continue
        if name in snap.active:
            record = snap.active[name]
            why = f": {record['reason']}" if record.get("reason") else ""
            on = "on (persistent)" if snap.modes[name]["active"] else f"on (by {record.get('by', 'user')}{why})"
        else:
            on = "off"
        modes.append(render.ModeRow(name, on, snap.modes[name]["agentMayEnable"], snap.mode_origins.get(name, [])))
    notes = []
    if not snap.hook_on:
        reason = f" ({snap.gstate['disabledReason']})" if snap.gstate.get("disabledReason") else ""
        kept = "; managed rules stay enforced" if view(snap.mstate, "rules") else ""
        notes.append(f"the global hook is disabled{reason}{kept}")
    status = render.Status(managed_line(snap), snap.hook_on, snap.ppath is not None
                           and snap.pstate.get("enabled", True) is False, rules, modes, snap.problems_in(scope),
                           notes=notes)
    if scope:
        status.no_rules = f"No rules with a {scope} entry."
    return status


def print_status_render(args: Args, snap: Snapshot) -> None:
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


def print_status_plain(args: Args, snap: Snapshot) -> None:
    if args.problems:
        problems = snap.problems_in(args.scope)
        print("problems:" if problems else "no problems")
        for problem in problems:
            print(f"  - {render.clean(problem)}")
        return
    extra, mstate, gstate, pstate, rule_origins = snap.extra, snap.mstate, snap.gstate, snap.pstate, snap.rule_origins
    disabled = f" ({gstate['disabledReason']})" if gstate.get("disabledReason") else ""
    default, *override = snap.sources
    print(f"managed state: {default}{store.presence(default)}")
    env = os.environ.get(store.MANAGED_ENV)
    for path in override:
        if extra and path == extra and not (env and os.path.abspath(env) == path):
            print(f"managed --path: {path}{store.presence(path)}")
        else:
            print(f"managed override: {path}{store.presence(path)}")
    in_use = [path for path in override if not store.presence(path)]
    if in_use and store.presence(default) == " (absent)":
        print(f"note: the platform default managed file is absent; managed rules come only from {', '.join(in_use)}")
    if extra and not store.hook_enforces(extra):
        print(f"note: --path {extra} is read for this status only; the hook enforces it only if "
              f"{store.MANAGED_ENV} points at it")
    print(f"global state:  {snap.gpath}")
    print(f"project state: {snap.ppath or '(not in a project)'}")
    kept = "" if snap.hook_on or not view(mstate, "rules") else "; managed rules stay enforced"
    print(f"hook enabled:  {'yes' if snap.hook_on else 'no'}{disabled}{kept}")
    if snap.ppath:
        print(f"project rules enabled: {'yes' if pstate.get('enabled', True) is not False else 'no'}")

    scope = args.scope
    print("\nrules:")
    shown = [rid for rid in sorted(snap.rules) if scope is None or scope in rule_origins.get(rid, [])]
    if not shown:
        print("  (none; the guardrails:setup skill installs recommended presets)")
    for rid in shown:
        rule = snap.rules[rid]
        source = "+".join(rule_origins.get(rid, []))
        flags = [str(rule.get("action"))]
        if rule.get("retry") == "same-command":
            flags.append("retry")
        if rule.get("enabled") is not True:
            flags.append("DISABLED")
        suspended = [m for m in policy.modes_of(rule) if m in snap.active]
        if suspended:
            flags.append("SUSPENDED by " + ",".join(suspended))
        elif rid in snap.blind and rule.get("enabled") is True:
            flags.append("NOT ENFORCED (engine unavailable)")
        if "managed" in rule_origins.get(rid, []) and not policy.modes_of(rule):
            flags.append("ALWAYS ENFORCED")
        print(render.clean(f"  {rid} [{source}] {' '.join(flags)}: {describe_match(rule)}"))
        if policy.modes_of(rule):
            print(render.clean(f"      suspended by modes: {', '.join(policy.modes_of(rule))}"))

    print("\nmodes:")
    names = [n for n in sorted(snap.modes) if scope is None or scope in snap.mode_origins.get(n, [])]
    if not names:
        print("  (none declared)")
    for name in names:
        mode = snap.modes[name]
        if name in snap.active:
            record = snap.active[name]
            why = f": {record['reason']}" if record.get("reason") else ""
            state = f"ACTIVE (by {record.get('by', 'user')}{why})"
        else:
            state = "inactive"
        print(render.clean(f"  {name} [{'+'.join(snap.mode_origins.get(name, []))}]: {state}; "
                           f"agent may enable: {'yes' if mode['agentMayEnable'] else 'no'}; {mode['description']}"))

    problems = snap.problems_in(scope)
    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {render.clean(problem)}")


def cmd_status(args: Args) -> int:
    if args.rule and not args.render:
        raise Invalid("--rule needs --render")
    snap = snapshot(args)
    if args.render:
        print_status_render(args, snap)
    else:
        print_status_plain(args, snap)
    return 0


def engine_fix(exc: Exception) -> str:
    """How to get the engine, when that is what is missing."""
    if not isinstance(exc, astrun.Missing):
        return ""
    if exc.unsupported:
        return ". ast-grep cannot run on this platform, so there is nothing to install; use regex rules here"
    fixes = f"`{astbin.FIX_INSTALL}` or `{astbin.npm_fix()}`" if exc.wheel else f"`{astbin.npm_fix()}`"
    return f". Fix: run {fixes}; `guardrails engine status` shows the state"


def check_ast_rule(rule: policy.Rule, wrappers: wrapper_table.Names) -> str:
    """Raise Invalid when the rule does not compile; a note when it could not be checked."""
    if not policy.needs_parse(rule):
        return ""
    try:
        errors = matching.check({"rule": rule}, wrappers, state_dir())
    except astrun.Unavailable as exc:
        return (f"note: the rule was not compile-checked ({exc}); the hook skips a rule that does not compile and "
                f"warns once per session{engine_fix(exc)}")
    if errors:
        raise Invalid(f"the rule does not compile: {errors['rule']}")
    return ""


def cmd_rule_add(args: Args) -> int:
    require_user(args, "rule add")
    check_name("rule", args.id)
    rule, _ = split_envelope(load_json(args.json, "--json"))
    policy.validate_rule(rule)
    unchecked = check_ast_rule(rule, current_wrappers(args))
    rule["setBy"] = stamp(args.reason)
    scope = resolve_scope(args)
    path = scope_path(scope, args)

    def change(state: store.State) -> bool:
        rules = table(state, "rules")
        existed = args.id in rules
        rules[args.id] = rule
        return existed

    existed = change_state(scope, path, change)
    print(f"{'replaced' if existed else 'added'} rule {args.id} in {path}")
    if unchecked:
        print(unchecked)
    if scope != "managed" and args.id in managed_rule_ids(args):
        print(f"note: {args.id} is also a managed rule; this entry can only tighten it, not reword it")
    note = always_enforced(scope, args.id, rule)
    if note:
        print(note)
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
        elif key == "ast":
            match[key] = load_json(value, "ast")
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


def json_assignments(fields: object) -> list[tuple[str, str]]:
    if not isinstance(fields, dict):
        raise Invalid("--json must be a JSON object of the fields to change")
    pairs = []
    for key, value in fields.items():
        if key not in SETTABLE:
            raise Invalid(f"--json key {key!r} must be one of {', '.join(SETTABLE)}")
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, list) and all(isinstance(x, str) for x in value):
            text = ",".join(value)
        elif isinstance(value, str):
            text = value
        elif isinstance(value, dict) and key == "ast":
            text = json.dumps(value)
        else:
            raise Invalid(f"--json key {key!r} must be a string, a list of strings or (for enabled) a boolean")
        pairs.append((key, text))
    return pairs


def cmd_rule_set(args: Args) -> int:
    require_user(args, "rule set")
    scope = resolve_scope(args)
    path = scope_path(scope, args)
    pairs: list[tuple[str, str]] = []
    if args.json is not None:
        pairs = json_assignments(load_json(args.json, "--json"))
    if not args.assignments and args.json is None:
        raise Invalid("give key=value assignments or --json")
    if args.assignments and args.json is not None:
        raise Invalid("give key=value assignments or --json, not both")
    for item in args.assignments:
        key, sep, value = item.partition("=")
        if not sep or key not in SETTABLE:
            raise Invalid(f"expected key=value with key one of {', '.join(SETTABLE)}; got {item!r}")
        pairs.append((key, value))
    refuse_managed_rule(args, scope, args.id, path)
    base = policy.effective_rules({}, store.load(store.global_state_path()), {}).get(args.id) \
        if scope == "project" else None

    unchecked: list[str] = []

    def change(state: store.State) -> policy.Rule:
        rules = table(state, "rules")
        if not isinstance(rules.get(args.id), dict):
            if base is None:
                raise Invalid(f"no rule '{args.id}' in {path}")
            rules[args.id] = {}
        rule = rules[args.id]
        for key, value in pairs:
            apply_assignment(rule, key, value)
        policy.validate_rule(policy.merge_rule(base, rule) if base is not None else rule)
        unchecked.append(check_ast_rule(rule, current_wrappers(args)))
        rule["setBy"] = stamp(args.reason)
        return rule

    rule = change_state(scope, path, change)
    print(f"updated rule {args.id} in {path}")
    if unchecked and unchecked[-1]:
        print(unchecked[-1])
    if base is not None:
        notes = ineffective(pairs, base)
        if notes:
            print("note: a project entry can only tighten a global rule; no effect: " + ", ".join(notes))
    note = always_enforced(scope, args.id, rule)
    if note:
        print(note)
    return 0


def cmd_rule_rm(args: Args) -> int:
    require_user(args, "rule rm")
    scope = resolve_scope(args)
    path = scope_path(scope, args)
    refuse_managed_rule(args, scope, args.id, path)

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
    if rule.get("enabled") is False:
        notes.append("rule is disabled")
    if rule.get("requires") and not policy.requirements_met(rule):
        notes.append(f"none of {'|'.join(rule['requires'])} is installed here, so the hook skips this rule")
    if "managed" not in layers and gstate.get("enabled", True) is False:
        notes.append("the global hook is disabled, so the hook does not enforce this rule")
    listed = policy.modes_of(rule)
    if listed:
        sid = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
        session = view(view(gstate, "sessions"), sid) if sid else {}
        active = policy.active_modes(policy.effective_modes(mstate, gstate, pstate), session)
        on = [m for m in listed if m in active]
        if on:
            notes.append(f"mode {', '.join(on)} is active, so the hook suspends this rule right now")
        else:
            notes.append(f"the hook suspends this rule while mode {' or '.join(listed)} is active (none is now)")
    return notes


Example = tuple[str, str, Optional[str]]


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
        if source not in render.SOURCES:
            raise Invalid(f"--examples[{i}] source must be one of {', '.join(render.SOURCES)}")
        if expect is not None and expect not in render.EXPECTATIONS:
            raise Invalid(f"--examples[{i}] expect must be one of {', '.join(render.EXPECTATIONS)}")
        out.append((item["cmd"], source, expect))
    return out


def state_dir() -> str:
    return os.path.dirname(store.global_state_path())


def current_wrappers(args: Args | None = None) -> wrapper_table.Names:
    """The effective wrapper names; an unreadable state file contributes none (status reports it)."""

    def load(path: str | None) -> store.State:
        try:
            return store.load(path)
        except store.StateError:
            return {}

    return policy.effective_wrappers(store.load_managed(extra_path(args) if args else None)[0],
                                     load(store.global_state_path()), load(store.project_state_path()))


def cannot_evaluate_note(ev: matching.Evaluation) -> str:
    """Why this rule's program/args/builtin/ast part could not be judged, when it could not."""
    if ev.refusal:
        return f"cannot evaluate the parsing part of this rule: {ev.refusal}; the hook denies such a command"
    if isinstance(ev.outage, astrun.Missing):
        return (f"cannot evaluate the parsing part of this rule (program, args, builtin, match.ast): the engine is "
                f"missing ({ev.outage}); the hook allows the command without it and warns the session"
                f"{engine_fix(ev.outage)}")
    return (f"cannot evaluate the parsing part of this rule (program, args, builtin, match.ast): the engine failed "
            f"({ev.failure_kind()}: {ev.outage}); the hook allows the command and warns the session")


def cmd_rule_test(args: Args) -> int:
    check_stdin((args.json, "--json"), (args.examples, "--examples"))
    if not args.render and (args.intent or args.id_name or args.scope):
        raise Invalid("--intent, --id-name and --scope need --render")
    if args.id_name:
        check_name("rule", args.id_name)
    if any(not c.strip() for c in args.commands):
        raise Invalid("a command must not be blank")
    if args.json is None and (args.scope or args.id_name):
        raise Invalid("--scope and --id-name label a draft given with --json; --id reads them from the rule")
    examples: list[Example] = [(c, args.source, None) for c in args.commands]
    if args.examples is not None:
        examples += parse_examples(args.examples)
    mstate, gstate, pstate = (managed_state(args), store.load(store.global_state_path()),
                              store.load(store.project_state_path()))
    file = ""
    if args.json is not None:
        draft, carried = split_envelope(load_json(args.json, "--json"))
        if carried is not None:
            examples += example_list(carried)
        policy.validate_rule(draft)
        rule = policy.with_defaults(draft)
        label = "(draft)"
        layers: list[str] = []
        named = args.id_name or rule.get("id")
        rid = named if isinstance(named, str) and named else "new-rule"
        scope = args.scope or "global"
        if scope == "managed" and store.managed_write_path(extra_path(args)) != store.default_managed_path():
            file = store.managed_write_path(extra_path(args))
    else:
        rules = policy.effective_rules(mstate, gstate, pstate)
        if args.id not in rules:
            hidden = pstate.get("enabled", True) is False and args.id in view(pstate, "rules")
            raise Invalid(f"no rule '{args.id}'" + (" (project rules are disabled, so project entries are not "
                                                     "loaded)" if hidden else ""))
        rule = rules[args.id]
        policy.validate_rule(rule)
        layers = policy.origins("rules", mstate, gstate, pstate)[args.id]
        label = f"{args.id} [{'+'.join(layers)}]"
        rid, scope = args.id, "+".join(layers)

    if not examples:
        raise Invalid("give at least one command or --examples")
    notes = effect_notes(args, rule, layers, mstate, gstate, pstate)
    if file and not store.hook_enforces(file):
        notes.append(f"the hook enforces {file} only if {store.MANAGED_ENV} points at it")
    wrappers = policy.effective_wrappers(mstate, gstate, pstate)
    evaluations = [matching.evaluate(cmd, {rid: rule}, wrappers, state_dir()) for cmd, _, _ in examples]
    broken = {rid_: why for ev in evaluations for rid_, why in ev.invalid.items()}
    if broken:
        raise Invalid(f"the rule does not compile: {broken[rid]}")
    unjudged = next((ev for ev in evaluations if rid in ev.unevaluated), None)
    if unjudged:
        notes.append(cannot_evaluate_note(unjudged))
    results = [render.Result(cmd, source, render.UNEVALUATED if rid in ev.unevaluated else ev.kinds[rid], expect)
               for (cmd, source, expect), ev in zip(examples, evaluations)]
    if args.render:
        print(render.rule_card(rid, rule, policy.programs_of(rule), policy.render(rule["message"]), scope,
                               args.intent or "", results, notes, file))
        return 0
    flags = [str(rule["action"])]
    if rule.get("retry") == "same-command":
        flags.append("retry")
    if policy.modes_of(rule):
        flags.append("modes=" + ",".join(policy.modes_of(rule)))
    print(render.clean(f"rule {label}: {' '.join(flags)}"))
    if any(not note.startswith("cannot evaluate") for note in notes):
        print("note: match only means the matcher selects the command; the hook would not act on it as follows")
    for note in notes:
        print(render.clean(f"note: {note}"))
    for result in results:
        label = "cannot" if result.kind == render.UNEVALUATED else "match" if result.matched else "-"
        print(f"  {label:<7}{render.clean(result.cmd, chr(92) + 'n')}")
    print(render.clean(f"message: {policy.render(rule['message'])}", chr(92) + "n"))
    return 0


def cmd_rule_ast(args: Args) -> int:
    if not args.command.strip():
        raise Invalid("the command must not be blank")
    mstate, gstate, pstate = (managed_state(args), store.load(store.global_state_path()),
                              store.load(store.project_state_path()))
    try:
        response = matching.tree(args.command, policy.effective_wrappers(mstate, gstate, pstate), state_dir())
    except astrun.Unavailable as exc:
        raise Invalid(f"the AST engine is unavailable: {exc}{engine_fix(exc)}") from exc
    units = response["units"]
    print(render.clean(f"command: {args.command}"))
    print(f"units: {len(units)} (1 as written, {len(units) - 1} from shell strings)")
    if response.get("limit"):
        print(f"note: the command {matching.LIMIT_REASONS[response['limit']]}; the hook denies it and deeper units "
              "are not shown")
    for unit in units:
        print()
        if unit["label"]:
            print(render.clean(f"tree: {unit['label']}, source: {unit['src']}"))
        else:
            print("tree: command as written")
        for depth, kind, text in unit["nodes"]:
            leaf = f" \u00ab{render.clean(text)}\u00bb" if text is not None else ""
            print(f"{'  ' * (depth + 1)}{render.clean(kind)}{leaf}")
        if unit["broken"]:
            print("note: the parser reported errors here (ERROR or MISSING nodes), so rules see a partial tree")
    return 0


def cmd_engine_install(args: Args) -> int:
    try:
        print(astbin.install(state_dir(), lambda line: print(line, flush=True)))
    except (astbin.InstallError, astbin.Missing) as exc:
        raise Invalid(f"engine install failed: {exc}") from exc
    return 0


def cmd_engine_status(args: Args) -> int:
    info = astbin.status(state_dir())
    print(f"ast-grep pin: {info['pin']}")
    if "unsupported" in info:
        print(f"platform: {render.clean(info['unsupported'])}")
    else:
        print(f"platform: {info['platform']}")
        for name in ("npm", "wheel"):
            part = info[name]
            state = "usable" if part["usable"] else (f"REJECTED: {part['why']}" if part["present"] else
                                                     (f"unavailable: {part['why']}" if part["why"] else "not installed"))
            print(f"{name}: {state} ({render.clean(str(part['path']))})")
        if "marker" in info:
            mark = info["marker"]
            print(f"wheel marker: pin {mark['pin']}, binary sha256 {str(mark['binarySha256'])[:16]}…, installed "
                  f"{render.clean(str(mark['installedAt']))}")
        if "failure" in info:
            fail = info["failure"]
            print(f"last download failure: {render.clean(fail['reason'])} (retry after "
                  f"{astbin.retry_clock(state_dir())}; `guardrails engine install` ignores the wait)")
    print(f"active: {info['active'] or 'none'}" + (f" ({info['binary']})" if info["binary"] else ""))
    if not info["binary"] and "unsupported" in info:
        print("ast-grep cannot run on this platform, so rules using program, args, builtin or match.ast are NOT enforced "
              "(their commands are allowed); regex rules still are. There is no fix here: use regex rules or a "
              "supported system")
    elif not info["binary"]:
        print("the syntax-tree engine is missing, so rules using program, args, builtin or match.ast are NOT enforced "
              "(their commands are allowed); regex rules still are. To fix, run one of:")
        if not info["wheel"]["why"]:
            print(f"  {astbin.FIX_INSTALL}")
        print(f"  {astbin.npm_fix()}")
    return 0


def cmd_engine_verify(args: Args) -> int:
    results = astbin.verify(state_dir())
    for ok, line in results:
        print(f"{'ok' if ok else 'FAIL'}: {render.clean(line)}")
    return 0 if all(ok for ok, _ in results) else 2


def cmd_wrapper_add(args: Args) -> int:
    require_user(args, "wrapper add")
    try:
        wrapper_table.check_name(args.name)
    except ValueError as exc:
        raise Invalid(str(exc)) from exc
    if args.name in wrapper_table.DEFAULTS:
        raise Invalid(f"wrapper '{args.name}' is already built in")
    scope = resolve_scope(args)
    path = scope_path(scope, args)

    def change(state: store.State) -> bool:
        wrappers = table(state, "wrappers")
        existed = args.name in wrappers
        wrappers[args.name] = {"setBy": stamp(args.reason)}
        return existed

    existed = change_state(scope, path, change)
    print(f"{'replaced' if existed else 'added'} wrapper {args.name} in {path}")
    return 0


def cmd_wrapper_rm(args: Args) -> int:
    require_user(args, "wrapper rm")
    scope = resolve_scope(args)
    path = scope_path(scope, args)

    def change(state: store.State) -> None:
        wrappers = table(state, "wrappers")
        if args.name not in wrappers:
            built = " (built-in wrappers cannot be removed)" if args.name in wrapper_table.DEFAULTS else ""
            raise Invalid(f"no wrapper '{args.name}' in {path}{built}")
        del wrappers[args.name]

    change_state(scope, path, change)
    print(f"removed wrapper {args.name} from {path}")
    return 0


def cmd_wrapper_list(args: Args) -> int:
    mstate = managed_state(args)
    gstate, pstate = store.load(store.global_state_path()), store.load(store.project_state_path())
    origins = policy.origins("wrappers", mstate, gstate, pstate)
    shown = False
    for name in sorted(policy.effective_wrappers(mstate, gstate, pstate)):
        layers = (["builtin"] if name in wrapper_table.DEFAULTS else []) + origins.get(name, [])
        if args.scope and args.scope not in layers:
            continue
        shown = True
        print(render.clean(f"{name} [{'+'.join(layers)}]"))
    if not shown:
        print(f"no wrappers with a {args.scope} entry")
    return 0


def cmd_mode_declare(args: Args) -> int:
    require_user(args, "mode declare")
    check_name("mode", args.name)
    scope = resolve_scope(args)
    path = scope_path(scope, args)

    def change(state: store.State) -> None:
        modes = table(state, "modes")
        previous = view(modes, args.name)
        modes[args.name] = {"description": args.description or str(previous.get("description", "")),
                            "agentMayEnable": bool(args.agent_may_enable),
                            "active": previous.get("active") is True,
                            "setBy": stamp(args.reason)}

    change_state(scope, path, change)
    print(f"declared mode {args.name} in {path} (agent may enable: {'yes' if args.agent_may_enable else 'no'})")
    if scope != "managed" and args.name in view(managed_state(args), "modes"):
        print(f"note: mode {args.name} is also declared in the managed file, which wins: this declaration can "
              "only tighten it")
    return 0


def cmd_mode_undeclare(args: Args) -> int:
    require_user(args, "mode undeclare")
    scope = resolve_scope(args)
    path = scope_path(scope, args)

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
    check_path_scope(args, args.scope)
    path = scope_path(args.scope, args)
    managed, gstate = managed_state(args), store.load(store.global_state_path())
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
    if not active and args.scope != "managed" and policy.effective_modes(managed, {}, {}).get(
            args.name, {}).get("active"):
        print(f"note: the managed scope keeps mode {args.name} on")
    if active and args.scope == "project" and args.name in view(managed, "modes"):
        print(f"note: mode {args.name} is declared in the managed file, so a project cannot switch it on")
    return 0


def cmd_mode_on(args: Args) -> int:
    if args.scope != "session":
        return set_persistent(args, True)
    sid = session_id(args)
    modes = policy.effective_modes(managed_state(args), store.load(store.global_state_path()),
                                   store.load(store.project_state_path()))
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
    print(f"{'project rules' if args.scope == 'project' else 'guardrails hook'} {'enabled' if enabled else 'disabled'} ({path})")
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
    scope = resolve_scope(args)
    path = scope_path(scope, args)
    by = stamp(args.reason or f"preset {args.name}")
    report: list[str] = []

    def change(state: store.State) -> None:
        srules = table(state, "rules")
        for rid, rule in sorted(chosen.items()):
            current = srules.get(rid)
            if isinstance(current, dict) and {k: v for k, v in current.items() if k != "setBy"} == rule:
                report.append(f"rule {rid}: unchanged")
                continue
            always = " (no modes: always enforced)" if scope == "managed" and not policy.modes_of(rule) else ""
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
    pathed = argparse.ArgumentParser(add_help=False)
    pathed.add_argument("--path", metavar="FILE", help="a managed-format file: with --scope managed writes go there, "
                        "read verbs load it as an extra managed source; the hook enforces it only if "
                        "GUARDRAILS_MANAGED_PATH points at it")
    scoped = argparse.ArgumentParser(add_help=False, parents=[pathed])
    scoped.add_argument("--scope", choices=SCOPES, help="which state file to change (default: global); "
                        "managed needs root: sudo guardrails --scope managed ...")

    parser = argparse.ArgumentParser(prog="guardrails", description="Manage guardrails rules, modes and presets.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    status = verbs.add_parser("status", parents=[common, pathed], help="show effective rules and modes")
    status.add_argument("--scope", choices=SCOPES, help="list only rules and modes with an entry in this layer")
    status.add_argument("--problems", action="store_true", help="print only the problems")
    status.add_argument("--render", action="store_true", help="print markdown for pasting verbatim")
    status.add_argument("--rule", metavar="ID", help="with --render: print only this rule's row")

    rule = verbs.add_parser("rule", help="add, change or remove rules").add_subparsers(dest="op", required=True)
    add = rule.add_parser("add", parents=[common, scoped], help="add or replace a rule")
    add.add_argument("id")
    add.add_argument("--json", required=True, help="the rule as a JSON object, @<file> or - for stdin")
    set_ = rule.add_parser("set", parents=[common, scoped], help="change fields of a rule")
    set_.add_argument("id")
    set_.add_argument("assignments", nargs="*", metavar="key=value")
    set_.add_argument("--json", help="fields to change as a JSON object, @<file> or - for stdin")
    rm = rule.add_parser("rm", parents=[common, scoped], help="remove a rule")
    rm.add_argument("id")
    test = rule.add_parser("test", parents=[common, pathed], help="dry-run a rule against sample commands")
    source = test.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", help="a draft rule as a JSON object, @<file> or - for stdin")
    source.add_argument("--id", help="an installed rule's id")
    test.add_argument("commands", nargs="*", metavar="CMD")
    test.add_argument("--examples", help='JSON list of {"cmd", "source", "expect"} objects, @<file> or - for stdin')
    test.add_argument("--source", choices=render.SOURCES, default="inferred",
                      help="source tag of the positional commands (default: inferred)")
    test.add_argument("--render", action="store_true", help="print the presentation block for pasting verbatim")
    test.add_argument("--intent", help="with --render: the rule's intent line")
    test.add_argument("--id-name", help="with --render and --json: the id shown in the title")
    test.add_argument("--scope", choices=SCOPES, help="with --json: the scope shown in the title (default: global)")
    ast = rule.add_parser("ast", parents=[common, pathed], help="print the parse tree of a command, with the units "
                          "its wrappers and shell strings expose")
    ast.add_argument("command", metavar="CMD")

    wrapper = verbs.add_parser("wrapper", help="commands whose own words program/builtin rules look through (sudo, env, xargs, ...)"
                               ).add_subparsers(dest="op", required=True)
    wadd = wrapper.add_parser("add", parents=[common, scoped], help="add a wrapper name")
    wadd.add_argument("name")
    wrm = wrapper.add_parser("rm", parents=[common, scoped], help="remove a wrapper name from a scope")
    wrm.add_argument("name")
    wlist = wrapper.add_parser("list", parents=[common, pathed], help="list the effective wrappers")
    wlist.add_argument("--scope", choices=SCOPES, help="list only wrappers with an entry in this layer")

    mode = verbs.add_parser("mode", help="declare modes and switch them on or off").add_subparsers(dest="op",
                                                                                                required=True)
    declare = mode.add_parser("declare", parents=[common, scoped], help="declare (or redeclare) a mode")
    declare.add_argument("name")
    declare.add_argument("--description", default="")
    declare.add_argument("--agent-may-enable", action="store_true")
    undeclare = mode.add_parser("undeclare", parents=[common, scoped], help="remove a mode declaration")
    undeclare.add_argument("name")
    for op in ("on", "off"):
        toggle = mode.add_parser(op, parents=[common, pathed], help=f"switch a mode {op}")
        toggle.add_argument("name")
        toggle.add_argument("--scope", choices=("session", *SCOPES), default="session")

    preset = verbs.add_parser("preset", help="bundled rule sets").add_subparsers(dest="op", required=True)
    preset.add_parser("list", parents=[common], help="list presets")
    show = preset.add_parser("show", parents=[common], help="print a preset")
    show.add_argument("name")
    install = preset.add_parser("install", parents=[common, scoped], help="copy a preset's rules and modes into state")
    install.add_argument("name")
    install.add_argument("--only", help="comma-separated rule ids to install")

    engine = verbs.add_parser("engine", help="the ast-grep binary behind program, args, builtin and match.ast (not a configuration change, so "
                              "--as-user does not apply)").add_subparsers(dest="op", required=True)
    engine.add_parser("install", help="download the pinned, hash-checked ast-grep wheel binary into the plugin data dir")
    engine.add_parser("status", help="which ast-grep the hook uses, where it came from, and how to fix a missing one")
    engine.add_parser("verify", help="re-hash the active ast-grep binary against the committed manifest")

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
    ("wrapper", "add"): cmd_wrapper_add,
    ("wrapper", "rm"): cmd_wrapper_rm,
    ("wrapper", "list"): cmd_wrapper_list,
    ("mode", "declare"): cmd_mode_declare,
    ("mode", "undeclare"): cmd_mode_undeclare,
    ("mode", "on"): cmd_mode_on,
    ("mode", "off"): cmd_mode_off,
    ("preset", "list"): cmd_preset_list,
    ("preset", "show"): cmd_preset_show,
    ("preset", "install"): cmd_preset_install,
    ("engine", "install"): cmd_engine_install,
    ("engine", "status"): cmd_engine_status,
    ("engine", "verify"): cmd_engine_verify,
    ("enable", None): lambda args: set_enabled(args, True),
    ("disable", None): lambda args: set_enabled(args, False),
}


def unenforced_note(args: Args) -> str:
    path = extra_path(args)
    if path and getattr(args, "scope", None) == "managed" and not store.hook_enforces(path):
        return f"note: the hook enforces {path} only if {store.MANAGED_ENV} points at it"
    return ""


def sudo_hint(args: Args, argv: list[str]) -> str:
    if getattr(args, "scope", None) != "managed" or extra_path(args):
        return shlex.join(argv)
    target = scope_path("managed", args)
    extra = [] if target == store.default_managed_path() else ["--path", target]
    return shlex.join([*argv, *extra])


def parse(argv: list[str]) -> Args:
    """parse_args, except that key=value words after an option still reach `rule set` on every Python version."""
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)
    if rest and (args.verb, getattr(args, "op", None)) == ("rule", "set") and all("=" in word for word in rest):
        args.assignments += rest
    elif rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    return args


def main(argv: list[str]) -> int:
    args = parse(argv)
    try:
        code = HANDLERS[(args.verb, getattr(args, "op", None))](args)
        note = unenforced_note(args) if code == 0 else ""
        if note:
            print(note, file=sys.stderr if getattr(args, "render", False) else sys.stdout)
        return code
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except store.NotWritable as exc:
        guard = os.path.join(store.HERE, "guard.py")
        print(f"error: {exc}. Re-run with sudo: sudo python3 {guard} {sudo_hint(args, argv)}", file=sys.stderr)
        return 2
    except (Invalid, store.StateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
