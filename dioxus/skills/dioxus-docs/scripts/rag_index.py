#!/usr/bin/env python3
"""Build, rebuild, disable, or report status on the dioxus-docs RAG indexes.

Invoked from rag.sh. Books map to source subtrees:
  docs     → vendor/docsite/docs-src/0.7/src/   (.md)
  src      → vendor/dioxus/packages/             (.rs)
  examples → vendor/dioxus/examples/             (.rs)
"""

import argparse
import sys
from pathlib import Path

from rag_lib import (
    chroma_client, embed, load_state, save_state, ollama_alive, ollama_pull,
    chunk_text, now_iso,
)

BOOK_PATTERNS: dict[str, tuple[str, ...]] = {
    "docs": ("**/*.md",),
    "src": ("**/*.rs",),
    "examples": ("**/*.rs",),
}

MAX_FILE_BYTES = 200_000  # skip huge files (lockfiles, generated code)
EMBED_BATCH = 32


def index_book(book: str, source_dir: Path, plugin_root: Path, model: str) -> None:
    if not source_dir.is_dir():
        sys.exit(f"source dir does not exist: {source_dir}")
    if book not in BOOK_PATTERNS:
        sys.exit(f"unknown book: {book} (valid: {', '.join(BOOK_PATTERNS)})")

    if ollama_alive():
        ollama_pull(model)
    else:
        print(f"[rag-index] Ollama unreachable — using sentence-transformers fallback for {model}", file=sys.stderr)

    client = chroma_client(plugin_root)
    coll_name = f"book_{book}"
    try:
        client.delete_collection(coll_name)
    except Exception:
        pass
    coll = client.create_collection(coll_name, metadata={"book": book, "model": model})

    files: list[Path] = []
    for pattern in BOOK_PATTERNS[book]:
        files.extend(source_dir.rglob(pattern))
    files = [f for f in files if f.is_file() and f.stat().st_size < MAX_FILE_BYTES]
    print(f"[rag-index] {book}: {len(files)} files to process from {source_dir}", file=sys.stderr)

    docs: list[str] = []
    ids: list[str] = []
    metas: list[dict] = []
    plugin_root_str = str(plugin_root)

    for f in files:
        try:
            text = f.read_text(errors="replace")
        except Exception as e:
            print(f"[rag-index] skip {f}: {e}", file=sys.stderr)
            continue
        f_str = str(f)
        rel = str(f.relative_to(plugin_root)) if f_str.startswith(plugin_root_str) else f_str
        for line, chunk in chunk_text(text):
            ids.append(f"{rel}:{line}:{len(docs)}")
            docs.append(chunk)
            metas.append({"path": rel, "line": line, "book": book})

    if not docs:
        sys.exit(f"no chunks produced from {source_dir} — check the book's source pattern")

    print(f"[rag-index] embedding {len(docs)} chunks with model={model}", file=sys.stderr)
    for i in range(0, len(docs), EMBED_BATCH):
        batch_docs = docs[i:i + EMBED_BATCH]
        embs = embed(model, batch_docs)
        coll.add(
            ids=ids[i:i + EMBED_BATCH],
            documents=batch_docs,
            embeddings=embs,
            metadatas=metas[i:i + EMBED_BATCH],
        )
        done = min(i + EMBED_BATCH, len(docs))
        print(f"[rag-index] {done}/{len(docs)}", file=sys.stderr)

    state = load_state(plugin_root)
    state.setdefault("books", {})[book] = {
        "model": model,
        "indexed_at": now_iso(),
        "chunk_count": len(docs),
        "file_count": len(files),
        "source_dir": str(source_dir.relative_to(plugin_root)) if source_dir.is_relative_to(plugin_root) else str(source_dir),
    }
    save_state(plugin_root, state)
    print(f"[rag-index] done: {book} → {len(docs)} chunks indexed", file=sys.stderr)


def disable_book(book: str, plugin_root: Path) -> None:
    client = chroma_client(plugin_root)
    try:
        client.delete_collection(f"book_{book}")
        print(f"[rag-index] dropped collection for {book}", file=sys.stderr)
    except Exception as e:
        print(f"[rag-index] no collection to drop for {book}: {e}", file=sys.stderr)
    state = load_state(plugin_root)
    state.get("books", {}).pop(book, None)
    save_state(plugin_root, state)


def status(plugin_root: Path) -> None:
    state = load_state(plugin_root)
    books = state.get("books", {})
    if not books:
        print("[rag] no books indexed.")
        return
    print("[rag] enabled books:")
    for book, info in books.items():
        print(
            f"  - {book}: model={info.get('model', '?')}, "
            f"chunks={info.get('chunk_count', '?')}, "
            f"files={info.get('file_count', '?')}, "
            f"indexed_at={info.get('indexed_at', '?')}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin-root", required=True)
    ap.add_argument("--action", default="index", choices=["index", "rebuild", "disable", "status"])
    ap.add_argument("--book")
    ap.add_argument("--source-dir")
    ap.add_argument("--model", default="qwen3-embedding:0.6b")
    args = ap.parse_args()

    plugin_root = Path(args.plugin_root)

    if args.action == "status":
        status(plugin_root)
    elif args.action == "disable":
        if not args.book:
            sys.exit("--book required for disable")
        disable_book(args.book, plugin_root)
    else:
        if not args.book or not args.source_dir:
            sys.exit("--book and --source-dir required for index/rebuild")
        index_book(args.book, Path(args.source_dir), plugin_root, args.model)


if __name__ == "__main__":
    main()
