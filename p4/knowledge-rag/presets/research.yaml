# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                              ║
# ║              KNOWLEDGE RAG — Research & Academic Preset                      ║
# ║                                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
#
# Optimized for: Academic research, papers, literature reviews, lab notebooks,
#                thesis work, datasets, and study notes.
#
# Usage:  cp presets/research.yaml config.yaml


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
    - .md        # Notes, summaries
    - .txt       # Plain text
    - .pdf       # Papers, textbooks (primary format for academia)
    - .docx      # Reports, thesis drafts
    - .xlsx      # Data tables, survey results
    - .pptx      # Presentations, lecture slides
    - .csv       # Experiment data, datasets
    - .ipynb     # Jupyter Notebooks (analysis, experiments)

  exclude_patterns: []

  chunking:
    chunk_size: 1500      # Larger chunks — academic text needs more context
    chunk_overlap: 300    # More overlap — preserve cross-paragraph references


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
# Suggested folder structure:
#   documents/
#   ├── papers/             → "papers"       (Published papers, preprints)
#   ├── books/              → "books"        (Textbook chapters, excerpts)
#   ├── notes/
#   │   ├── lectures/       → "lectures"     (Class notes, lecture summaries)
#   │   └── reading/        → "reading"      (Reading notes, annotations)
#   ├── thesis/             → "thesis"       (Your thesis/dissertation work)
#   ├── experiments/        → "experiments"  (Lab notebooks, protocols)
#   ├── datasets/           → "datasets"     (Data descriptions, codebooks)
#   └── writing/            → "writing"      (Drafts, submissions, reviews)

category_mappings:
  "papers": "papers"
  "books": "books"
  "notes/lectures": "lectures"
  "notes/reading": "reading"
  "notes": "notes"
  "thesis": "thesis"
  "experiments": "experiments"
  "datasets": "datasets"
  "writing": "writing"


# ============================================================================
# KEYWORD ROUTING
# ============================================================================

keyword_routes:

  # --- Papers & Literature ---
  papers:
    - paper
    - study
    - findings
    - methodology
    - hypothesis
    - abstract
    - conclusion
    - literature review
    - systematic review
    - meta-analysis
    - citation
    - doi
    - arxiv
    - preprint
    - peer review
    - journal
    - conference

  # --- Lectures & Coursework ---
  lectures:
    - lecture
    - class
    - professor
    - course
    - exam
    - midterm
    - final
    - syllabus
    - assignment
    - homework
    - quiz
    - tutorial
    - seminar
    - workshop

  # --- Thesis Work ---
  thesis:
    - thesis
    - dissertation
    - research question
    - research gap
    - contribution
    - defense
    - proposal
    - committee
    - advisor
    - chapter

  # --- Experiments & Lab ---
  experiments:
    - experiment
    - protocol
    - lab
    - sample
    - control group
    - treatment
    - variable
    - measurement
    - observation
    - replication
    - trial
    - procedure
    - apparatus

  # --- Data & Analysis ---
  datasets:
    - dataset
    - data
    - csv
    - survey
    - questionnaire
    - respondent
    - codebook
    - variable
    - statistical
    - regression
    - correlation
    - p-value
    - significance
    - confidence interval
    - sample size

  # --- Writing & Publishing ---
  writing:
    - draft
    - revision
    - submission
    - reviewer
    - rebuttal
    - camera-ready
    - formatting
    - template
    - impact factor
    - open access
    - supplementary


# ============================================================================
# QUERY EXPANSION
# ============================================================================

query_expansions:

  # --- Research Methods ---
  rct:                ["randomized controlled trial", "rct"]
  qual:               ["qualitative", "qual", "qualitative research"]
  quant:              ["quantitative", "quant", "quantitative research"]
  lit review:         ["literature review", "lit review", "systematic review"]
  meta:               ["meta-analysis", "meta"]
  grounded theory:    ["grounded theory", "gt"]
  ethnography:        ["ethnography", "ethnographic"]

  # --- Statistics ---
  anova:              ["analysis of variance", "anova"]
  ci:                 ["confidence interval", "ci"]
  sd:                 ["standard deviation", "sd", "std dev"]
  se:                 ["standard error", "se"]
  df:                 ["degrees of freedom", "df"]
  r-squared:          ["r-squared", "r2", "coefficient of determination"]
  chi-square:         ["chi-square", "chi-squared", "chi2"]
  t-test:             ["t-test", "student's t-test", "independent t-test"]

  # --- Machine Learning (if applicable) ---
  ml:                 ["machine learning", "ml"]
  dl:                 ["deep learning", "dl"]
  nlp:                ["natural language processing", "nlp"]
  cv:                 ["computer vision", "cv"]
  nn:                 ["neural network", "nn"]
  cnn:                ["convolutional neural network", "cnn"]
  rnn:                ["recurrent neural network", "rnn"]
  llm:                ["large language model", "llm"]
  gpt:                ["generative pre-trained transformer", "gpt"]
  bert:               ["bidirectional encoder representations", "bert"]
  gan:                ["generative adversarial network", "gan"]
  rl:                 ["reinforcement learning", "rl"]
  svm:                ["support vector machine", "svm"]
  pca:                ["principal component analysis", "pca"]
  knn:                ["k-nearest neighbors", "knn"]

  # --- Academic Publishing ---
  doi:                ["digital object identifier", "doi"]
  isbn:               ["international standard book number", "isbn"]
  issn:               ["international standard serial number", "issn"]
  oa:                 ["open access", "oa"]
  cc:                 ["creative commons", "cc"]

  # --- Common Academic Abbreviations ---
  et al:              ["et al", "et alia", "and others"]
  ibid:               ["ibid", "ibidem", "same source"]
  cf:                 ["cf", "compare", "confer"]
  eg:                 ["for example", "e.g.", "eg"]
  ie:                 ["that is", "i.e.", "ie"]
