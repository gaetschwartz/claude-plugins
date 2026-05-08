"""Shared RAG utilities for the dioxus-docs skill.

Embedding backends:
  - Ollama (preferred): HTTP API on localhost:11434.
  - sentence-transformers (fallback): pure-Python, downloads HF model on first use.

Vector store: ChromaDB persistent client at $PLUGIN_ROOT/.rag-index/.
State:        $PLUGIN_ROOT/.rag-state.json (which books are enabled, model used).
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


_st_cache: dict[str, object] = {}


def st_embed(model: str, texts: list[str]) -> list[list[float]]:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as e:
        sys.exit(
            f"sentence-transformers fallback unavailable ({e}). "
            "Install Ollama (https://ollama.com), or `pip install sentence-transformers` in the venv."
        )
    if model not in _st_cache:
        hf_id = HF_ALIAS.get(model, model)
        print(f"[rag] loading sentence-transformer {hf_id}…", file=sys.stderr)
        _st_cache[model] = SentenceTransformer(hf_id, trust_remote_code=True)
    return _st_cache[model].encode(texts, convert_to_numpy=True).tolist()  # type: ignore[attr-defined]


def embed(model: str, texts: list[str]) -> list[list[float]]:
    """Embed via Ollama if reachable, else fall back to sentence-transformers."""
    if ollama_alive():
        return ollama_embed(model, texts)
    return st_embed(model, texts)


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


def chroma_client(plugin_root: Path):
    import chromadb  # lazy import: avoid penalty for non-RAG calls
    return chromadb.PersistentClient(path=str(plugin_root / ".rag-index"))


def state_path(plugin_root: Path) -> Path:
    return plugin_root / ".rag-state.json"


def load_state(plugin_root: Path) -> dict:
    p = state_path(plugin_root)
    if p.exists():
        return json.loads(p.read_text())
    return {"books": {}}


def save_state(plugin_root: Path, state: dict) -> None:
    state_path(plugin_root).write_text(json.dumps(state, indent=2))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
