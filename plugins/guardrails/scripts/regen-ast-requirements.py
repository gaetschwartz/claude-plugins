#!/usr/bin/env python3
"""Rewrite lib/ast-requirements.txt for PIN with the sha256 of every wheel PyPI lists (run after bumping PIN in lib/astrun.py)."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "lib")


def pin() -> str:
    with open(os.path.join(LIB, "astrun.py")) as fh:
        match = re.search(r'^PIN = "([^"]+)"', fh.read(), re.MULTILINE)
    if match is None:
        sys.exit("PIN not found in lib/astrun.py")
    return match.group(1)


def main() -> int:
    version = pin()
    with urllib.request.urlopen(f"https://pypi.org/pypi/ast-grep-py/{version}/json", timeout=30) as response:
        files = json.load(response)["urls"]
    hashes = sorted(f["digests"]["sha256"] for f in files if f["filename"].endswith(".whl"))
    if not hashes:
        sys.exit(f"no wheels listed for ast-grep-py {version}")
    lines = [f"ast-grep-py=={version} \\"]
    lines += [f"    --hash=sha256:{h} \\" for h in hashes]
    lines[-1] = lines[-1].removesuffix(" \\")
    with open(os.path.join(LIB, "ast-requirements.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"ast-grep-py=={version}: {len(hashes)} wheel hashes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
