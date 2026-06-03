"""Golden retrieval evaluation harness (Issue #344).

An OFFLINE harness for detecting hybrid-search (dense + BM25) retrieval-quality
**regressions** before they ship. See ``README.md`` for the methodology and the
statistical do/don'ts — in particular, this is a self-consistency / regression
harness, NOT a human-labeled gold benchmark, so the absolute P@5/MRR numbers are
drift signals, not quality claims for external publication.

Two layers, split by infrastructure cost:

- **Deterministic (runs in normal CI):** corpus-schema validation, the leakage
  check (``tools/leakage_check.py`` + ``test_leakage.py``), stratification
  (``tools/stratify.py``), and the metric functions (``metrics.py``). Pure token
  analysis — no Qdrant, no embedding API.
- **Live-stack (developer-local ``make eval-retrieval``):** the actual
  retrieval-quality measurement (``test_retrieval_quality.py``), which needs
  Qdrant + an embedding provider + Sudachi and is ``skipif``-guarded.
"""
