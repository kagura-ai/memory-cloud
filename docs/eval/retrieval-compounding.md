# Retrieval Compounding Experiment (Issue #969)

The Tier B companion to the [#967 multi-arm benchmark](retrieval-benchmark.md):
that benchmark proves what each retrieval layer adds on a *cold* corpus; this
experiment asks whether retrieval quality **compounds with use** — does driving
real recall traffic through a context measurably improve later retrieval?

> **Read the lift deltas, not the absolute values** — same publication policy
> as the Tier A benchmark. The corpus is held fixed throughout, so any warm
> lift is attributable to the learned layer, never to "more data".

## What is measured, and where

Per the Issue #120 architecture decision (deliberate, decision-pinned), the
neural graph is **written** by `recall()` (co-activation → Hebbian edge
updates) but **read** by `explore()` (activation spreading) — graph signals
are kept out of `recall()` ranking because mixing them in degrades precision.
Compounding therefore has to be measured on the surface that reads the
learned layer:

| Lane | Surface | Metric | Expectation |
|---|---|---|---|
| **graph lane** (primary) | `explore()` from a probe's seed gold doc | companion `recovery@5/10`, MRR@10 | this is where lift can appear |
| **recall lane** (control) | hybrid `recall()`, neural off | P@5/10, MRR@10, nDCG@5/10 | flat by design — movement = regression |

Probes are the 5 multi-gold `cross-source` queries of the frozen corpus: only
a multi-doc gold set can show "the graph learned that these belong together".

## Protocol

Per replay mode, in an isolated throwaway workspace:

```
ingest (16 docs) → COLD checkpoint
                 → replay traffic: recall() × 8 rounds, ENABLE_NEURAL_MEMORY=true
                 → WARM_REPLAY checkpoint
                 → Sleep edges_only consolidation (no-LLM auto-accept judge)
                 → WARM_SLEEP checkpoint → lift tables
```

- `exclude_probes`: probes never appear in replay traffic — lift measures
  **generalization** from related traffic.
- `include_probes`: every query replayed — the production reality
  (**rehearsal**: users re-ask the questions that matter to them).

Checkpoints are read-only w.r.t. the graph and snapshot per-origin edge
counts, so lift attribution is mechanical: `hebbian` edges come from replay,
`semantic` edges from Sleep.

## Result — 2026-06-10 run

Frozen corpus v1, `recall k=10`, 8 replay rounds (200/240 recalls per mode),
embedding `text-embedding-3-small` (512 dims). Source:
[`backend/tests/eval/results/compounding-2026-06-10.json`](../../backend/tests/eval/results/compounding-2026-06-10.json).

**Retrieval does not yet compound with use — and the experiment can say
exactly why.**

| Checkpoint | graph-lane recovery@10 | recall-lane nDCG@10 | hebbian edges |
|---|---|---|---|
| cold | 0.0 | 0.9707 | 0 |
| warm_replay | 0.0 | 0.9707 | 6 |
| warm_sleep | 0.0 | 0.9707 | 6 |

(Both modes identical on these figures.)

### Attribution (the gate audit)

The mechanism itself **works**: replay traffic created Hebbian edges (6 rows,
avg weight ~0.33–0.37 after 8 rounds) wherever the write-path gates allowed.
The zero lift is fully explained by two gates in `recall()`'s Hebbian write
path:

1. **Semantic gate** (`min_similarity_for_edge = 0.5`): a co-activated pair
   only forms an edge if its embedding cosine reaches 0.5. On this corpus,
   inter-doc cosine runs ≈0.1–0.5 under `text-embedding-3-small`, so 80–91%
   of co-activated pair observations are gated. Critically, **every probe
   gold pair was co-recalled with healthy Hebbian Δw (0.014–0.036, well above
   the prune floor) and 100% of them were cosine-gated** (their cosines:
   0.37–0.40). Cross-topic associations — exactly the pairs where a learned
   layer would add value over similarity search — are exactly the pairs the
   anti-noise gate rejects.
2. **Prune cliff** (`prune_threshold = 0.01`): a first update below the
   threshold deletes the edge instead of storing it, so sub-threshold pairs
   can never accumulate across rounds. A secondary effect here (~12% of
   pairs), but it converts "slowly learnable" into "never learnable".

The Sleep `edges_only` pass found `no_edge_candidates` on this corpus (its
mid-similarity candidate band sits above the corpus's inter-doc cosine), so
the Sleep increment is also zero — recorded, not assumed.

### What this does and does not say

- It does **not** say the Hebbian mechanism is broken — edges form and
  persist (post-#970 the half-life is 14 days) where the gates pass.
- It does **not** say the gate is wrong — `min_similarity_for_edge` is a
  deliberate anti-noise device (Issue #118). It says the **absolute 0.5
  threshold is not calibrated to the embedding model's similarity
  distribution**, the same class of problem #240 solved for k-NN seeding
  with percentile calibration.
- It **does** say that under today's defaults, co-recall traffic cannot grow
  the cross-topic associations that would make retrieval compound — on this
  corpus and, by the same cosine argument, on any corpus whose related-but-
  distinct docs sit below 0.5 cosine.
- The recall lane stayed exactly flat across all checkpoints in both modes —
  the warming traffic causes **no precision regression**, confirming the
  Issue #120 separation does its job.

## Reproduce it

```bash
# 1. Stack up (Postgres + Qdrant + Redis) and schema current:
make up
cd backend && alembic upgrade head && cd ..

# 2. Deterministic layer (no infra — plan/metric/lift/audit logic):
cd backend && pytest tests/eval/test_compounding.py -q && cd ..

# 3. Live experiment (writes backend/tests/eval/results/compounding-<date>.json):
make eval-compounding
```

Numbers are never fabricated — a results JSON exists only if a real run
produced it.

## Scope boundaries

| Concern | Where it lives |
|---|---|
| Static layer-by-layer quality on the cold corpus | [#967 benchmark](retrieval-benchmark.md) |
| Calibrating the semantic gate per embedding model (percentile-based, cf. #240) | follow-up filed from this experiment |
| Recall-feedback labels to replace synthetic gold | [#344](https://github.com/kagura-ai/memory-cloud/issues/344) / [#375](https://github.com/kagura-ai/memory-cloud/issues/375) |
| Harness internals (corpus, buckets, leakage, protocol details) | [`backend/tests/eval/README.md`](../../backend/tests/eval/README.md) |
