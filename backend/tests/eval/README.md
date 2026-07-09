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
| `compounding.py` | Pure #969 experiment logic: replay plan, companion-recovery metric, lift table, gate audit | ✅ (`test_compounding.py`) |
| `test_retrieval_quality.py` + `runner.py` | **Live** multi-arm P@5/MRR/nDCG measurement → `results/<date>.json` | 🌙 nightly (`eval-nightly.yml`) |
| `test_compounding_live.py` + `replay_runner.py` | **Live** cold→replay→warm compounding experiment (#969) → `results/compounding-<date>.json` | ❌ skip-guarded |
| `update_runner.py` | **Live** H4 update-correctness experiment (update-slice) → `results/<label>-<date>.json` | 🌙 nightly (`eval-nightly.yml`) |
| `ci_gates.py` + `fixtures/ci_baseline.json` | #1210 contract gates over live results (claims → contracts) | ✅ (`test_ci_gates.py`) + 🌙 nightly |

The deterministic layer (everything but the live rows) is pure token analysis and
runs in normal CI (`backend-unit`). The live measurements need Postgres + Qdrant +
Redis + an embedding provider + Sudachi; since #1210 the update-slice and
retrieval slices run **nightly** via `.github/workflows/eval-nightly.yml`
(secret-gated on `OPENAI_API_KEY`; fork clones and secret-less runs exit neutral).
Locally they remain skip-guarded behind `KAGURA_EVAL_LIVE=1`.

## Claims → contracts (#1210)

Every headline number from the eval program is a standing contract the nightly
workflow re-measures. Defined in `ci_gates.py`, bounded by
`fixtures/ci_baseline.json`:

| Contract | Bound | Why |
|---|---|---|
| `update.stale_only_zero` | 0 | the #1195 failure mode (judged merge deletes the CURRENT fact) stays extinct; hard even with a flaky judge — the #1198 veto is deterministic |
| `update.mc_update_success_floor` | ≥ 0.80 | headline metric floor = min across archived judged runs |
| `update.vr_sanity_band` | [0.30, 0.78] | the no-update-path vanilla arm stays a coin flip; drift = broken harness/corpus |
| `update.llm_call_failures_zero` | 0 | silent judge death (#1177) cannot hide behind a green run |
| `retrieval.overall_p5_floor` | ≥ 0.18 | golden-corpus drift signal with embedding-jitter margin |

Rules:

- **Three-value gate**: PASS (0) / BREACH (1, the only red) / INFRA (3 —
  measurement unavailable; the workflow warns and files an `eval-infra` issue
  instead of a false red).
- **Staged promotion** (#336 pattern): gate mode comes from the
  `EVAL_GATES_MODE` repo variable — `advisory` (report only) for the first
  soak week, flip to `blocking` once flakiness < 1%.
- **Baseline governance**: `fixtures/ci_baseline.json` is updated ONLY via a
  normal PR that justifies the intentional change and links its issue. CI
  never rewrites the baseline — auto-updating teaches the gate to accept
  regressions as the new normal.
- Breaches/infra auto-file (or comment on) a tracking issue labeled
  `eval-regression` / `eval-infra` — nightly failures must not depend on
  someone watching the Actions tab.

## Running

```bash
# Deterministic gates (CI-equivalent, no infra):
make eval-leakage                # leakage check only (fast)
cd backend && pytest tests/eval/ -m "not asyncio" -q   # all deterministic gates

# Live retrieval measurement (needs the stack up: make up):
make eval-retrieval              # sets KAGURA_EVAL_LIVE=1, writes results/<date>.json

# Live update-correctness measurement (needs the stack up: make up):
make eval-update                 # writes results/update-<date>.json

# Contract gates over the newest update results (0=pass 1=breach 3=infra):
make eval-ci-gates

# Live compounding experiment (#969, needs the stack up: make up):
make eval-compounding            # writes results/compounding-<date>.json
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

### Compounding experiment (#969)

Tier B companion to the arms above: does retrieval improve **as the context is
used**? Per Issue #120 (decision-pinned), the neural graph is read by
`explore()` (activation spreading), not by `recall()` ranking — so the
experiment measures two lanes per checkpoint:

- **graph lane** (primary): the 5 multi-gold `cross-source` queries are held
  out as probes. From each probe's *seed* gold doc, can activation spreading
  recover the *companion* gold docs? (`recovery@k`, MRR over the explore
  ranking.)
- **recall lane** (control): hybrid `recall()` P@5/MRR/nDCG over all queries,
  measured with neural off (read-only). Flat-by-design; movement here is a
  regression signal, not lift.

Protocol per replay mode, each in its own throwaway workspace, corpus held
fixed throughout (growth ≠ "more data"):

```
ingest → COLD checkpoint → replay traffic (ENABLE_NEURAL_MEMORY=true, 8 rounds)
       → WARM_REPLAY checkpoint → Sleep edges_only run (no-LLM auto-accept)
       → WARM_SLEEP checkpoint → per-lane lift tables
```

Two replay modes separate generalization from rehearsal: `exclude_probes`
(probes never replayed — lift measures generalization from related traffic)
and `include_probes` (production reality — users re-ask what matters to them).
Checkpoints snapshot per-origin edge counts, so lift is attributable
(`hebbian` ← replay, `semantic` ← Sleep).

**Gate audit.** The results JSON carries a per-pair audit of the replay
traffic against the two edge-formation gates in `recall()`'s write path: the
semantic gate (pair cosine ≥ `min_similarity_for_edge`) and the prune cliff
(a first update below `prune_threshold` is deleted, never accumulated). The
2026-06-10 run showed why this matters: every probe gold pair co-recalls with
healthy Δw, but **all of them are cosine-gated** (0.37–0.40 vs the 0.5
threshold), so the graph-lane lift is structurally zero on this corpus — an
attributable finding about gate calibration, not a mute number. See
[`docs/eval/retrieval-compounding.md`](../../../docs/eval/retrieval-compounding.md)
for the written result.

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
