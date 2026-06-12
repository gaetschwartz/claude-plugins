# P4 knowledge manual

Operational guide for grounding every P4 answer in the corpus. You loaded this
because the environment is initialized. Use the `mcp__p4-knowledge-rag__*` tools
directly from here on — this manual tells you how.

## Sources, ordered by trust

Prefer the higher-trust source when they conflict; surface conflicts, don't
paper over them.

1. **The local knowledge-rag corpus (PRIMARY — REQUIRED).** Bundled in this
   plugin under `knowledge-rag/documents/`, queried via the
   `mcp__p4-knowledge-rag__*` tools. Spec/guides are authoritative for *what
   should happen*.
2. **The user's checked-out source.** `/Users/gaetan/dev/p4/open-p4studio`
   (bf-sde / open-p4studio) and `/Volumes/T7/dev/p4/` (other P4 projects, incl.
   `magnetite`). Read with `Grep`/`Glob`/`Read` for driver internals, p4c passes,
   SDK behaviour not in the corpus. Source is authoritative for *what the code
   currently does*.
3. **External fallback.** `WebFetch`/`WebSearch` for material absent from corpus
   and disk: spec errata, p4lang issues, ONOS/Stratum docs, papers. Lowest
   trust; cite the URL.

## Corpus contents

| Category | Documents |
|---|---|
| `language` | P4_16 spec v1.2.5, P4 cheat sheet |
| `architecture` | P4_16 Tofino Native Architecture (TNA) Application Note |
| `runtime` | BF-Runtime Guide, BF-Runtime gRPC Guide |
| `sde` | P4 Studio SDE Installation Guide, BfSwitch CLI Guide |
| `examples` | p4lang P4 tutorial slides |

## Mandatory search workflow

**You are FORBIDDEN from answering substantive P4 questions from memory alone.**
"Substantive" = anything beyond confirming a tool name or routing the user to a
section. If the question is answerable purely from the user's phrasing
(clarifying intent), you may skip the search.

1. Call `mcp__p4-knowledge-rag__search_knowledge` with a focused query BEFORE you
   answer — even when you think you know it. The spec is precise; training data
   is not authoritative.
2. **Exact-term / construct queries** (`extern`, `PARDE`, `bf_switchd`,
   `BfRt::TableEntry`): default `hybrid_alpha: 0.3` (keyword-heavy).
3. **Conceptual / how-to queries** ("how does packet replication work on
   Tofino"): `hybrid_alpha: 0.6` (semantic-heavy).
4. Narrow with `category` when the domain is known: `language`, `architecture`,
   `compiler`, `runtime`, `targets`, `sde`, `examples`, `papers`.
5. **Cite inline** using the returned `source` + `chunk_index`:
   `[CAP-UG27-005_Bf-Runtime-Guide.pdf §3.2]` or
   `[P4-16-spec-v1.2.5.md "Header types"]`.
6. If the search returns no useful hits, say so explicitly, then fall back to
   sources 2 and 3.
7. When the spec and the SDE guide disagree, present both with citations.

## Ingesting documents

When the user asks to add a document, treat it as **non-redistributable by
default** and place it under `documents/local/<category>/` (gitignored — keeps
proprietary Intel/Barefoot docs out of the public repo). Category is detected by
substring match on the path, so `local/<category>/` still resolves correctly
(`runtime`, `sde`, …).

- Local file the user points to → `mcp__p4-knowledge-rag__add_document` with a
  `filepath` under `local/<category>/` and the correct `category`.
- Raw content / URL → `add_document` / `add_from_url`, same `local/<category>/`
  placement and explicit `category`.
- Place a doc directly under a committed `documents/<category>/` (no `local/`)
  **only** when it is openly redistributable: a canonical p4.org / p4lang
  source, an open-licensed doc, or an Intel doc explicitly marked `PUBLIC`.

After adding, tell the user where it landed (local vs committed) and the
category, then ask whether to `mcp__p4-knowledge-rag__reindex_documents` now or
defer.

## Proactive corpus growth

When an external source meaningfully answered a question and meets ALL criteria,
ingest it (placement per above) so future queries find it locally:

- **Reliable** — primary/canonical: p4.org, p4lang/* GitHub, Intel/Barefoot
  vendor docs, ONF (Stratum, ONOS), peer-reviewed papers. NOT blogs,
  AI-generated articles, Stack Overflow, marketing, unverified tutorials.
- **Substantive** — full spec/guide/paper or long-form deep-dive. NOT snippets,
  single-issue comments, release-notes bullets.
- **Stable** — versioned/archival URL (PDF, pinned GitHub blob, dated asset,
  DOI). NOT homepages, rewritten current-stable docs, dashboards, search pages.
- **Relevant** — clearly within P4 scope.

After ingesting, tell the user what/which-category/why in a sentence or two;
they can `remove_document` if they disagree. Borderline source (fails one
criterion, e.g. no archival URL): use it for the current answer, cite, don't
ingest.

## Tool reference

`search_knowledge` · `search_similar` · `list_documents` · `list_categories` ·
`get_document` · `add_document` · `add_from_url` · `update_document` ·
`remove_document` · `get_index_stats` · `reindex_documents` ·
`evaluate_retrieval` — all under the `mcp__p4-knowledge-rag__` prefix.
