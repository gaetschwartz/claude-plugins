from __future__ import annotations

from types import ModuleType

from . import fd_rg, pkill

MODULES: tuple[ModuleType, ...] = (pkill, fd_rg)


def all_rules() -> dict[str, str]:
    """{rule_id: description} across every module, in registry order."""
    out: dict[str, str] = {}
    for mod in MODULES:
        out.update(mod.RULES)
    return out


def requires() -> dict[str, tuple[str, ...]]:
    """{rule_id: candidate binaries} for rules that need a replacement tool installed."""
    out: dict[str, tuple[str, ...]] = {}
    for mod in MODULES:
        out.update(getattr(mod, "REQUIRES", {}))
    return out
