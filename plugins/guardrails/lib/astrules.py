"""ast-grep rules for a guardrails rule: the program/args/builtin shorthand, the user's own match.ast and the helper
rules that find shell strings. All of it is data for ast-grep; nothing here reads shell syntax."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Any

import policy

Rule = dict[str, Any]

SHELLS = ("bash", "sh", "zsh", "dash", "ksh", "script")
GREPS = ("grep", "egrep", "fgrep")
DIRECT, PIPED, WRAPPER = "direct", "piped", "wrapper"
SHELL_SCRIPT, EVAL_COMMAND, EVAL_ARG = "shell-script", "eval-command", "eval-arg"
HEREDOC_BODY, SHELL_HEREDOC, SHELL_HERESTRING, SUBSTITUTION = ("heredoc-body", "shell-heredoc", "shell-herestring",
                                                                "substitution")
CONTEXT = {"any": [{"kind": "pipeline"}, {"kind": "command_substitution"}, {"kind": "process_substitution"}],
           "stopBy": "end"}
ARGUMENT_KINDS = ("raw_string", "string", "ansi_c_string", "word", "number", "concatenation", "simple_expansion",
                  "expansion", "command_substitution", "arithmetic_expansion", "process_substitution")
COMMAND_STRING_FLAG = r"^-[A-Za-z]*c[A-Za-z]*$"
RECURSIVE_FLAG = r"^(?:-[A-Za-z&&[^efmABCdD]]*[rR][A-Za-z]*|-drecurse|--recursive|--dereference-recursive|--directories=recurse)$"
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
    """A command name the parser found but could not build a command around (it sits directly in an ERROR node)."""
    return {"kind": "command_name", "regex": name_regex(names), "inside": {"kind": "ERROR"}}


def command_wrapping(names: Sequence[str], wrappers: Sequence[str]) -> Rule:
    """A wrapper command one of whose own words is one of these names."""
    return {"kind": "command", "all": [{"has": {"field": "name", "regex": name_regex(wrappers)}},
                                       {"has": {"regex": name_regex(names)}}]}


def recursive_flag() -> Rule:
    """A recursive flag among grep's words, not after a `--` that follows the grep word (a wrapper's `--` is not one)."""
    grep = name_regex(GREPS)
    after_grep = {"follows": {"regex": grep, "stopBy": "end"}}
    before_double_dash = {"not": {"follows": {"regex": "^--$", "stopBy": {"regex": grep}}}, **after_grep}
    glued = {"regex": "^(?:-d|--directories)$", "precedes": {"regex": "^recurse$"}}
    return {"any": [{"has": {"regex": RECURSIVE_FLAG, **before_double_dash}},
                    {"has": {**glued, **before_double_dash}}]}


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
    head = found.group(1)
    several = COMMAND_LIST.search(QUOTED.sub("", head))
    bare = head if several else {"context": head, "selector": "command"}
    either = {"any": [{"pattern": pattern}, {"pattern": bare}]}
    keep = {k: v for k, v in out.items() if k in ("stopBy", "field")}
    rest = {k: v for k, v in out.items() if k not in ("pattern", "stopBy", "field")}
    return {"all": [either, rest] if rest else [either], **keep}


def simple_command(text: str) -> tuple[str, list[str]] | None:
    """(name, literal words) when a pattern is exactly one simple command with arguments, else None."""
    text = text.strip()
    head, _, rest = text.partition(" ")
    if not rest or head in KEYWORDS or not SIMPLE_COMMAND.fullmatch(text):
        return None
    return head, [word for word in rest.split() if not word.startswith("$") and "'" not in word and '"' not in word]


def word_named(name: str) -> Rule:
    return {"has": {"regex": name_regex([name])}}


def loosened(node: Any, wrappers: Sequence[str], behind: bool) -> Rule | None:
    """The rule with the single-command patterns at its top level (through any/all) made tolerant: name in any
    spelling (quotes, a directory) and up to three leading assignments; with `behind`, the command behind a wrapper,
    by name and literal words. None when it has none. Anything else is left for ast-grep as written."""
    if not isinstance(node, dict):
        return None
    out = dict(node)
    found = False
    for key in ("any", "all"):
        if isinstance(node.get(key), list):
            forms = [loosened(member, wrappers, behind) for member in node[key]]
            found = found or any(form is not None for form in forms)
            out[key] = ([form for form in forms if form is not None] if key == "any"
                        else [form or member for form, member in zip(forms, node[key])])
    has = node.get("has")
    if behind and node.get("kind") == "command" and isinstance(has, dict) and has.get("field") == "name" \
            and set(has) == {"field", "regex"}:
        del out["has"]
        out["all"] = [{"has": {"field": "name", "regex": name_regex(wrappers)}}, {"has": {"regex": has["regex"]}},
                      *out.get("all", [])]
        found = True
    pattern = out.pop("pattern", None)
    in_context = isinstance(pattern, dict) and pattern.get("selector") == "command"
    text = pattern.get("context") if in_context else pattern
    command = simple_command(text) if isinstance(text, str) else None
    if command is None:
        if pattern is not None:
            out["pattern"] = pattern
    else:
        name, words = command
        found = True
        if behind:
            named = [{"has": {"field": "name", "regex": name_regex(wrappers)}}, word_named(name),
                     *({"has": {"regex": f"^{re.escape(word)}$"}} for word in words)]
            out["all"] = [{"kind": "command"}, *named, *out.get("all", [])]
        else:
            checked = [{"has": {"field": "name", "regex": name_regex([name])}}, *out.get("all", [])]
            rest = text.strip().partition(" ")[2]
            variants = []
            for skipped in range(ASSIGNMENTS + 1):
                shaped = "$_A " * skipped + f"$_N {rest}"
                variants.append({**out, "pattern": {**pattern, "context": shaped} if in_context else shaped,
                                 "all": checked})
            out = {"any": variants}
    return out if found and out.get("any", True) else None


def branches(rule: policy.Rule, wrappers: Sequence[str], plain: bool = False) -> dict[str, dict[str, Any]]:
    """The ast-grep rules that, together, say where this rule matches (`plain`: the ast rule exactly as written).

    `direct` hits count as the command itself, `piped` ones sit inside a pipeline or substitution, `wrapper` ones
    were reached through a wrapper: the shorthand's name behind a wrapper's own words, or the ast rule's commands
    behind any wrapper name.
    """
    match = policy.view(rule, "match")
    direct, wrapped = shorthand(match, wrappers)
    ast = policy.ast_of(rule)
    tolerant = None if plain or not ast else loosened(ast, wrappers, False)
    alternatives = [part for part in (direct, widen(tolerant or ast) if ast else None) if part]
    if not alternatives:
        return {}
    found = alternatives[0] if len(alternatives) == 1 else {"any": alternatives}
    out = {DIRECT: {"rule": {"all": [found, {"not": {"inside": CONTEXT}}]}},
           PIPED: {"rule": {"all": [found, {"inside": CONTEXT}]}}}
    shifted = None if plain or not ast else loosened(ast, wrappers, True)
    behind = [part for part in (wrapped, widen(shifted) if shifted else None) if part]
    if behind:
        out[WRAPPER] = {"rule": behind[0] if len(behind) == 1 else {"any": behind}}
    return out


def shell_string_rules(wrappers: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Helpers that locate what a shell will run as text: the script of `bash -c '...'` (also behind a wrapper and for
    `script -c`), `eval` commands and their arguments, heredocs and here-strings fed to a shell, the bodies of
    unquoted heredocs (their `$(...)` and backticks run) and the substitutions a unit contains."""
    shell = name_regex(SHELLS)
    runs_shell = {"kind": "command", "any": [
        {"has": {"field": "name", "regex": shell}},
        {"all": [{"has": {"field": "name", "regex": name_regex(wrappers)}}, {"has": {"regex": shell}}]}]}
    after_flag = {"any": [{"regex": COMMAND_STRING_FLAG}, {"regex": "^--$", "follows": {"regex": COMMAND_STRING_FLAG}}]}
    arguments = {"any": [{"kind": kind} for kind in ARGUMENT_KINDS]}
    script = {**arguments, "follows": after_flag, "inside": runs_shell}
    evaluates = command_named(["eval"])
    heredoc = {"kind": "heredoc_body", "inside": {"kind": "heredoc_redirect"}}
    unquoted = {**heredoc, "inside": {"kind": "heredoc_redirect", "has": {"kind": "heredoc_start", "regex": "^[A-Za-z0-9_]+$"}}}
    fed = {**heredoc, "inside": {"kind": "heredoc_redirect", "inside": {"kind": "redirected_statement", "has": runs_shell}}}
    herestring = {**arguments, "inside": {"kind": "herestring_redirect", "inside": runs_shell}}
    return {SHELL_SCRIPT: {"rule": script}, EVAL_COMMAND: {"rule": evaluates},
            EVAL_ARG: {"rule": {**arguments, "inside": evaluates}}, HEREDOC_BODY: {"rule": unquoted},
            SHELL_HEREDOC: {"rule": fed}, SHELL_HERESTRING: {"rule": herestring},
            SUBSTITUTION: {"rule": {"kind": "command_substitution"}}}


def documents(rules: dict[str, policy.Rule], wrappers: Sequence[str], plain: Collection[str] = ()
              ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, str]]]:
    """(every ast-grep rule to run, by id; the (guardrails rule id, branch) behind each id that is not a helper)."""
    bodies = shell_string_rules(wrappers)
    owners: dict[str, tuple[str, str]] = {}
    for index, (rid, rule) in enumerate(rules.items()):
        for branch, body in branches(rule, wrappers, rid in plain).items():
            bodies[f"{index}:{branch}"] = body
            owners[f"{index}:{branch}"] = (rid, branch)
    return bodies, owners
