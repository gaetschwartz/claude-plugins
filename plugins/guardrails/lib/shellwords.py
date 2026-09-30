"""Tokenise a Bash command line into the simple commands it would run.

Shared by the rules so they all agree on what counts as "the command being
invoked" (wrappers like sudo/xargs/timeout are looked through, `bash -c`/eval
strings and $(...) substitutions are descended into, heredoc bodies and
redirect targets are not commands).
"""

from __future__ import annotations

import re
import shlex
from typing import Any

PUNCT = "();|&\n<>"
KEYWORDS = {"if", "then", "else", "elif", "fi", "while", "until", "for",
            "select", "function", "do", "done", "case", "esac", "in", "!", "{", "}"}
WRAPPERS = {"sudo", "doas", "env", "command", "builtin", "exec", "nohup",
            "setsid", "stdbuf", "time", "timeout", "xargs", "nice", "ionice"}
# their payload is a shell string (or the rest of the line), parsed recursively
RECURSE = {"eval", "bash", "sh", "zsh", "dash", "ksh", "watch", "script"}
# wrapper options that consume the following token (so it is not a command)
WRAPPER_OPTS_WITH_ARG = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "--user", "--group"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "timeout": {"-k", "-s", "--kill-after", "--signal"},
    "xargs": {"-I", "-d", "-a", "-E", "-L", "-n", "-P", "-s",
              "--replace", "--delimiter", "--arg-file", "--max-args",
              "--max-procs", "--max-lines"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "watch": {"-n", "-d", "--interval"},
}

ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DURATION = re.compile(r"^[0-9]+(\.[0-9]+)?[smhd]?$")
PLACEHOLDER = re.compile(r"^\{.*\}$")
ANSI_C_QUOTED = re.compile(r"\$'(?:\\.|[^'\\])*'")
MAX_DEPTH = 16
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|(\w+))(?!<)")


class SimpleCommand:
    __slots__ = ("args", "assigns", "name", "wrapped")

    def __init__(self, name: str, args: list[str] | None = None, assigns: list[str] | None = None,
                 wrapped: bool = False) -> None:
        self.name = name
        self.args = [] if args is None else args
        self.assigns = [] if assigns is None else assigns
        self.wrapped = wrapped


def split_heredocs(text: str) -> tuple[str, list[str]]:
    """The text without heredoc bodies, and the bodies whose delimiter is unquoted (their substitutions run)."""
    lines = text.split("\n")
    out = []
    code = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            dash, single, double, bare = m.groups()
            delim = single or double or bare
            end = next((j for j in range(i, len(lines))
                        if (lines[j].lstrip("\t") if dash else lines[j]) == delim), None)
            if end is not None:
                if bare:
                    code.append("\n".join(lines[i:end]))
                i = end + 1
    return "\n".join(out), code


def strip_heredocs(text: str) -> str:
    return split_heredocs(text)[0]


def match_paren(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def substitutions(text: str) -> list[str]:
    """Bodies of the outermost $(...) and `...` in text (nested ones are found when a body is parsed in turn)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("$((", i):
            i += 3
        elif text.startswith("$(", i):
            j = match_paren(text, i + 1)
            if j < 0:
                i += 2
                continue
            out.append(text[i + 2:j])
            i = j + 1
        elif text[i] == "`":
            j = text.find("`", i + 1)
            if j < 0:
                break
            out.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return out


def cluster_takes_value(token: str, flags: set[str]) -> bool:
    """A cluster of short flags such as -nu whose last letter takes the next word (an earlier one takes a glued value)."""
    if len(token) < 3 or token[0] != "-" or token[1] == "-" or not token[1:].isalpha():
        return False
    for i, letter in enumerate(token[1:]):
        if f"-{letter}" in flags:
            return i == len(token) - 2
    return False


def mask_single_quoted(text: str) -> str:
    """Blank single-quoted runs; an apostrophe inside "..." is literal."""
    out = []
    i, n, in_double = 0, len(text), False
    while i < n:
        c = text[i]
        if c == "\\" and not in_double:
            out.append("  ")
            i += 2
            continue
        if c == '"':
            in_double = not in_double
        elif c == "'" and not in_double:
            j = text.find("'", i + 1)
            if j == -1:
                out.append(" " * (n - i))
                break
            out.append(" " * (j - i + 1))
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=PUNCT)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def user_wrappers(table: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {name: entry for name, entry in (table or {}).items()
            if name not in WRAPPERS and name not in RECURSE and not entry.get("shellString")}


def simple_commands(text: str, depth: int = 0, table: dict[str, dict[str, Any]] | None = None) -> list[SimpleCommand]:
    """Raise ValueError on unbalanced quotes, like shlex.

    A user wrapper is parsed both as a wrapper and as a plain command and the results are united, so declaring
    one can never hide a command.
    """
    plain = _commands(text, depth, {})
    extra = user_wrappers(table)
    if not extra:
        return plain
    seen = {(c.name, tuple(c.args), tuple(c.assigns), c.wrapped) for c in plain}
    for cmd in _commands(text, depth, extra):
        if (cmd.name, tuple(cmd.args), tuple(cmd.assigns), cmd.wrapped) not in seen:
            plain.append(cmd)
    return plain


def _commands(text: str, depth: int, extra: dict[str, dict[str, Any]]) -> list[SimpleCommand]:
    if depth > MAX_DEPTH:
        return []
    wrappers = set(WRAPPERS) | set(extra)
    opts_with_arg = {name: set(flags) for name, flags in WRAPPER_OPTS_WITH_ARG.items()}
    for name, entry in extra.items():
        opts_with_arg.setdefault(name, set()).update(entry.get("flagsWithValue", ()))

    text, bodies = split_heredocs(text)
    text = ANSI_C_QUOTED.sub("''", text)
    cmds: list[SimpleCommand] = []
    current: SimpleCommand | None = None
    assigns: list[str] = []
    wrapper: str | None = None
    skip_next = False
    inline_script = False
    sink = SimpleCommand("")
    last: SimpleCommand | None = None
    pipe_next = False
    subst: list[bool] = []

    tokens = _tokens(text)
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token and all(c in PUNCT for c in token):
            if ("<" in token or ">" in token) and not ("(" in token or ")" in token):
                skip_next = True
            else:
                current = None
                assigns = []
                wrapper = None
                inline_script = False
                for offset, char in enumerate(token):
                    if char == "(":
                        glued = tokens[index - 1].endswith("$") if offset == 0 and index else token[offset - 1] in "<>$"
                        subst.append(glued)
                    elif char == ")" and subst:
                        subst.pop()
                operator = token.strip("\n")
                if operator in ("|", "|&"):
                    if last is not None:
                        last.wrapped = True
                    pipe_next = True
                elif operator:
                    pipe_next = False
                last = None
            continue
        if current is not None:
            current.args.append(token)
            continue
        if ASSIGN.match(token) and not (inline_script and any(c.isspace() for c in token)):
            assigns.append(token)
            continue
        if token.startswith("-"):
            if wrapper == "command" and token in ("-v", "-V"):
                current = sink
            elif wrapper and (token in opts_with_arg.get(wrapper, ()) or cluster_takes_value(
                    token, opts_with_arg.get(wrapper, set()))):
                skip_next = True
            continue
        if token in KEYWORDS or DURATION.match(token) or PLACEHOLDER.match(token):
            continue
        if inline_script:
            cmds.extend(_commands(token, depth + 1, extra))
            inline_script = False
            current = sink
            continue

        base = token.rsplit("/", 1)[-1]
        if base in wrappers:
            wrapper = base
            continue
        if base in RECURSE:
            wrapper = base
            inline_script = True
            continue
        current = SimpleCommand(base, [], assigns, bool(wrapper) or depth > 0 or pipe_next or any(subst))
        assigns = []
        wrapper = None
        pipe_next = False
        last = current
        cmds.append(current)

    for inner in substitutions(mask_single_quoted(text)):
        if inner.strip():
            cmds.extend(_commands(inner, depth + 1, extra))
    for body in bodies:
        for inner in substitutions(body):
            if inner.strip():
                cmds.extend(_commands(inner, depth + 1, extra))

    return cmds
