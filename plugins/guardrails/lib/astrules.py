"""ast-grep rules for a guardrails rule: the program/args/builtin shorthand, the user's own match.ast and the helper
rules that find shell strings. All of it is data for ast-grep; nothing here reads shell syntax."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import policy

Rule = dict[str, Any]

SHELLS = ("bash", "sh", "zsh", "dash", "ksh")
GREPS = ("grep", "egrep", "fgrep")
DIRECT, PIPED, WRAPPER = "direct", "piped", "wrapper"
SHELL_SCRIPT, EVAL_COMMAND, EVAL_ARG = "shell-script", "eval-command", "eval-arg"
CONTEXT = {"any": [{"kind": "pipeline"}, {"kind": "command_substitution"}, {"kind": "process_substitution"}],
           "stopBy": "end"}
ARGUMENT_KINDS = ("raw_string", "string", "ansi_c_string", "word", "number", "concatenation", "simple_expansion",
                  "expansion", "command_substitution", "arithmetic_expansion", "process_substitution")
COMMAND_STRING_FLAG = r"^-[A-Za-z]*c[A-Za-z]*$"
RECURSIVE_FLAG = r"^(?:-[A-Za-z&&[^efmABCdD]]*[rR][A-Za-z]*|-drecurse|--recursive|--dereference-recursive|--directories=recurse)$"
BEFORE_DOUBLE_DASH = {"not": {"follows": {"regex": "^--$", "stopBy": "end"}}}
TRAILING_HOLE = re.compile(r"^(.*\S)\s+\$\$\$$", re.DOTALL)
BEHIND_WRAPPER = "$W $$$ "


def name_regex(names: Sequence[str]) -> str:
    """A command word that is one of these names, allowing quotes around it and a directory before it."""
    alternatives = "|".join(re.escape(name) for name in names)
    return f"^[\"']?(?:[^\\s]*/)?(?:{alternatives})[\"']?$"


def command_named(names: Sequence[str]) -> Rule:
    return {"kind": "command", "has": {"field": "name", "regex": name_regex(names)}}


def command_wrapping(names: Sequence[str], wrappers: Sequence[str]) -> Rule:
    """A wrapper command one of whose own words is one of these names."""
    return {"kind": "command", "all": [{"has": {"field": "name", "regex": name_regex(wrappers)}},
                                       {"has": {"regex": name_regex(names)}}]}


def recursive_flag() -> Rule:
    glued = {"regex": "^(?:-d|--directories)$", "precedes": {"regex": "^recurse$"}}
    return {"any": [{"has": {"regex": RECURSIVE_FLAG, **BEFORE_DOUBLE_DASH}},
                    {"has": {**glued, **BEFORE_DOUBLE_DASH}}]}


def shorthand(match: dict[str, Any], wrappers: Sequence[str]) -> tuple[Rule | None, Rule | None]:
    """(direct rule, wrapper rule) for program/args/builtin; both None when the rule has none of them."""
    programs, builtin = policy.programs_of({"match": match}), match.get("builtin")
    if not programs and not builtin:
        return None, None
    extra = [{"regex": match["args"]}] if match.get("args") else []
    if builtin:
        extra.append(recursive_flag())
    name_sets = [names for names in (programs, list(GREPS) if builtin else []) if names]
    direct = [command_named(names) for names in name_sets] + extra
    wrapped = [command_wrapping(names, wrappers) for names in name_sets] + extra
    return {"all": direct}, {"all": wrapped}


def widen(rule: Any) -> Any:
    """A pattern ending in ` $$$` also selects the command when it has no arguments (the bare hole never does)."""
    if isinstance(rule, list):
        return [widen(item) for item in rule]
    if not isinstance(rule, dict):
        return rule
    out = {key: value if key == "pattern" else widen(value) for key, value in rule.items()}
    pattern = rule.get("pattern")
    found = TRAILING_HOLE.match(pattern.strip()) if isinstance(pattern, str) else None
    if not found:
        return out
    either = {"any": [{"pattern": pattern}, {"pattern": {"context": found.group(1), "selector": "command"}}]}
    keep = {k: v for k, v in out.items() if k in ("stopBy", "field")}
    rest = {k: v for k, v in out.items() if k not in ("pattern", "stopBy", "field")}
    return {"all": [either, rest] if rest else [either], **keep}


def behind_wrapper(node: Any) -> Rule | None:
    """The rule with each command pattern it names at its top level (through any/all) moved behind a wrapper and any
    words between; None when it names none."""
    if not isinstance(node, dict):
        return None
    out = dict(node)
    pattern = node.get("pattern")
    found = isinstance(pattern, str) or (isinstance(pattern, dict) and pattern.get("selector") == "command")
    if isinstance(pattern, str):
        out["pattern"] = BEHIND_WRAPPER + pattern
    elif found:
        out["pattern"] = {**pattern, "context": BEHIND_WRAPPER + pattern["context"]}
    for key in ("any", "all"):
        if isinstance(node.get(key), list):
            forms = [behind_wrapper(member) for member in node[key]]
            found = found or any(form is not None for form in forms)
            if key == "any":
                out[key] = [form for form in forms if form is not None]
            else:
                out[key] = [form or member for form, member in zip(forms, node[key])]
    return out if found and out.get("any", True) else None


def branches(rule: policy.Rule, wrappers: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The ast-grep rules that, together, say where this rule matches.

    `direct` hits count as the command itself, `piped` ones sit inside a pipeline or substitution, `wrapper` ones
    were reached through a wrapper: the shorthand's name behind a wrapper's own words, or the ast rule's command
    patterns behind any wrapper name.
    """
    match = policy.view(rule, "match")
    direct, wrapped = shorthand(match, wrappers)
    ast = policy.ast_of(rule)
    alternatives = [part for part in (direct, widen(ast) if ast else None) if part]
    if not alternatives:
        return {}
    found = alternatives[0] if len(alternatives) == 1 else {"any": alternatives}
    out: dict[str, dict[str, Any]] = {
        DIRECT: {"rule": {"all": [found, {"not": {"inside": CONTEXT}}]}},
        PIPED: {"rule": {"all": [found, {"inside": CONTEXT}]}}}
    shifted = behind_wrapper(ast) if ast else None
    behind = [part for part in (wrapped, widen(shifted) if shifted else None) if part]
    if behind:
        out[WRAPPER] = {"rule": behind[0] if len(behind) == 1 else {"any": behind}}
        if shifted:
            out[WRAPPER]["constraints"] = {"W": {"regex": name_regex(wrappers)}}
    return out


def shell_string_rules(wrappers: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Helpers that locate the script of `bash -c '...'` (also behind a wrapper), `eval` commands and their arguments."""
    shell = name_regex(SHELLS)
    runs_shell = {"kind": "command", "any": [
        {"has": {"field": "name", "regex": shell}},
        {"all": [{"has": {"field": "name", "regex": name_regex(wrappers)}}, {"has": {"regex": shell}}]}]}
    after_flag = {"any": [{"regex": COMMAND_STRING_FLAG}, {"regex": "^--$", "follows": {"regex": COMMAND_STRING_FLAG}}]}
    script = {"any": [{"kind": kind} for kind in ARGUMENT_KINDS], "follows": after_flag, "inside": runs_shell}
    evaluates = command_named(["eval"])
    eval_arg = {"any": [{"kind": kind} for kind in ARGUMENT_KINDS], "inside": evaluates}
    return {SHELL_SCRIPT: {"rule": script}, EVAL_COMMAND: {"rule": evaluates}, EVAL_ARG: {"rule": eval_arg}}


def documents(rules: dict[str, policy.Rule], wrappers: Sequence[str]
              ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, str]]]:
    """(every ast-grep rule to run, by id; the (guardrails rule id, branch) behind each id that is not a helper)."""
    bodies = shell_string_rules(wrappers)
    owners: dict[str, tuple[str, str]] = {}
    for index, (rid, rule) in enumerate(rules.items()):
        for branch, body in branches(rule, wrappers).items():
            bodies[f"{index}:{branch}"] = body
            owners[f"{index}:{branch}"] = (rid, branch)
    return bodies, owners
