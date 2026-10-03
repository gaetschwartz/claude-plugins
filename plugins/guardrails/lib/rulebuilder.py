"""Typed ast-grep rules for a guardrails rule: the program/args/builtin shorthand and the user's own match.ast, made
tolerant of how a command is spelled. All of it is data for ast-grep; nothing here reads shell syntax."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import policy

if TYPE_CHECKING:
    from ast_grep_py import Config, Rule

GREPS = ("grep", "egrep", "fgrep")
RECURSIVE_FLAG = (r"^(?:-[A-Za-z&&[^efmABCdD]]*[rR][A-Za-z]*|-drecurse|--recursive|--dereference-recursive"
                  r"|--directories=recurse)$")
TRAILING_HOLE = re.compile(r"^(.*\S)\s+\$\$\$$", re.DOTALL)
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
COMMAND_LIST = re.compile(r"[|;&\n]")
SIMPLE_COMMAND = re.compile(r"[A-Za-z0-9_.+-]+(?: [^|&;<>(){}`\n]*)?")
KEYWORDS = frozenset({"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "in",
                      "function", "select", "time", "coproc"})
ASSIGNMENTS = 3


def name_regex(names: Sequence[str]) -> str:
    """A command word that is one of these names, allowing quotes around it and a directory before it."""
    alternatives = "|".join(re.escape(name) for name in names)
    return f"^[\"']?(?:[^\\s]*/)?(?:{alternatives})[\"']?$"


def command_named(names: Sequence[str]) -> Rule:
    return {"kind": "command", "has": {"field": "name", "regex": name_regex(names)}}


def orphan_name(names: Sequence[str]) -> Rule:
    """A command name the parser found but could not build a command around: a `command_name` directly in an ERROR
    node, or a bare word there that is not an argument of a word before it."""
    found = name_regex(names)
    bare: Rule = {"kind": "word", "regex": found, "inside": {"kind": "ERROR"},
                  "not": {"follows": {"kind": "word", "stopBy": "neighbor"}}}
    return {"any": [{"kind": "command_name", "regex": found, "inside": {"kind": "ERROR"}}, bare]}


def recursive_flag() -> Rule:
    """A recursive flag among grep's words, not after a `--` that follows the grep word."""
    grep = name_regex(GREPS)
    after_grep = {"follows": {"regex": grep, "stopBy": "end"}}
    before_double_dash = {"not": {"follows": {"regex": "^--$", "stopBy": {"regex": grep}}}, **after_grep}
    glued = {"regex": "^(?:-d|--directories)$", "precedes": {"regex": "^recurse$"}}
    return {"any": [{"has": {"regex": RECURSIVE_FLAG, **before_double_dash}},
                    {"has": {**glued, **before_double_dash}}]}


def shorthand(match: dict[str, Any]) -> Rule | None:
    """The rule for program/args/builtin, or None when the rule has none of them."""
    programs, builtin = policy.programs_of({"match": match}), match.get("builtin")
    if not programs and not builtin:
        return None
    extra: list[Rule] = [{"regex": match["args"]}] if match.get("args") else []
    if builtin:
        extra.append(recursive_flag())
    name_sets = [names for names in (programs, list(GREPS) if builtin else []) if names]
    rule: Rule = {"all": [command_named(names) for names in name_sets] + extra}
    return rule if extra else {"any": [rule, orphan_name(programs)]}


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
    head = found.group(1)
    several = COMMAND_LIST.search(QUOTED.sub("", head))
    bare = head if several else {"context": head, "selector": "command"}
    either = {"any": [{"pattern": pattern}, {"pattern": bare}]}
    keep = {k: v for k, v in out.items() if k in ("stopBy", "field")}
    rest = {k: v for k, v in out.items() if k not in ("pattern", "stopBy", "field")}
    return {"all": [either, rest] if rest else [either], **keep}


def simple_command_name(text: str) -> str | None:
    """The command name when a pattern is exactly one simple command with arguments, else None."""
    text = text.strip()
    head, _, rest = text.partition(" ")
    if not rest or head in KEYWORDS or not SIMPLE_COMMAND.fullmatch(text):
        return None
    return head


def loosened(node: Any) -> Any:
    """The rule with the single-command patterns at its top level (through any/all) made tolerant: name in any
    spelling (quotes, a directory) and up to three leading assignments. None when it has none. Anything else is left
    for ast-grep as written."""
    if not isinstance(node, dict):
        return None
    out = dict(node)
    found = False
    for key in ("any", "all"):
        if isinstance(node.get(key), list):
            forms = [loosened(member) for member in node[key]]
            found = found or any(form is not None for form in forms)
            out[key] = ([form for form in forms if form is not None] if key == "any"
                        else [form or member for form, member in zip(forms, node[key], strict=True)])
    pattern = out.pop("pattern", None)
    in_context = isinstance(pattern, dict) and pattern.get("selector") == "command"
    text = pattern.get("context") if in_context else pattern
    name = simple_command_name(text) if isinstance(text, str) else None
    if name is None:
        if pattern is not None:
            out["pattern"] = pattern
    else:
        found = True
        checked = [{"has": {"field": "name", "regex": name_regex([name])}}, *out.get("all", [])]
        rest = text.strip().partition(" ")[2]
        variants = []
        for skipped in range(ASSIGNMENTS + 1):
            shaped = "$_A " * skipped + f"$_N {rest}"
            variants.append({**out, "pattern": {**pattern, "context": shaped} if in_context else shaped,
                             "all": checked})
        out = {"any": variants}
    return out if found and out.get("any", True) else None


def found_rule(rule: policy.Rule, plain: bool) -> Rule | None:
    """Where this rule matches (`plain`: the ast rule exactly as written)."""
    ast = policy.ast_of(rule)
    tolerant = None if plain or not ast else loosened(ast)
    parts = [part for part in (shorthand(policy.view(rule, "match")), widen(tolerant or ast) if ast else None) if part]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"any": parts}


def configs_of(rule: policy.Rule) -> tuple[Config, ...]:
    """The configs to try for this rule in order: spelling-tolerant first, then the ast rule as written."""
    first = found_rule(rule, False)
    if first is None:
        return ()
    second = found_rule(rule, True)
    return ({"rule": first},) if second == first or second is None else ({"rule": first}, {"rule": second})


def regex_config(rule: policy.Rule) -> Config | None:
    """The config that finds `match.regex` in the whole command's text (ast-grep's regex engine runs in linear time)."""
    pattern = policy.view(rule, "match").get("regex")
    return {"rule": {"kind": "program", "regex": pattern}} if isinstance(pattern, str) and pattern else None
