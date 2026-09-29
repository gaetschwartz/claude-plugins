#!/usr/bin/env python3
"""RAG configuration sub-commands — show / set-* / reset.

Pure stdlib (urllib, json, os, pathlib). Does NOT need the plugin's venv,
so it runs even before the user has set RAG up. The venv is only needed
for actually building/querying indexes (handled by index.py / query.py).

Output for `show` is markdown with an "Agent instructions" section that
tells the calling agent what to ask the user and how to interpret the
response. The shape of that section depends on the current state
(no books / config matches indexed books / config drifted, etc.).
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from paths import PROG, data_dir
from state import (
    DEFAULTS_BY_BACKEND, default_config, load_config, load_state, read_secrets,
    save_config, write_secret,
)


# --- backend readiness probes -----------------------------------------------

def probe_ollama(url: str = "http://localhost:11434") -> tuple[bool, str]:
    try:
        req = urllib.request.Request(f"{url}/api/tags")
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                body = json.loads(r.read())
                names = sorted(m.get("name", "?") for m in body.get("models", []))
                if names:
                    return True, f"running, models pulled: {', '.join(names[:6])}{' …' if len(names) > 6 else ''}"
                return True, "running (no models pulled yet)"
            return False, f"HTTP {r.status}"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return False, f"unreachable ({type(e).__name__})"


def probe_openai(data: Path, cfg: dict) -> tuple[bool, str]:
    key_env = cfg.get("openai_api_key_env", "OPENAI_API_KEY")
    if os.environ.get(key_env):
        return True, f"key set via env ${key_env}"
    if read_secrets(data).get(key_env):
        return True, "key stored in .rag-config-secrets (chmod 600)"
    return False, f"no key (export ${key_env}, or pipe a key into `{PROG} rag config set-openai-key`)"


def probe_st(data: Path) -> tuple[bool, str]:
    venv = data / ".rag-venv"
    if not venv.exists():
        return False, "venv not set up yet (will be created on `rag enable`)"
    # Probe the venv's site-packages for sentence_transformers
    for sp in venv.glob("lib/python*/site-packages/sentence_transformers"):
        if sp.exists():
            return True, "installed in plugin venv"
    return False, "venv exists but sentence-transformers not installed"


# --- show command -----------------------------------------------------------

def render_show(data: Path) -> str:
    cfg = load_config(data)
    state = load_state(data)
    books = state.get("books", {})

    ollama_ok, ollama_msg = probe_ollama()
    openai_ok, openai_msg = probe_openai(data, cfg)
    st_ok, st_msg = probe_st(data)

    backend = cfg["backend"]
    model = cfg["model"]
    base = cfg["openai_base_url"]
    key_env = cfg["openai_api_key_env"]
    key_source = "env" if os.environ.get(key_env) else ("file" if read_secrets(data).get(key_env) else "none")

    lines = []
    lines.append("# RAG configuration")
    lines.append("")
    lines.append(f"- **backend**: `{backend}` (default for new indexes)")
    lines.append(f"- **model**:   `{model}`")
    lines.append(f"- **openai_base_url**: `{base}` (used only when backend=openai)")
    lines.append(f"- **openai_api_key**: source=`{key_source}`, env var=`${key_env}`")
    lines.append(f"- **trust_remote_code**: `{str(cfg['trust_remote_code']).lower()}` (sentence-transformers only; some models need it, it runs code from the model repo)")
    lines.append("")
    lines.append("## Backend readiness")
    lines.append("")
    lines.append(f"- **ollama**:                {'OK ' if ollama_ok else 'NOT READY '} — {ollama_msg}")
    lines.append(f"- **openai**:                {'OK ' if openai_ok else 'NOT READY '} — {openai_msg}")
    lines.append(f"- **sentence-transformers**: {'OK ' if st_ok else 'NOT READY '} — {st_msg}")
    lines.append("")
    lines.append("## Indexed books")
    lines.append("")
    if not books:
        lines.append("_no books indexed yet_")
    else:
        for book, info in books.items():
            b = info.get("backend", "ollama")
            m = info.get("model", "?")
            chunks = info.get("chunk_count", "?")
            at = info.get("indexed_at", "?")
            drift = "" if (b == backend and m == model) else " (config drifted from indexed)"
            lines.append(f"- **{book}**: backend=`{b}`, model=`{m}`, chunks={chunks}, indexed_at={at}{drift}")
    lines.append("")

    # --- Agent instructions block: state-dependent ---
    lines.append("## Agent instructions")
    lines.append("")
    lines.extend(_agent_instructions(
        backend=backend, model=model,
        ollama_ok=ollama_ok, openai_ok=openai_ok, st_ok=st_ok,
        books=books, key_env=key_env,
    ))
    lines.append("")
    lines.append("## Raw state")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({"config": cfg, "books": books}, indent=2))
    lines.append("```")
    return "\n".join(lines)


def _agent_instructions(*, backend: str, model: str, ollama_ok: bool, openai_ok: bool, st_ok: bool, books: dict, key_env: str) -> list[str]:
    """Render the state-dependent guidance block.

    Policy: **the agent drives the conversation and runs the commands itself.**
    Ask the user for inputs (backend choice, model, base URL); then run
    `rag config set-*` and `rag enable` directly via Bash. Only run `set-*`
    with values the user explicitly provided in this conversation; never
    fabricate models / URLs. API keys never go on a command line. Confirm
    intent before destructive operations (`rag disable`, `rag enable --force`).
    """
    out = []

    if not books:
        # State 1: nothing indexed yet
        out.append("**RAG is not set up. You drive setup; the user provides inputs.**")
        out.append("")
        out.append("Ask the user this verbatim:")
        out.append("")
        out.append("> Want to enable semantic search over the Dioxus docs? Three options:")
        out.append(">")
        out.append("> 1. **Ollama** (free, local) — needs `ollama serve` + `ollama pull <model>`")
        out.append("> 2. **OpenAI** (paid, $0.02/1M tokens for `text-embedding-3-small`) — needs an API key")
        out.append("> 3. **sentence-transformers** (free, local) — downloads ~1 GB into the plugin venv on first use")
        out.append(">")
        out.append("> Which would you like?")
        out.append("")
        out.append("Then gather inputs and run the commands yourself (do not hand them back to the user):")
        out.append("")
        out.append("- **ollama** →")
        if ollama_ok:
            out.append(f"    Run: `{PROG} rag config set-backend ollama`")
            out.append(f"    Run: `{PROG} rag enable docs`")
            out.append("    Report when indexing finishes.")
        else:
            out.append("    Ollama is unreachable. Ask the user: \"Please start `ollama serve` in another terminal, then say 'ready'.\"")
            out.append("    Wait for their confirmation, then:")
            out.append(f"    Run: `{PROG} rag config set-backend ollama`")
            out.append(f"    Run: `{PROG} rag enable docs`")
            out.append("    Report when indexing finishes.")
        out.append("- **openai** →")
        out.append(f"    Key: if ${key_env} is not in their environment, ask them to export it and restart, or to run `{PROG} rag config set-openai-key` themselves in a terminal (it prompts for the key). Never ask them to paste the key into the chat.")
        out.append("    Ask the user for:")
        out.append("    (a) model — default `text-embedding-3-small`, alternative `text-embedding-3-large`;")
        out.append("    (b) custom base URL — only if they're using Azure / OpenRouter / vLLM / llama.cpp etc.")
        out.append("    Then run, in order, with the values they gave you (the key must already be available as described above):")
        out.append(f"    `{PROG} rag config set-backend openai`")
        out.append(f"    `{PROG} rag config set-model <model>`        (skip if default)")
        out.append(f"    `{PROG} rag config set-openai-base <url>`    (skip if default)")
        out.append(f"    `{PROG} rag enable docs`")
        out.append("    Report when indexing finishes.")
        out.append("- **sentence-transformers** →")
        out.append("    Confirm the ~1 GB torch+model download is acceptable (`enable` installs sentence-transformers into the plugin venv on first use). Then run:")
        out.append(f"    `{PROG} rag config set-backend sentence-transformers`")
        out.append(f"    `{PROG} rag enable docs`")
        out.append("    Report when indexing finishes.")
        out.append("")
        out.append("**Rules**:")
        out.append("- Only run `set-*` with values the user explicitly gave you in this conversation. No fabricated keys, URLs, or model names.")
        out.append("- State each command before running it (so the user can audit), but don't ask permission for each one once they've consented to the backend.")
        return out

    # State 2: at least one book indexed
    drifted = []
    for bk, info in books.items():
        b = info.get("backend", "ollama")
        m = info.get("model", "?")
        if b != backend or m != model:
            drifted.append((bk, b, m))

    if not drifted:
        out.append(f"RAG is configured and working: backend=`{backend}`, model=`{model}`.")
        out.append("")
        out.append("If the user wants to:")
        out.append(f"- **add another book** (`src` / `examples`): ask which, then run `{PROG} rag enable <book>` yourself.")
        out.append("- **change backend or model**: ask for the new values, confirm migration intent, then run `disable` + `set-*` + `enable` yourself.")
        out.append("  (Each book's embeddings are tied to its recorded backend+model — dimensions don't match across.)")
        out.append(f"- **refresh content** (after `{PROG} update`): confirm intent, then run `{PROG} rag enable <book> --force` yourself (reuses the recorded backend and model).")
        return out

    # State 3: drift — config doesn't match indexed books
    out.append("Current config doesn't match what some indexed books use:")
    for bk, b, m in drifted:
        out.append(f"- `{bk}` indexed with backend=`{b}` model=`{m}` (config wants `{backend}`/`{model}`)")
    out.append("")
    out.append("Queries still work against each book using its recorded backend+model (this is intentional).")
    out.append("")
    out.append("If the user wants to migrate `<book>` to the current config, confirm intent then run yourself:")
    out.append(f"  `{PROG} rag disable <book>` then `{PROG} rag enable <book>`")
    return out


# --- set-* commands ---------------------------------------------------------

def cmd_set_backend(data: Path, name: str) -> None:
    if name not in DEFAULTS_BY_BACKEND:
        sys.exit(f"unknown backend: {name} (valid: {', '.join(DEFAULTS_BY_BACKEND)})")
    new_model = DEFAULTS_BY_BACKEND[name]["model"]
    save_config(data, backend=name, model=new_model)
    print(f"[rag-config] backend={name}, model={new_model} (default for {name}; override with set-model)", file=sys.stderr)


def cmd_set_model(data: Path, model: str) -> None:
    save_config(data, model=model)
    print(f"[rag-config] model={model}", file=sys.stderr)


def cmd_set_openai_base(data: Path, url: str) -> None:
    save_config(data, openai_base_url=url)
    print(f"[rag-config] openai_base_url={url}", file=sys.stderr)


def cmd_set_trust_remote_code(data: Path, value: str) -> None:
    if value not in ("on", "off"):
        sys.exit("set-trust-remote-code takes 'on' or 'off'")
    save_config(data, trust_remote_code=(value == "on"))
    print(f"[rag-config] trust_remote_code={value}", file=sys.stderr)


def read_key_input(key_env: str) -> str:
    if not sys.stdin.isatty():
        key = sys.stdin.read().strip()
        if key:
            return key
    else:
        key = getpass.getpass(f"{key_env}: ").strip()
        if key:
            return key
    key = os.environ.get(key_env, "").strip()
    if key:
        return key
    sys.exit(f"no key provided: pipe it on stdin or export {key_env}")


def cmd_set_openai_key(data: Path, argv_key: str | None) -> None:
    if argv_key is not None:
        sys.exit(
            "refusing a key on the command line (it would end up in shell history and the transcript); "
            f"pipe it on stdin instead: printf %s \"$KEY\" | {PROG} rag config set-openai-key"
        )
    key_env = load_config(data).get("openai_api_key_env", "OPENAI_API_KEY")
    write_secret(data, key_env, read_key_input(key_env))
    print(f"[rag-config] stored ${key_env} in .rag-config-secrets (chmod 600)", file=sys.stderr)


def cmd_reset(data: Path) -> None:
    save_config(data, **default_config())
    print("[rag-config] reset to defaults", file=sys.stderr)


# --- main -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(prog="rag/config.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show")
    p = sub.add_parser("set-backend"); p.add_argument("name")
    p = sub.add_parser("set-model"); p.add_argument("model")
    p = sub.add_parser("set-openai-base"); p.add_argument("url")
    p = sub.add_parser("set-trust-remote-code"); p.add_argument("value")
    p = sub.add_parser("set-openai-key"); p.add_argument("key", nargs="?")
    sub.add_parser("reset")

    # Read-only accessors used by rag.sh.
    p = sub.add_parser("get"); p.add_argument("field")
    p = sub.add_parser("get-book"); p.add_argument("book"); p.add_argument("field")

    args = ap.parse_args()
    data = data_dir()

    if args.cmd == "show":
        print(render_show(data))
    elif args.cmd == "set-backend":
        cmd_set_backend(data, args.name)
    elif args.cmd == "set-model":
        cmd_set_model(data, args.model)
    elif args.cmd == "set-openai-base":
        cmd_set_openai_base(data, args.url)
    elif args.cmd == "set-trust-remote-code":
        cmd_set_trust_remote_code(data, args.value)
    elif args.cmd == "set-openai-key":
        cmd_set_openai_key(data, args.key)
    elif args.cmd == "reset":
        cmd_reset(data)
    elif args.cmd == "get":
        print(load_config(data).get(args.field, ""))
    elif args.cmd == "get-book":
        state = load_state(data)
        info = state.get("books", {}).get(args.book, {})
        print(info.get(args.field, ""))


if __name__ == "__main__":
    main()
