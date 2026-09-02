from __future__ import annotations

import re

from context import Context, Decision

RULES: dict[str, str] = {"pkill": "Deny pkill (signalling by name pattern); use pgrep + kill <pid>"}
KEYWORDS: tuple[str, ...] = ("pkill",)

TARGET = "pkill"
FALLBACK = re.compile(r"(^|[^A-Za-z0-9_.-])([^\s]*/)?" + TARGET + r"([^A-Za-z0-9_-]|$)")

REASON = (
    "pkill is blocked by the shell-guard hook. Signalling processes by name pattern is "
    "too broad: it can match unrelated processes and kill the user's editors, "
    "agents, or servers. Target a specific PID instead (pgrep/ps to identify it, "
    "confirm the match, then kill <pid>), or ask the user to run the pkill themselves."
)

WARNING = (
    "Note: pkill is heavily discouraged here. It is not being invoked by this "
    "command, so nothing was blocked - but prefer pgrep + kill <pid> over "
    "name-pattern signalling anywhere it ends up running."
)


def check(ctx: Context) -> Decision | None:
    cmds = ctx.cmds
    if cmds is None:
        invoked = FALLBACK.search(ctx.command) is not None
    else:
        invoked = any(c.name == TARGET for c in cmds)

    if invoked:
        return "deny", REASON
    if FALLBACK.search(ctx.command):
        return "warn", WARNING
    return None
