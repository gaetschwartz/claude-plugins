"""RAG config, state and secrets storage (stdlib only, shared by every RAG script)."""

import json
import os
import sys
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
        "trust_remote_code": False,
    }


def state_path(data: Path) -> Path:
    return data / ".rag-state.json"


def secrets_path(data: Path) -> Path:
    return data / ".rag-config-secrets"


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _load_json(path: Path, what: str) -> dict:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"{what} at {path} is not valid JSON ({e}); fix or delete it")


def load_state(data: Path) -> dict:
    p = state_path(data)
    state = _load_json(p, "RAG state") if p.exists() else {}
    state.setdefault("books", {})
    return state


def save_state(data: Path, state: dict) -> None:
    atomic_write(state_path(data), json.dumps(state, indent=2) + "\n")


def load_config(data: Path) -> dict:
    cfg = default_config()
    cfg.update(load_state(data).get("config", {}))
    return cfg


def save_config(data: Path, **updates) -> None:
    state = load_state(data)
    cfg = state.get("config") or default_config()
    cfg.update({k: v for k, v in updates.items() if v is not None})
    state["config"] = cfg
    save_state(data, state)


def read_secrets(data: Path) -> dict:
    p = secrets_path(data)
    return _load_json(p, "RAG secrets") if p.exists() else {}


def write_secret(data: Path, key: str, value: str) -> None:
    secrets = read_secrets(data)
    secrets[key] = value
    atomic_write(secrets_path(data), json.dumps(secrets, indent=2) + "\n", mode=0o600)
