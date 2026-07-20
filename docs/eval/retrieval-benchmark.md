# Reproducible Retrieval Benchmark (Issue #967)

A multi-arm retrieval benchmark on the frozen golden corpus, built on the
[#344 eval harness](../../backend/tests/eval/README.md). It answers one
question with numbers anyone can reproduce: **what does each retrieval layer
add over the previous one?**

> **Read the deltas, not the absolute values.** The relevance labels are an
> authored starter set (binary, ≥1 relevant doc per query), not a
> human-labeled gold benchmark with inter-annotator agreement — that is the
> [#375 labeling protocol](https://github.com/kagura-ai/memory-cloud/issues/375)
> follow-up. Arm-to-arm comparisons on the *same* corpus + labels are
> methodologically sound; the absolute numbers are not a quality claim.

## Arms

All arms run against the same ingested corpus in the same process, so the
deltas isolate the retrieval layer, not the data:

| Arm | What it is |
|---|---|
| `keyword` | BM25-only (Qdrant sparse vectors, Sudachi tokenization) — "plain full-text search" |
| `semantic` | Dense-vector-only (cosine) — "plain RAG" |
| `hybrid` | Production hybrid scoring (60% semantic + 40% BM25), neural layer off |
| `hybrid_neural` | Full production posture: hybrid + neural boost (activation spreading, Hebbian graph) |

## Results — baseline 2026-06-10

Frozen corpus v1 (16 docs, 30 queries × 6 buckets), `recall k=10`, Sudachi
dict `20260116`, embedding `text-embedding-3-small` (512 dims). Source:
[`backend/tests/eval/results/2026-06-10.json`](../../backend/tests/eval/results/2026-06-10.json).

| Arm | MRR@10 | nDCG@5 | nDCG@10 | P@5 | P@10 |
|---|---|---|---|---|---|
| `keyword` (BM25-only) | 0.8198 | 0.8140 | 0.8342 | 0.2000 | 0.1100 |
| `semantic` (vector-only) | 0.9028 | 0.8940 | 0.9001 | 0.2200 | 0.1133 |
| `hybrid` | **1.0000** | **0.9586** | **0.9707** | 0.2133 | 0.1133 |
| `hybrid_neural` | **1.0000** | **0.9586** | **0.9707** | 0.2133 | 0.1133 |

### How to read this

- **Hybrid beats both single-signal baselines decisively.** MRR@10 1.0 means
  the first relevant doc ranked #1 for every one of the 30 queries; BM25-only
  misses that on hiragana/paraphrase queries (−0.18 MRR), vector-only on
  exact-term queries (−0.10 MRR). The two signals fail on *different* buckets,
  which is exactly why the 60/40 merge recovers both.
- **P@k is ceiling-limited here, not low.** Most queries have a single
  relevant doc, so P@5 cannot exceed 0.2 for them. MRR and nDCG are the
  informative metrics on this corpus.
- **`hybrid_neural` equals `hybrid` exactly — by design of the experiment.**
  The benchmark ingests a fresh corpus into a fresh context: the neural graph
  is cold (in this run, zero seed edges — the environment had no similarity
  calibration for the embedding model), so the neural layer has nothing to act
  on. The honest claims this row supports are (a) the neural layer causes **no
  regression** when cold, and (b) any lift must come from *use* — which is the
  controlled cold→warm replay experiment tracked in
  [#969](https://github.com/kagura-ai/memory-cloud/issues/969), not this
  benchmark.
- **Run-to-run jitter exists.** Tie-breaks among equally-scored docs depend on
  per-run point IDs; observed jitter is ≲0.02 on the keyword arm. Per the
  harness policy, treat sub-0.02 single-arm drift as noise and rank-order
  changes in the top-5 as the signal worth reviewing.

## Reconsidering the 60/40 hybrid default on knowledge-base corpora (#1404)

The 16-doc result above is a **small, exact-match-heavy** corpus where hybrid's
two-signal recovery decisively wins. That does **not** generalize to the larger
knowledge-base corpus family, and the evidence there is now twice-replicated:

On the frozen **kagura_L** corpus (300 docs / 240 held-out queries, KB-style
content), `hybrid − max(keyword, semantic)` held-out P@5:

- **prereg-v1** (v0.43/v0.44): null, significantly **negative** at the larger
  embedder ("Two Wins, Two Nulls" §7).
- **prereg-v2** (v0.53.0 run0): **−0.0117**, BCa 95% [−0.0258, 0.0025],
  σ_d = 0.111, **achieved power 1.00 at δ = 0.05** — a construct null, not
  underpowered. Arm means: `semantic` 0.1800 > `hybrid_neural` 0.1725 >
  `hybrid` 0.1683 ≫ `keyword` 0.1142.
- The #1212 query-intent router, once enabled, effectively *learns* this: it
  routed **243/280** held-out queries to semantic, 36 to keyword, 1 to hybrid,
  and edged the best static arm by only +0.005.

Artifacts: "Two Wins, Two Nulls" §7 + §14.1 (kagura-memory-eval `paper/draft-full.md`;
`results/v053/f1-v053-*.json`, `router-calibration-2026-07-19.json`).

### Decision (an experiment + a decision, not a blind flip)

1. **Keep `ContextSearchConfig.semantic_weight = 0.60` (60/40) as the fleet
   default for now.** The evidence favors a higher semantic weight (or
   semantic-with-keyword-fallback) on KB corpora, but a fleet default change
   must ship with a **paired kagura_L CI** (the harness + BCa machinery take
   minutes), not on this reasoning alone — the same discipline the reinforce
   (#1207) and graph-boost (#1213/#1405) defaults were held to.
2. **The principled mechanism is the query-intent router, not a static
   re-weight.** `routing_mode='active'` (#1212) converges to per-query semantic
   on KB corpora and keyword on exact-match ones, so it dominates any single
   static arm across corpus families. It already ships **default-off until the
   stage-3 calibration gate shows it beating semantic-only** (see
   [`router-calibration-gate.md`](router-calibration-gate.md) and the
   `routing_mode` field comment in `backend/src/models/config.py`) — the same
   bar hybrid failed. Promoting the router to default (or raising the semantic
   weight) is gated on that run, tracked by #1404.
3. **Caveat to carry:** this null is one corpus family. Short,
   exact-match-heavy workloads (like the 16-doc corpus above) genuinely favor
   the keyword/hybrid signals — which is precisely the router's case, and why
   per-query routing is the principled default rather than any fixed weight.

## Reproduce it

```bash
# 1. Stack up (Postgres + Qdrant + Redis) and schema current:
make up
cd backend && alembic upgrade head && cd ..

# 2. Deterministic gates (no infra, same checks CI runs):
make eval-leakage
cd backend && pytest tests/eval/ -q && cd ..

# 3. Live multi-arm measurement (writes backend/tests/eval/results/<date>.json):
make eval-retrieval
```

The harness provisions a throwaway workspace/context, ingests the corpus,
measures all four arms (the graph-writing `hybrid_neural` arm runs last so it
cannot warm the read-only arms), and tears everything down. Numbers are never
fabricated — a results JSON exists only if a real run produced it.

## Scope boundaries

| Concern | Where it lives |
|---|---|
| Compounding ("gets better with use"): cold→warm lift under replayed co-recall traffic | [`retrieval-compounding.md`](retrieval-compounding.md) ([#969](https://github.com/kagura-ai/memory-cloud/issues/969)) |
| Wiring the live measurement into CI as an automated gate | [#336](https://github.com/kagura-ai/memory-cloud/issues/336) |
| Human-labeled gold set (inter-annotator agreement) to make absolute numbers publishable | [#375](https://github.com/kagura-ai/memory-cloud/issues/375) |
| Harness internals: corpus, buckets, leakage rules, stratification, statistical do/don't | [`backend/tests/eval/README.md`](../../backend/tests/eval/README.md) |
| Older SDK-driven Japanese search-quality benchmark (P@1/Hit@k, 129 memories) | [`docs/search-quality-benchmark.md`](../search-quality-benchmark.md) |
