#!/usr/bin/env python3
"""RAG configuration sub-commands — show / set-* / reset.

Pure stdlib (urllib, json, os, pathlib). Does NOT need the plugin's venv,
so it runs even before the user has set RAG up. The venv is only needed
for actually building/querying indexes (handled by rag_index.py / rag_query.py).

Output for `show` is markdown with an "Agent instructions" section that
tells the calling agent what to ask the user and how to interpret the
response. The shape of that section depends on the current state
(no books / config matches indexed books / config drifted, etc.).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


DEFAULTS_BY_BACKEND = {
    "ollama": {
        "model": "qwen3-embedding:0.6b",
        "alternatives": ["nomic-embed-text", "mxbai-embed-large"],
    },
    "openai": {
        "model": "text-embedding-3-small",
        "alternatives": ["text-embedding-3-large", "text-embedding-ada-002"],
    },
    "sentence-transformers": {
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "alternatives": ["nomic-ai/nomic-embed-text-v1.5", "BAAI/bge-large-en-v1.5"],
    },
}


def default_config() -> dict:
    return {
        "backend": "ollama",
        "model": DEFAULTS_BY_BACKEND["ollama"]["model"],
        "openai_base_url": "https://api.openai.com/v1",
        "openai_api_key_env": "OPENAI_API_KEY",
    }


# --- state I/O ---------------------------------------------------------------

def state_path(plugin_root: Path) -> Path:
    return plugin_root / ".rag-state.json"


def secrets_path(plugin_root: Path) -> Path:
    return plugin_root / ".rag-config-secrets"


def load_state(plugin_root: Path) -> dict:
    p = state_path(plugin_root)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_state(plugin_root: Path, state: dict) -> None:
    state_path(plugin_root).write_text(json.dumps(state, indent=2) + "\n")


def get_config(plugin_root: Path) -> dict:
    state = load_state(plugin_root)
    cfg = default_config()
    cfg.update(state.get("config", {}))
    return cfg


def set_config(plugin_root: Path, **updates) -> None:
    state = load_state(plugin_root)
    cfg = state.get("config") or default_config()
    cfg.update({k: v for k, v in updates.items() if v is not None})
    state["config"] = cfg
    save_state(plugin_root, state)


def read_secrets(plugin_root: Path) -> dict:
    p = secrets_path(plugin_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def write_secret(plugin_root: Path, key: str, value: str) -> None:
    p = secrets_path(plugin_root)
    secrets = read_secrets(plugin_root)
    secrets[key] = value
    p.write_text(json.dumps(secrets, indent=2) + "\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass


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


def probe_openai(plugin_root: Path, cfg: dict) -> tuple[bool, str]:
    key_env = cfg.get("openai_api_key_env", "OPENAI_API_KEY")
    if os.environ.get(key_env):
        return True, f"key set via env ${key_env}"
    if read_secrets(plugin_root).get(key_env):
        return True, f"key stored in .rag-config-secrets (.gitignored)"
    return False, f"no key (set ${key_env} or run `rag config set-openai-key <KEY>`)"


def probe_st(plugin_root: Path) -> tuple[bool, str]:
    venv = plugin_root / ".rag-venv"
    if not venv.exists():
        return False, "venv not set up yet (will be created on `rag enable`)"
    # Probe the venv's site-packages for sentence_transformers
    for sp in venv.glob("lib/python*/site-packages/sentence_transformers"):
        if sp.exists():
            return True, "installed in plugin venv"
    return False, "venv exists but sentence-transformers not installed"


# --- show command -----------------------------------------------------------

def render_show(plugin_root: Path) -> str:
    cfg = get_config(plugin_root)
    state = load_state(plugin_root)
    books = state.get("books", {})

    ollama_ok, ollama_msg = probe_ollama()
    openai_ok, openai_msg = probe_openai(plugin_root, cfg)
    st_ok, st_msg = probe_st(plugin_root)

    backend = cfg["backend"]
    model = cfg["model"]
    base = cfg["openai_base_url"]
    key_env = cfg["openai_api_key_env"]
    key_source = "env" if os.environ.get(key_env) else ("file" if read_secrets(plugin_root).get(key_env) else "none")

    lines = []
    lines.append("# RAG configuration")
    lines.append("")
    lines.append(f"- **backend**: `{backend}` (default for new indexes)")
    lines.append(f"- **model**:   `{model}`")
    lines.append(f"- **openai_base_url**: `{base}` (used only when backend=openai)")
    lines.append(f"- **openai_api_key**: source=`{key_source}`, env var=`${key_env}`")
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
    out = []

    if not books:
        # State 1: nothing indexed yet
        out.append("RAG is not set up. To enable, the user picks a backend and a book.")
        out.append("")
        out.append("If the user hasn't expressed a preference, ask them this verbatim:")
        out.append("")
        out.append("> Want to enable semantic search over the Dioxus docs? Three options:")
        out.append(">")
        out.append("> 1. **Ollama** (free, local) — needs `ollama serve` + `ollama pull <model>`")
        out.append("> 2. **OpenAI** (paid, $0.02/1M tokens for `text-embedding-3-small`) — needs an API key")
        out.append("> 3. **sentence-transformers** (free, local) — downloads ~1 GB into the plugin venv on first use")
        out.append(">")
        out.append("> Which would you like?")
        out.append("")
        out.append("Response handling:")
        out.append("")
        out.append("- **ollama** →")
        if ollama_ok:
            out.append("    `/dioxus-docs rag config set-backend ollama` then `/dioxus-docs rag enable docs`.")
        else:
            out.append("    Tell user to run `ollama serve` first. Then:")
            out.append("    `/dioxus-docs rag config set-backend ollama` then `/dioxus-docs rag enable docs`.")
        out.append("- **openai** →")
        out.append(f"    Check ${key_env}. If unset, ask: \"Set ${key_env} in your shell, or run `/dioxus-docs rag config set-openai-key <KEY>` to store it (gitignored).\"")
        out.append("    Ask: \"Which model? Default `text-embedding-3-small`. Alternatives: `text-embedding-3-large`.\"")
        out.append("    Ask if they need a custom base URL (Azure, OpenRouter, vLLM, llama.cpp, etc.).")
        out.append("    Run: `/dioxus-docs rag config set-backend openai`")
        out.append("    Then: `/dioxus-docs rag config set-model <model>`")
        out.append("    Then (if needed): `/dioxus-docs rag config set-openai-base <url>`")
        out.append("    Then: `/dioxus-docs rag enable docs`")
        out.append("- **sentence-transformers** →")
        out.append("    Confirm the ~1 GB torch+model download is acceptable.")
        out.append("    Run: `/dioxus-docs rag config set-backend sentence-transformers`")
        out.append("    Then: `/dioxus-docs rag enable docs`")
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
        out.append("- **add another book**: `/dioxus-docs rag enable src` or `… enable examples` (uses current config).")
        out.append("- **change backend or model**: `disable` each book → `set-backend` / `set-model` → re-`enable`.")
        out.append("  (Each book's embeddings are tied to its backend+model — dimensions don't match across.)")
        out.append("- **refresh content** (after vendor pull): `/dioxus-docs rag rebuild <book>`.")
        return out

    # State 3: drift — config doesn't match indexed books
    out.append("Current config doesn't match what some indexed books use:")
    for bk, b, m in drifted:
        out.append(f"- `{bk}` indexed with backend=`{b}` model=`{m}` (config wants `{backend}`/`{model}`)")
    out.append("")
    out.append("Queries still work against each book using its recorded backend+model (this is intentional).")
    out.append("")
    out.append("If the user wants to migrate `<book>` to the current config:")
    out.append("  `/dioxus-docs rag disable <book>` then `/dioxus-docs rag enable <book>`")
    return out


# --- set-* commands ---------------------------------------------------------

def cmd_set_backend(plugin_root: Path, name: str) -> None:
    if name not in DEFAULTS_BY_BACKEND:
        sys.exit(f"unknown backend: {name} (valid: {', '.join(DEFAULTS_BY_BACKEND)})")
    new_model = DEFAULTS_BY_BACKEND[name]["model"]
    set_config(plugin_root, backend=name, model=new_model)
    print(f"[rag-config] backend={name}, model={new_model} (default for {name}; override with set-model)", file=sys.stderr)


def cmd_set_model(plugin_root: Path, model: str) -> None:
    set_config(plugin_root, model=model)
    print(f"[rag-config] model={model}", file=sys.stderr)


def cmd_set_openai_base(plugin_root: Path, url: str) -> None:
    set_config(plugin_root, openai_base_url=url)
    print(f"[rag-config] openai_base_url={url}", file=sys.stderr)


def cmd_set_openai_key(plugin_root: Path, key: str) -> None:
    cfg = get_config(plugin_root)
    key_env = cfg.get("openai_api_key_env", "OPENAI_API_KEY")
    write_secret(plugin_root, key_env, key)
    print(f"[rag-config] stored ${key_env} in .rag-config-secrets (gitignored, chmod 600)", file=sys.stderr)


def cmd_reset(plugin_root: Path) -> None:
    set_config(plugin_root, **default_config())
    print("[rag-config] reset to defaults", file=sys.stderr)


# --- main -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(prog="rag_config_io.py")
    ap.add_argument("--plugin-root", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show")
    p = sub.add_parser("set-backend"); p.add_argument("name")
    p = sub.add_parser("set-model"); p.add_argument("model")
    p = sub.add_parser("set-openai-base"); p.add_argument("url")
    p = sub.add_parser("set-openai-key"); p.add_argument("key")
    sub.add_parser("reset")

    # Read-only accessors used by rag.sh to thread config values through.
    p = sub.add_parser("get"); p.add_argument("field")
    p = sub.add_parser("get-book"); p.add_argument("book"); p.add_argument("field")

    args = ap.parse_args()
    plugin_root = Path(args.plugin_root)

    if args.cmd == "show":
        print(render_show(plugin_root))
    elif args.cmd == "set-backend":
        cmd_set_backend(plugin_root, args.name)
    elif args.cmd == "set-model":
        cmd_set_model(plugin_root, args.model)
    elif args.cmd == "set-openai-base":
        cmd_set_openai_base(plugin_root, args.url)
    elif args.cmd == "set-openai-key":
        cmd_set_openai_key(plugin_root, args.key)
    elif args.cmd == "reset":
        cmd_reset(plugin_root)
    elif args.cmd == "get":
        print(get_config(plugin_root).get(args.field, ""))
    elif args.cmd == "get-book":
        state = load_state(plugin_root)
        info = state.get("books", {}).get(args.book, {})
        print(info.get(args.field, ""))


if __name__ == "__main__":
    main()
