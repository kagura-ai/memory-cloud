# Japanese Search Quality Benchmark

Comprehensive search quality benchmark for Kagura Memory Cloud's hybrid search (semantic + BM25), tested with the [Python SDK](https://github.com/kagura-ai/kagura-memory-python-sdk).

## Latest Results (v0.3.1+)

**129 memories, 96 queries, 31 categories**

| Metric | Result |
|--------|--------|
| P@1 | 85/96 (89%) |
| Hit@3 | 90/96 (94%) |
| Hit@5 | 92/96 (96%) |

### Category Breakdown

#### Perfect (100%) — 21 categories

homonym, conjugation, okurigana, long-vowel, homograph, number, katakana-typo, old-kanji, abbreviation, honorific, dialect, voice, keigo, long-query, comparison, temporal, date-format, counter, onomatopoeia, place-name, near-duplicate

#### Good (75%+) — 5 categories

kanji-kana, wago-kango, ultra-short, contextual, polysemy

#### Weak (<75%) — 3 categories

typo, mixed-lang, noise

## Scale Resilience

| Memories | P@1 | Hit@5 |
|----------|-----|-------|
| 50 (old manual BM25) | 76% | 96% |
| 65 (native sparse BM25) | 91% | 97% |
| 93 (cross-domain) | 91% | 98% |
| 129 (final) | 89% | 96% |

## Search Architecture

```
Query → Sudachi tokenization → Synonym expansion (Sudachi dict, ~25k groups)
  ↓
BM25: Sparse vector search (Qdrant native, Modifier.IDF)
  - summary/context_summary tokens (weight ×2.0)
  - content tokens (weight ×1.0)
  ↓
Semantic: Dense vector search (embedding model, COSINE)
  - summary only (no content/tags to avoid vector pollution)
  ↓
Hybrid merge: 60% semantic + 40% BM25
  - Min-max normalization per result set
  - fetch_factor=5 (cap at 200)
  ↓
Optional: Reranker (Voyage AI / Cohere)
  - Minimal improvement (+1 P@1) — not recommended as default
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Embedding = summary only | Tags/content in embedding caused vector pollution (P@1 regression) |
| Tags NOT in BM25 | Tags in BM25 text_conditions caused score inflation for tag-heavy memories |
| Tags = exact-match filter only | `filters: {"tags": ["python"]}` via MatchAny |
| Content in BM25 at 0.5x weight | Prevents length bias from long content fields |
| Synonym expansion capped at 50 tokens | Prevents BM25 score distortion from large synonym groups |
| MurmurHash3 for sparse indices | Deterministic, no vocabulary management, <0.1% collision rate |

## Embedding Models Tested

| Model | Dimensions | Notes |
|-------|-----------|-------|
| qwen3-embedding:8b (Ollama) | 4096 | Primary test model, local |
| text-embedding-3-small (OpenAI) | 512 | Default for new users |

## Test Script

```bash
cd kagura-memory-python-sdk
uv run python examples/test_japanese_search.py --cleanup  # Reset
uv run python examples/test_japanese_search.py             # Run
```

## Evolution History

| Version | Change | P@1 |
|---------|--------|-----|
| v0.3.0 | Manual TF scoring (baseline) | 76% |
| v0.3.1 | Tag filtering (P0+P1) | 76% |
| v0.3.1 | Content in BM25 + fetch_factor 5 | 88% |
| v0.3.1+ | Native sparse vector BM25 (#16) | 89% |
| v0.3.1+ | Sudachi synonym expansion (#69) | 89% (stable at 129 memories) |
