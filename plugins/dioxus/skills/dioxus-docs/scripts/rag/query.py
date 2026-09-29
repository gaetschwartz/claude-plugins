#!/usr/bin/env python3
"""Query enabled RAG books and print top-k chunks.

Output: path:line<TAB>distance<TAB>snippet  (one per line)
This matches the path:line convention of `search` so the agent can parse it the same way.
"""

import argparse
import sys

from lib import chroma_client, embed
from paths import PROG, data_dir
from state import load_state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--book", default="all")
    ap.add_argument("--top-k", type=int, default=8)
    args = ap.parse_args()

    data = data_dir()
    state = load_state(data)
    books_state = state["books"]
    if not books_state:
        sys.exit(f"no books indexed; the user can enable one with: {PROG} rag enable <book>")

    if args.book == "all":
        books = list(books_state.keys())
    else:
        if args.book not in books_state:
            sys.exit(f"book '{args.book}' is not enabled. enabled: {list(books_state.keys())}")
        books = [args.book]

    spaces = {(books_state[b]["backend"], books_state[b]["model"]) for b in books}
    mixed = len(spaces) > 1
    if mixed:
        print(
            "[rag-query] WARN: books were indexed with different embedding models "
            f"({', '.join(f'{b}/{m}' for b, m in sorted(spaces))}); distances are not comparable across books, "
            "so results are listed per book",
            file=sys.stderr,
        )

    client = chroma_client(data)
    pooled: list[dict] = []

    for book in books:
        info = books_state[book]
        model = info["model"]
        backend = info["backend"]
        try:
            coll = client.get_collection(f"book_{book}")
        except Exception as e:
            print(f"[rag-query] WARN: skipping {book}: {e}", file=sys.stderr)
            continue
        q_emb = embed(model, [args.query], backend=backend, data=data).vectors[0]
        res = coll.query(query_embeddings=[q_emb], n_results=args.top_k)
        for i in range(len(res["ids"][0])):
            pooled.append({
                "path": res["metadatas"][0][i].get("path", "?"),
                "line": res["metadatas"][0][i].get("line", "?"),
                "distance": res["distances"][0][i] if res.get("distances") else None,
                "doc": res["documents"][0][i],
                "book": book,
            })

    def by_distance(r: dict) -> float:
        return r["distance"] if r["distance"] is not None else 0.0

    if mixed:
        per_book = []
        for book in books:
            per_book.extend(sorted((r for r in pooled if r["book"] == book), key=by_distance)[:args.top_k])
        pooled = per_book
    else:
        pooled = sorted(pooled, key=by_distance)[:args.top_k]

    for r in pooled:
        snippet = r["doc"][:300].replace("\n", " ").replace("\t", " ")
        score = f"{r['distance']:.4f}" if r["distance"] is not None else "?"
        print(f"{r['path']}:{r['line']}\t{score}\t{snippet}…")


if __name__ == "__main__":
    main()
