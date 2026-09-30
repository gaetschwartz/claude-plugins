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
LITERAL_NAME = re.compile(r"[A-Za-z0-9_.+-]+")


def name_regex(names: Sequence[str]) -> str:
    """A command word that is one of these names, allowing quotes around it and a directory before it."""
    alternatives = "|".join(re.escape(name) for name in names)
    return f"^[\"']?(?:[^\\s]*/)?(?:{alternatives})[\"']?$"


def command_named(names: Sequence[str]) -> Rule:
    return {"kind": "command", "has": {"field": "name", "regex": name_regex(names)}}


def orphan_name(names: Sequence[str]) -> Rule:
    """A command name the parser found but could not build a command around (it sits directly in an ERROR node)."""
    return {"kind": "command_name", "regex": name_regex(names), "inside": {"kind": "ERROR"}}


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
    direct = {"all": [command_named(names) for names in name_sets] + extra}
    wrapped = {"all": [command_wrapping(names, wrappers) for names in name_sets] + extra}
    if not extra:
        direct = {"any": [direct, orphan_name(programs)]}
    return direct, wrapped


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


def loosen_command(text: str, behind: bool) -> tuple[str, str] | None:
    """(pattern, name) for a command pattern: with `behind`, the pattern as written moved behind a wrapper name;
    otherwise its name replaced by a wildcard, to be checked separately so that any spelling of it passes (quotes, a
    directory). None unless the pattern starts with a plain name and has arguments."""
    head, _, rest = text.strip().partition(" ")
    if not rest or not LITERAL_NAME.fullmatch(head):
        return None
    return (f"$W $$$ {head} {rest}" if behind else f"$_N {rest}"), head


def loosened(node: Any, consts: dict[str, Any], behind: bool, wrappers: Sequence[str]) -> Rule | None:
    """The rule with the commands it names at its top level (through any/all) accepted in any spelling of the name, or
    with `behind` behind a wrapper's own words; None when it names none."""
    if not isinstance(node, dict):
        return None
    out = dict(node)
    also: list[Rule] = []
    pattern = node.get("pattern")
    in_context = isinstance(pattern, dict) and pattern.get("selector") == "command"
    text = pattern.get("context") if in_context else pattern
    found = loosen_command(text, behind) if isinstance(text, str) else None
    if found:
        new, name = found
        out["pattern"] = {**pattern, "context": new} if in_context else new
        if behind:
            consts["W"] = {"regex": name_regex(wrappers)}
        else:
            also.append({"has": {"field": "name", "regex": name_regex([name])}})
    for key in ("any", "all"):
        if isinstance(node.get(key), list):
            forms = [loosened(member, consts, behind, wrappers) for member in node[key]]
            found = found or any(form is not None for form in forms)
            out[key] = ([form for form in forms if form is not None] if key == "any"
                        else [form or member for form, member in zip(forms, node[key])])
    has = node.get("has")
    if behind and node.get("kind") == "command" and isinstance(has, dict) and has.get("field") == "name" \
            and set(has) == {"field", "regex"}:
        del out["has"]
        also += [{"has": {"field": "name", "regex": name_regex(wrappers)}}, {"has": {"regex": has["regex"]}}]
        found = True
    if also:
        out["all"] = [*also, *out.get("all", [])]
    return out if found and out.get("any", True) else None


def body(rule: Rule, consts: dict[str, Any]) -> dict[str, Any]:
    return {"rule": rule, **({"constraints": consts} if consts else {})}


def branches(rule: policy.Rule, wrappers: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The ast-grep rules that, together, say where this rule matches.

    `direct` hits count as the command itself, `piped` ones sit inside a pipeline or substitution, `wrapper` ones
    were reached through a wrapper: the shorthand's name behind a wrapper's own words, or the ast rule's commands
    behind any wrapper name.
    """
    match = policy.view(rule, "match")
    direct, wrapped = shorthand(match, wrappers)
    ast = policy.ast_of(rule)
    consts: dict[str, Any] = {}
    parts = [direct, widen(loosened(ast, consts, False, wrappers) or ast) if ast else None]
    alternatives = [part for part in parts if part]
    if not alternatives:
        return {}
    found = alternatives[0] if len(alternatives) == 1 else {"any": alternatives}
    out = {DIRECT: body({"all": [found, {"not": {"inside": CONTEXT}}]}, consts),
           PIPED: body({"all": [found, {"inside": CONTEXT}]}, consts)}
    behind_consts: dict[str, Any] = {}
    shifted = loosened(ast, behind_consts, True, wrappers) if ast else None
    behind = [part for part in (wrapped, widen(shifted) if shifted else None) if part]
    if behind:
        out[WRAPPER] = body(behind[0] if len(behind) == 1 else {"any": behind}, behind_consts)
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
