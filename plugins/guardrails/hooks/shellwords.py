"""Tokenise a Bash command line into the simple commands it would run.

Shared by the rules so they all agree on what counts as "the command being
invoked" (wrappers like sudo/xargs/timeout are looked through, `bash -c`/eval
strings and $(...) substitutions are descended into, heredoc bodies and
redirect targets are not commands).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

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
    "xargs": {"-I", "-i", "-d", "-a", "-E", "-L", "-n", "-P", "-s",
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
SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
ANSI_C_QUOTED = re.compile(r"\$'(?:\\.|[^'\\])*'")
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|(\w+))(?!<)")


@dataclass
class SimpleCommand:
    name: str
    args: list[str] = field(default_factory=list)
    assigns: list[str] = field(default_factory=list)


def strip_heredocs(text: str) -> str:
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            dash, *names = m.groups()
            delim = next(n for n in names if n)
            end = next((j for j in range(i, len(lines))
                        if (lines[j].lstrip("\t") if dash else lines[j]) == delim), None)
            if end is not None:
                i = end + 1
    return "\n".join(out)


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


def simple_commands(text: str, depth: int = 0) -> list[SimpleCommand]:
    """Raise ValueError on unbalanced quotes, like shlex."""
    if depth > 3:
        return []

    text = ANSI_C_QUOTED.sub("''", strip_heredocs(text))
    cmds: list[SimpleCommand] = []
    current: SimpleCommand | None = None
    assigns: list[str] = []
    wrapper: str | None = None
    skip_next = False
    inline_script = False
    sink = SimpleCommand("")

    for token in _tokens(text):
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
            continue
        if current is not None:
            current.args.append(token)
            continue
        if ASSIGN.match(token):
            assigns.append(token)
            continue
        if token.startswith("-"):
            if wrapper == "command" and token in ("-v", "-V"):
                current = sink
            elif wrapper and token in WRAPPER_OPTS_WITH_ARG.get(wrapper, ()):
                skip_next = True
            continue
        if token in KEYWORDS or DURATION.match(token) or PLACEHOLDER.match(token):
            continue
        if inline_script:
            cmds.extend(simple_commands(token, depth + 1))
            inline_script = False
            current = sink
            continue

        base = token.rsplit("/", 1)[-1]
        if base in WRAPPERS:
            wrapper = base
            continue
        if base in RECURSE:
            wrapper = base
            inline_script = True
            continue
        current = SimpleCommand(base, [], assigns)
        assigns = []
        wrapper = None
        cmds.append(current)

    for groups in SUBST.findall(mask_single_quoted(text)):
        for inner in groups:
            if inner:
                cmds.extend(simple_commands(inner, depth + 1))

    return cmds
