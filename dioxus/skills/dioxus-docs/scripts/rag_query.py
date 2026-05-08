#!/usr/bin/env python3
"""Query enabled RAG books and print top-k chunks.

Output: path:line<TAB>distance<TAB>snippet  (one per line)
This matches the conventions of `search.sh` so the agent can parse it the same way.
"""

import argparse
import sys
from pathlib import Path

from rag_lib import chroma_client, embed, load_state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin-root", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--book", default="all")
    ap.add_argument("--top-k", type=int, default=8)
    args = ap.parse_args()

    plugin_root = Path(args.plugin_root)
    state = load_state(plugin_root)
    books_state = state.get("books", {})
    if not books_state:
        sys.exit("no books indexed. user can run: /dioxus-docs rag enable <book>")

    if args.book == "all":
        books = list(books_state.keys())
    else:
        if args.book not in books_state:
            sys.exit(f"book '{args.book}' is not enabled. enabled: {list(books_state.keys())}")
        books = [args.book]

    client = chroma_client(plugin_root)
    pooled: list[dict] = []

    for book in books:
        model = books_state[book]["model"]
        try:
            coll = client.get_collection(f"book_{book}")
        except Exception as e:
            print(f"[rag-query] WARN: skipping {book}: {e}", file=sys.stderr)
            continue
        q_emb = embed(model, [args.query])[0]
        res = coll.query(query_embeddings=[q_emb], n_results=args.top_k)
        for i in range(len(res["ids"][0])):
            pooled.append({
                "path": res["metadatas"][0][i].get("path", "?"),
                "line": res["metadatas"][0][i].get("line", "?"),
                "distance": res["distances"][0][i] if res.get("distances") else None,
                "doc": res["documents"][0][i],
                "book": book,
            })

    pooled.sort(key=lambda r: r["distance"] if r["distance"] is not None else 0.0)
    pooled = pooled[:args.top_k]

    for r in pooled:
        snippet = r["doc"][:300].replace("\n", " ").replace("\t", " ")
        score = f"{r['distance']:.4f}" if r["distance"] is not None else "?"
        print(f"{r['path']}:{r['line']}\t{score}\t{snippet}…")


if __name__ == "__main__":
    main()
