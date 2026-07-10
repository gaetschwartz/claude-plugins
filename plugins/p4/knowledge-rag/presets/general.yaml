# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                              ║
# ║              KNOWLEDGE RAG — General Purpose Preset                          ║
# ║                                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
#
# Blank slate configuration. Pure semantic search, no domain-specific logic.
# Perfect starting point for any use case not covered by other presets.
#
# Usage:  cp presets/general.yaml config.yaml
#
# This preset:
#   - Zero keyword routing (pure semantic search)
#   - Zero query expansion (search terms used as-is)
#   - Zero categories (all documents treated equally)
#   - Sensible defaults for chunking and models
#
# Add your own categories, routes, and expansions as you discover
# what works best for your documents.


# ============================================================================
# PATHS
# ============================================================================

paths:
  documents_dir: "./documents"
  data_dir: "./data"
  models_cache_dir: "./models_cache"


# ============================================================================
# DOCUMENTS
# ============================================================================

documents:
  supported_formats:
    - .md
    - .txt
    - .pdf
    - .docx

  exclude_patterns: []

  chunking:
    chunk_size: 1000
    chunk_overlap: 200


# ============================================================================
# MODELS
# ============================================================================

models:
  embedding:
    model: "BAAI/bge-small-en-v1.5"
    dimensions: 384

  reranker:
    enabled: true
    model: "Xenova/ms-marco-MiniLM-L-6-v2"
    top_k_multiplier: 3


# ============================================================================
# SEARCH
# ============================================================================

search:
  default_results: 5
  max_results: 20
  collection_name: "knowledge_base"


# ============================================================================
# CATEGORIES
# ============================================================================
# No categories by default. Add your own as needed.
# Example:
#   category_mappings:
#     "work": "work"
#     "personal": "personal"

category_mappings: {}


# ============================================================================
# KEYWORD ROUTING
# ============================================================================
# No routing by default. Pure semantic search.
# Example:
#   keyword_routes:
#     work:
#       - project
#       - deadline
#       - meeting

keyword_routes: {}


# ============================================================================
# QUERY EXPANSION
# ============================================================================
# No expansions by default. Search terms used as-is.
# Example:
#   query_expansions:
#     asap:
#       - as soon as possible
#       - asap
#       - urgent

query_expansions: {}
