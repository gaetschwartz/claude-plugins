#!/usr/bin/env python3
"""Rewrite lib/runtime-manifest.json, lib/runtime-requirements.txt and lib/runtime-id from PyPI for the pins below."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

BOOTSTRAP_VERSION = 1
PYTHON = "3.13"
UV = "0.12.22"
AST_GREP_PY = "0.45.3"

UV_WHEELS = {
    "darwin-arm64": "macosx_11_0_arm64",
    "darwin-x86_64": "macosx_10_12_x86_64",
    "linux-x86_64": "manylinux_2_17_x86_64.manylinux2014_x86_64",
    "linux-aarch64": "manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64",
}
AST_GREP_WHEELS = ("macosx_10_12_x86_64", "macosx_11_0_arm64", "manylinux_2_28_aarch64", "manylinux_2_28_x86_64")
LIB = Path(__file__).resolve().parent.parent / "lib"


def release(package: str, version: str) -> dict[str, dict[str, str]]:
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        files = json.load(response)["urls"]
    return {item["filename"]: item for item in files if item["packagetype"] == "bdist_wheel"}


def requirements(wheels: dict[str, dict[str, str]]) -> str:
    cp = PYTHON.replace(".", "")
    names = [f"ast_grep_py-{AST_GREP_PY}-cp{cp}-cp{cp}-{tag}.whl" for tag in AST_GREP_WHEELS]
    hashes = [f"    --hash=sha256:{wheels[name]['digests']['sha256']}" for name in names]
    return f"ast-grep-py=={AST_GREP_PY} \\\n" + " \\\n".join(hashes) + "\n"


def manifest(wheels: dict[str, dict[str, str]]) -> dict[str, object]:
    platforms = {}
    for key, tag in UV_WHEELS.items():
        name = f"uv-{UV}-py3-none-{tag}.whl"
        item = wheels[name]
        platforms[key] = {"url": item["url"], "sha256": item["digests"]["sha256"], "size": item["size"],
                          "member": f"uv-{UV}.data/scripts/uv"}
    return {"bootstrapVersion": BOOTSTRAP_VERSION, "python": PYTHON, "astGrepPy": AST_GREP_PY,
            "uv": {"version": UV, "wheels": platforms}}


def main() -> None:
    document = json.dumps(manifest(release("uv", UV)), indent=2, sort_keys=True) + "\n"
    pinned = requirements(release("ast-grep-py", AST_GREP_PY))
    digest = hashlib.sha256((document + pinned).encode()).hexdigest()[:12]
    (LIB / "runtime-manifest.json").write_text(document)
    (LIB / "runtime-requirements.txt").write_text(pinned)
    (LIB / "runtime-id").write_text(f"r{BOOTSTRAP_VERSION}-{digest}\n")


if __name__ == "__main__":
    main()
