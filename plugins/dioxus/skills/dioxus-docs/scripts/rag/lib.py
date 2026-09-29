"""Shared RAG utilities for the dioxus-docs skill.

Embedding backends:
  - Ollama:                HTTP API on localhost:11434 (or $OLLAMA_URL).
  - OpenAI:                /v1/embeddings against any OpenAI-compatible endpoint
                           (api.openai.com, Azure, OpenRouter, vLLM, llama.cpp).
  - sentence-transformers: pure-Python, downloads HF model on first use.

The backend is selected per call by `embed(...)`, which reports the backend and
model that actually produced the vectors. Only indexing may fall back from an
unreachable Ollama to an already-installed sentence-transformers; querying
never switches, because vectors from different backends are not comparable.

Vector store: ChromaDB persistent client at $DIOXUS_DATA/.rag-index/.
Config, state and secrets live in state.py.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

import requests

from paths import PROG
from state import load_config, read_secrets

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Ollama tag → HuggingFace id, for the sentence-transformers fallback.
HF_ALIAS = {
    "qwen3-embedding:0.6b": "Qwen/Qwen3-Embedding-0.6B",
    "nomic-embed-text": "nomic-ai/nomic-embed-text-v1.5",
}


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

def _openai_api_key(data: Path, cfg: dict) -> str:
    key_env = cfg.get("openai_api_key_env", "OPENAI_API_KEY")
    key = os.environ.get(key_env) or read_secrets(data).get(key_env)
    if not key:
        sys.exit(
            f"OpenAI backend selected but no API key found.\n"
            f"  Either: export {key_env}=sk-...\n"
            f"  Or pipe it in: printf %s \"$KEY\" | {PROG} rag config set-openai-key"
        )
    return key


def openai_embed(model: str, texts: list[str], data: Path) -> list[list[float]]:
    cfg = load_config(data)
    base = cfg.get("openai_base_url", "https://api.openai.com/v1")
    key = _openai_api_key(data, cfg)
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

_st_cache: dict[tuple[str, bool], object] = {}


def st_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401  # type: ignore
    except ImportError:
        return False
    return True


def st_embed(model: str, texts: list[str], trust_remote_code: bool) -> list[list[float]]:
    if not st_available():
        sys.exit(
            "sentence-transformers is not installed in the RAG venv. "
            f"Run `{PROG} rag enable <book> --backend=sentence-transformers` (installs it, ~1 GB)."
        )
    from sentence_transformers import SentenceTransformer  # type: ignore
    key = (model, trust_remote_code)
    if key not in _st_cache:
        print(f"[rag] loading sentence-transformer {model}…", file=sys.stderr)
        _st_cache[key] = SentenceTransformer(model, trust_remote_code=trust_remote_code)
    return _st_cache[key].encode(texts, convert_to_numpy=True).tolist()  # type: ignore[attr-defined]


# --- dispatch ---------------------------------------------------------------

class Embedded(NamedTuple):
    vectors: list[list[float]]
    backend: str
    model: str


def embed(
    model: str,
    texts: list[str],
    backend: str,
    data: Path,
    allow_fallback: bool = False,
) -> Embedded:
    """Embed `texts`, returning the vectors plus the backend and model actually used.

    `allow_fallback` lets an unreachable Ollama degrade to an installed
    sentence-transformers (indexing only).
    """
    if backend == "ollama":
        if ollama_alive():
            return Embedded(ollama_embed(model, texts), "ollama", model)
        if not allow_fallback:
            sys.exit(f"Ollama is unreachable at {OLLAMA_URL}; start it with `ollama serve` (this index was built with it).")
        hf_id = HF_ALIAS.get(model)
        if hf_id is None or not st_available():
            sys.exit(
                f"Ollama is unreachable at {OLLAMA_URL} and no sentence-transformers fallback is available for '{model}'. "
                f"Start `ollama serve`, or run `{PROG} rag enable <book> --backend=sentence-transformers`."
            )
        print(f"[rag] Ollama unreachable at {OLLAMA_URL}; using sentence-transformers {hf_id}", file=sys.stderr)
        vectors = st_embed(hf_id, texts, load_config(data)["trust_remote_code"])
        return Embedded(vectors, "sentence-transformers", hf_id)
    if backend == "openai":
        return Embedded(openai_embed(model, texts, data), "openai", model)
    if backend == "sentence-transformers":
        return Embedded(st_embed(model, texts, load_config(data)["trust_remote_code"]), backend, model)
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

def chroma_client(data: Path):
    import chromadb  # lazy import: avoid penalty for non-RAG calls
    return chromadb.PersistentClient(path=str(data / ".rag-index"))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
