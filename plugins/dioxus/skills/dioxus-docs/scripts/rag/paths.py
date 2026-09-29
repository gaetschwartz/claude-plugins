"""Shared location and naming constants for the RAG scripts (stdlib only)."""

import os
import sys
from pathlib import Path

PROG = "dioxus-docs"


def data_dir() -> Path:
    v = os.environ.get("DIOXUS_DATA")
    if not v:
        sys.exit(f"DIOXUS_DATA is not set; run RAG through `{PROG} rag ...`")
    return Path(v)
