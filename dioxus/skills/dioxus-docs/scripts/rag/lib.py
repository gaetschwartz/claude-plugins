"""Shared RAG utilities for the dioxus-docs skill.

Embedding backends:
  - Ollama:                HTTP API on localhost:11434 (or $OLLAMA_URL).
  - OpenAI:                /v1/embeddings against any OpenAI-compatible endpoint
                           (api.openai.com, Azure, OpenRouter, vLLM, llama.cpp).
  - sentence-transformers: pure-Python, downloads HF model on first use.

The active backend per-call is selected by `embed(model, texts, backend, plugin_root)`.
For Ollama specifically: if the user explicitly configured Ollama but it's
unreachable at call time, we fall back to sentence-transformers automatically
(rationale: Ollama is the "local default"; ST is its symmetric local fallback).
For OpenAI: no auto-fallback — the user opted into a remote API, errors are
explicit.

Vector store: ChromaDB persistent client at $PLUGIN_ROOT/.rag-index/.
State:        $PLUGIN_ROOT/.rag-state.json — `{config, books}`.
Secrets:      $PLUGIN_ROOT/.rag-config-secrets — gitignored, chmod 600.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Ollama tag → HuggingFace id, for the sentence-transformers fallback.
HF_ALIAS = {
    "qwen3-embedding:0.6b": "Qwen/Qwen3-Embedding-0.6B",
    "nomic-embed-text": "nomic-ai/nomic-embed-text-v1.5",
}


# --- config & secrets (mirror of rag/config.py — kept in sync) ------------

def _default_config() -> dict:
    return {
        "backend": "ollama",
        "model": "qwen3-embedding:0.6b",
        "openai_base_url": "https://api.openai.com/v1",
        "openai_api_key_env": "OPENAI_API_KEY",
    }


def load_config(plugin_root: Path) -> dict:
    state = load_state(plugin_root)
    cfg = _default_config()
    cfg.update(state.get("config", {}))
    return cfg


def save_config(plugin_root: Path, **updates) -> None:
    state = load_state(plugin_root)
    cfg = state.get("config") or _default_config()
    cfg.update({k: v for k, v in updates.items() if v is not None})
    state["config"] = cfg
    save_state(plugin_root, state)


def read_secrets(plugin_root: Path) -> dict:
    p = plugin_root / ".rag-config-secrets"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


# --- Ollama -----------------------------------------------------------------

def ollama_alive() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        return r.status_code == 200
    except requests.RequestException:
        return False


def ollama_pull(model: str) -> None:
    print(f"[rag] pulling model {model} via Ollama (may take a few minutes)…", file=sys.stderr)
    r = requests.post(f"{OLLAMA_URL}/api/pull", json={"name": model}, stream=True, timeout=None)
    r.raise_for_status()
    for line in r.iter_lines():
        if not line:
            continue
        try:
            payload = json.loads(line)
            status = payload.get("status")
            if status:
                print(f"[ollama] {status}", file=sys.stderr)
        except json.JSONDecodeError:
            pass


def ollama_embed(model: str, texts: list[str]) -> list[list[float]]:
    r = requests.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": model, "input": texts},
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["embeddings"]


# --- OpenAI / OpenAI-compatible ---------------------------------------------

def _openai_api_key(plugin_root: Path, cfg: dict) -> str:
    key_env = cfg.get("openai_api_key_env", "OPENAI_API_KEY")
    key = os.environ.get(key_env) or read_secrets(plugin_root).get(key_env)
    if not key:
        sys.exit(
            f"OpenAI backend selected but no API key found.\n"
            f"  Either: export {key_env}=sk-...\n"
            f"  Or run: /dioxus-docs rag config set-openai-key <KEY>"
        )
    return key


def openai_embed(model: str, texts: list[str], plugin_root: Path) -> list[list[float]]:
    cfg = load_config(plugin_root)
    base = cfg.get("openai_base_url", "https://api.openai.com/v1")
    key = _openai_api_key(plugin_root, cfg)
    r = requests.post(
        f"{base.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "input": texts},
        timeout=300,
    )
    if r.status_code != 200:
        sys.exit(f"OpenAI embed failed ({r.status_code}): {r.text[:500]}")
    body = r.json()
    return [item["embedding"] for item in body["data"]]


# --- sentence-transformers --------------------------------------------------

_st_cache: dict[str, object] = {}


def st_embed(model: str, texts: list[str]) -> list[list[float]]:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as e:
        sys.exit(
            f"sentence-transformers unavailable ({e}). "
            "Install Ollama (https://ollama.com), or `pip install sentence-transformers` in the venv."
        )
    if model not in _st_cache:
        hf_id = HF_ALIAS.get(model, model)
        print(f"[rag] loading sentence-transformer {hf_id}…", file=sys.stderr)
        _st_cache[model] = SentenceTransformer(hf_id, trust_remote_code=True)
    return _st_cache[model].encode(texts, convert_to_numpy=True).tolist()  # type: ignore[attr-defined]


# --- dispatch ---------------------------------------------------------------

def embed(model: str, texts: list[str], backend: str = "ollama", plugin_root: Path | None = None) -> list[list[float]]:
    """Embed `texts` with `model` using the named backend.

    Backends:
      - "ollama": HTTP to OLLAMA_URL. Auto-falls back to sentence-transformers
        when Ollama is unreachable (preserves the original Ollama+ST fallback
        UX even when the user has explicitly configured backend=ollama).
      - "openai": /v1/embeddings against `cfg.openai_base_url` with the API
        key from $cfg.openai_api_key_env (or the secrets file). No auto-fallback.
      - "sentence-transformers": local HF model in the plugin venv.
    """
    if backend == "ollama":
        if ollama_alive():
            return ollama_embed(model, texts)
        print(f"[rag] Ollama unreachable at {OLLAMA_URL}; falling back to sentence-transformers", file=sys.stderr)
        return st_embed(model, texts)
    if backend == "openai":
        if plugin_root is None:
            sys.exit("openai backend requires plugin_root (internal error: caller did not pass it)")
        return openai_embed(model, texts, plugin_root)
    if backend == "sentence-transformers":
        return st_embed(model, texts)
    sys.exit(f"unknown backend: {backend} (valid: ollama, openai, sentence-transformers)")


# --- chunking ---------------------------------------------------------------

def chunk_text(text: str, target_chars: int = 1500, overlap_lines: int = 4) -> list[tuple[int, str]]:
    """Split text into overlapping chunks, returning (start_line, chunk_text) tuples.

    Lines are 1-indexed. Chunks overlap by `overlap_lines` lines for context continuity.
    """
    lines = text.split("\n")
    n = len(lines)
    out: list[tuple[int, str]] = []
    cursor = 0
    while cursor < n:
        size = 0
        i = cursor
        while i < n and size < target_chars:
            size += len(lines[i]) + 1
            i += 1
        chunk = "\n".join(lines[cursor:i])
        if chunk.strip():
            out.append((cursor + 1, chunk))
        if i >= n:
            break
        cursor = max(cursor + 1, i - overlap_lines)
    return out


# --- vector store -----------------------------------------------------------

def chroma_client(plugin_root: Path):
    import chromadb  # lazy import: avoid penalty for non-RAG calls
    return chromadb.PersistentClient(path=str(plugin_root / ".rag-index"))


# --- state ------------------------------------------------------------------

def state_path(plugin_root: Path) -> Path:
    return plugin_root / ".rag-state.json"


def load_state(plugin_root: Path) -> dict:
    p = state_path(plugin_root)
    if p.exists():
        return json.loads(p.read_text())
    return {"books": {}}


def save_state(plugin_root: Path, state: dict) -> None:
    state_path(plugin_root).write_text(json.dumps(state, indent=2) + "\n")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
