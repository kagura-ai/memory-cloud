# Golden Retrieval Eval Harness (Issue #344)

An **offline harness** for detecting hybrid-search (dense + BM25) retrieval-quality
**regressions** before they ship. Synthesis of the #335 PhD/CxO panel (CAIO + Stats + DS + CS).

> **This is a regression / self-consistency harness, NOT a human-labeled gold
> benchmark.** The relevance labels are an authored starter set. The absolute
> P@5 / MRR numbers are **drift signals** — compare a run against a prior run on
> the same frozen corpus. **Do not publish the absolute numbers as a quality
> claim.** Human-labeled gold sets (inter-annotator agreement, Krippendorff's α)
> are tracked separately under the #375 labeling protocol.

## Layout

| Path | What | Runs in CI? |
|---|---|---|
| `fixtures/golden_corpus.yaml` | Frozen corpus: 16 docs + 30 queries × 6 buckets + binary labels | — |
| `metrics.py` | Pure P@k / MRR@k / source-recall functions | ✅ (`test_metrics.py`) |
| `tools/corpus.py` | Corpus loader + BM25-aligned tokenization + IDF stats | ✅ |
| `tools/leakage_check.py` | Leakage detector (3 rules) | ✅ (`test_leakage.py`) |
| `tools/stratify.py` | Difficulty stratification (IDF-spec, BM25-rank, corpus-overlap) | ✅ (`test_stratification.py`) |
| `test_corpus_schema.py` | Structural contract (buckets, labels, sources) | ✅ |
| `test_retrieval_quality.py` + `runner.py` | **Live** multi-arm P@5/MRR/nDCG measurement → `results/<date>.json` | ❌ skip-guarded |

The deterministic layer (everything but the last row) is pure token analysis and
runs in normal CI. The live measurement needs Postgres + Qdrant + an embedding
provider + Sudachi, which is **not currently CI-realistic** (#336 unscheduled),
so it is skip-guarded behind `KAGURA_EVAL_LIVE=1`.

## Running

```bash
# Deterministic gates (CI-equivalent, no infra):
make eval-leakage                # leakage check only (fast)
cd backend && pytest tests/eval/ -m "not asyncio" -q   # all deterministic gates

# Live retrieval measurement (needs the stack up: make up):
make eval-retrieval              # sets KAGURA_EVAL_LIVE=1, writes results/<date>.json
```

### Comparison arms (#967)

Each live run measures every query under four **arms** on the same ingested
corpus, so the deltas between arms isolate what each retrieval layer adds:

| Arm | `search_mode` | `ENABLE_NEURAL_MEMORY` | What it isolates |
|---|---|---|---|
| `keyword` | `keyword` | off | BM25-only baseline ("plain full-text search") |
| `semantic` | `semantic` | off | dense-vector-only baseline ("plain RAG") |
| `hybrid` | `hybrid` | off | hybrid (60/40) scoring without the neural layer |
| `hybrid_neural` | `hybrid` | on | full production posture (neural boost + activation spreading) |

Arm order is load-bearing: with neural enabled, `recall()` itself performs
co-activation tracking + Hebbian updates (graph writes), so `hybrid_neural`
runs **last**. All arms see the same cold graph (k-NN / tag co-occurrence
seeding happens at embedding time, independent of the env var). Quality growth
as the graph warms with use is the #969 companion experiment, not this harness.

Results JSON: per-arm metrics live under `"arms"`; the top-level
`overall` / `per_bucket` / `source_recall@10` mirror the `production_arm`
(`hybrid_neural`) so the pre-#967 shape — and drift comparison against older
results files — keeps working. **Publish arm-to-arm deltas only**, never the
absolute numbers (see "Statistical do / don't").

### Baseline

`results/<YYYY-MM-DD>.json` is produced **only** by a real `make eval-retrieval`
run — numbers are never hand-written or fabricated (a fabricated baseline
silently corrupts the regression signal). If `results/` has no JSON yet, the
baseline has not been generated in an environment with the live stack; run
`make eval-retrieval` locally to create the first one and commit it alongside.

## Buckets (query difficulty / coverage)

| Bucket | Probes |
|---|---|
| `retrieval-exact` | Verbatim-phrase queries — exact-term matching |
| `retrieval-semantic` | Paraphrases — dense/semantic matching with low lexical overlap |
| `hiragana-only` | Hiragana queries against kanji/mixed docs — tokenizer + reading augmentation |
| `cross-source` | Gold set spans both memory and resource docs |
| `resource-only` | Gold set is resource-source docs |
| `memory-only` | Gold set is memory-source docs |

## Leakage check (`tools/leakage_check.py`)

A query "leaks" when it is so lexically close to its own relevant doc that any
retriever trivially wins, inflating the metric. Three rules flag a
`(query, relevant_doc)` pair:

1. **Token Jaccard > 0.5** — share more than half their token vocabulary (all buckets).
2. **Any shared 3-gram** — a verbatim 3-token run in both. **Exempt for
   `retrieval-exact`** — those queries are *designed* to share a phrase, so the
   rule would flag every valid one.
3. **Rare-term unique cooccurrence** — the query uses a corpus-unique term
   (document frequency == 1, i.e. it appears in **only** the relevant doc).
   `df == 1` terms carry the maximum IDF, so this is the robust form of the #335
   "high-IDF term unique to the relevant doc" rule — an IDF percentile gate with
   strict `>` is silently dead on hapax-heavy corpora. **Scale-gated:** fires
   only when the corpus has ≥ `MIN_DOCS_FOR_RARE_TERM` (50) docs; below that,
   `df == 1` is the norm rather than a rarity signal, so it would flag normal
   on-topic overlap (this MVP starter corpus is below the gate, so Rule 3 is
   dormant — Rules 1 & 2 cover the harmful cases at small N). `test_leakage.py`
   includes a synthetic large-corpus test proving the rule fires at scale.

`test_leakage.py` fails loud and prints every flag. Keep the corpus leakage-free.

## Stratification (`tools/stratify.py`)

Descriptive difficulty signals — **coverage characterization, not pass/fail gates**:

- **spec(q)** = avg corpus IDF of query terms (high = specific vocabulary).
- **bm25_rank** = rank of the first relevant doc under BM25-only → `easy` (1) /
  `medium` (2–3) / `hard` (>3 or unranked). The lexical-difficulty pseudo-label.
- **corpus_overlap** = overlap coefficient (|q ∩ top| / |q|) with the top-1000
  most-frequent corpus tokens. (Overlap coefficient, not true Jaccard — see
  `tools/stratify.py`.)

`test_stratification.py` only asserts the set spans ≥2 regimes and has ≥1 `hard`
query (hiragana-only is BM25-hard by construction), so an all-easy corpus — which
cannot surface ranking regressions — fails.

## Statistical do / don't

- **DO** use deterministic, count-based per-bucket assertions and rank-stability
  (top-5 list changed → human review) for regression detection.
- **DON'T** run hypothesis testing on the per-bucket metrics. The Stats analysis
  in #335 showed detecting a memory-only −0.03 MRR change needs **N ≈ 200 per
  bucket**, infeasible at this MVP scale (5/bucket). p-values here are noise.
- **DON'T** treat absolute P@5/MRR as a quality claim — only as before/after drift
  on the same frozen corpus + Sudachi version (both stamped in the results JSON).
- **DON'T** add LLM-as-judge scoring — non-determinism breaks delta monitoring.

## Labels — note on count

The #335 guidance suggested 3–5 labels per query; that assumed a larger corpus.
This frozen starter set uses **binary relevance with ≥1 relevant doc per query**
(the harness only needs a non-empty gold set). Expanding to a human-labeled set
with more judgments per query is the #375 follow-up.
