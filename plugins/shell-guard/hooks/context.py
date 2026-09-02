from __future__ import annotations

import shutil
from typing import Literal

from shellwords import SimpleCommand, simple_commands

Decision = tuple[Literal["deny", "warn"], str]


class Context:
    """What a rule gets to look at for one Bash tool call."""

    def __init__(self, command: str, transcript_path: str | None, enabled: frozenset[str]) -> None:
        self.command = command
        self.transcript_path = transcript_path
        self.enabled = enabled
        self._cmds: list[SimpleCommand] | None = None
        self._parsed = False

    @property
    def cmds(self) -> list[SimpleCommand] | None:
        """Simple commands, or None when the lexer rejects the input."""
        if not self._parsed:
            self._parsed = True
            try:
                self._cmds = simple_commands(self.command)
            except ValueError:
                self._cmds = None
        return self._cmds

    @staticmethod
    def which(name: str) -> str:
        return shutil.which(name) or ""
