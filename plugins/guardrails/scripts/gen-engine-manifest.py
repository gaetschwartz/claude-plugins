#!/usr/bin/env python3
"""Rewrite lib/engine-manifest.json for the ast-grep version pinned in package.json (run after bumping the pin and
regenerating package-lock.json). Downloads every wheel and npm tarball once to hash the binary each contains."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
OUT = os.path.join(ROOT, "lib", "engine-manifest.json")
WHEELS = {
    "darwin-universal2": "macosx_10_12_universal2",
    "linux-x86_64-gnu": "manylinux_2_28_x86_64",
    "linux-aarch64-gnu": "manylinux_2_28_aarch64",
}
NPM = ("darwin-arm64", "darwin-x64", "linux-x64-gnu", "linux-arm64-gnu")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def pinned() -> str:
    with open(os.path.join(ROOT, "package.json")) as fh:
        return json.load(fh)["dependencies"]["@ast-grep/cli"]


def wheel_entry(version: str, files: list[dict]) -> dict[str, dict]:
    out = {}
    for key, tag in WHEELS.items():
        found = [f for f in files if f["packagetype"] == "bdist_wheel" and f["filename"].endswith(f"{tag}.whl")
                 and "-py3-none-" in f["filename"]]
        if len(found) != 1:
            sys.exit(f"expected exactly one {key} wheel for {version}, found {len(found)}")
        info = found[0]
        data = fetch(info["url"])
        if hashlib.sha256(data).hexdigest() != info["digests"]["sha256"]:
            sys.exit(f"{info['filename']}: sha256 differs from PyPI's listing")
        member = f"ast_grep_cli-{version}.data/scripts/ast-grep"
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            binary = zf.read(member)
        out[key] = {"filename": info["filename"], "url": info["url"], "sha256": info["digests"]["sha256"],
                    "size": info["size"], "member": member, "binarySha256": hashlib.sha256(binary).hexdigest()}
    return out


def npm_entry(version: str) -> dict[str, dict]:
    with open(os.path.join(ROOT, "package-lock.json")) as fh:
        packages = json.load(fh)["packages"]
    out = {}
    for key in NPM:
        name = f"@ast-grep/cli-{key}"
        locked = packages[f"node_modules/{name}"]
        if locked["version"] != version:
            sys.exit(f"{name}: package-lock.json has {locked['version']}, expected {version}")
        data = fetch(locked["resolved"])
        algorithm, _, digest = locked["integrity"].partition("-")
        if base64.b64encode(hashlib.new(algorithm, data).digest()).decode() != digest:
            sys.exit(f"{name}: tarball differs from package-lock.json's integrity")
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            member = tf.extractfile("package/ast-grep")
            binary = member.read() if member else b""
        out[key] = {"package": name, "binarySha256": hashlib.sha256(binary).hexdigest()}
    return out


def main() -> int:
    version = pinned()
    files = json.loads(fetch(f"https://pypi.org/pypi/ast-grep-cli/{version}/json"))["urls"]
    manifest = {"version": version, "wheels": wheel_entry(version, files), "npm": npm_entry(version)}
    with open(OUT, "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print(f"ast-grep {version}: {len(manifest['wheels'])} wheels, {len(manifest['npm'])} npm binaries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
