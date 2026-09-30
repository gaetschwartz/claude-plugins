from __future__ import annotations  # noqa: I001

from helpers import AstIsolated, ast_mode

import parity_study as study


def unbalanced_quotes(command: str) -> bool:
    import matching

    return matching.lex(command) is None


def nested_substitution(command: str) -> bool:
    return "$(" in command and command.count("$(") > 1 or "$(z)" in command


def explained(command: str, rule: str) -> str | None:
    """Why the two engines may disagree on this command, or None when it is unexplained."""
    if rule == "echo-anchored":
        return "args regex is anchored on the joined arguments; the compiled rule can only see the command text"
    if unbalanced_quotes(command):
        return "unbalanced quotes: only the stdlib path has the raw-text fallback"
    if nested_substitution(command):
        return "stdlib lexer loses commands inside nested substitutions; the tree is right"
    return None


class Parity(AstIsolated):
    """The compiled program/builtin matchers agree with the stdlib path except in explained ways (see references)."""

    def check(self, commands: list[str]) -> None:
        unexplained = [(c, r, p) for c, r, p, _ in study.run(commands) if explained(c, r) is None]
        self.assertEqual(unexplained, [])

    def test_existing_corpus(self) -> None:
        self.check(study.corpus())

    def test_fuzz_sample(self) -> None:
        if ast_mode() != "inprocess":
            self.skipTest("the fuzz sample needs ast-grep-py importable in this process")
        self.check(study.fuzz(700, 5))

    def test_default_matchers_stay_on_the_stdlib_path(self) -> None:
        import matching

        self.assertFalse(matching.PARITY)
