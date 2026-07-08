# Graph-Boost Placebo Gate (#1213)

The warm co-activation graph carries edge-specific companion structure
(`explore()` recovers gold companions +0.20 over a degree-matched rewiring,
BCa 95% [0.117, 0.308]) that production `recall()` never consumes. #1213
runs the obvious next experiment: a **bounded multiplicative graph term** on
the recall top-k — with the known failure mode (fusion dilution / density
artifact) built into the gate as kill-shots.

## The mechanism (ships OFF)

`MemoryService._maybe_graph_boost` — runs after the reinforce re-rank:

- Env-gated: `KAGURA_GRAPH_BOOST_ENABLED` (default off → **bit-identical
  recall**: no edge query, no reorder), `KAGURA_GRAPH_BOOST_MAX` (default
  0.15, clamped to [0, 0.5]). An env flag, not a config column: an
  experiment must not mint a fifth parallel migration head (e55–e58), and
  per-context graduation only happens if this gate passes.
- Signal: each candidate's score × `1 + b·(conn/max_conn)` where `conn` is
  the summed weight of its **hebbian** edges to other candidates in the same
  pool. Boost-only `[1, 1+b]`; isolated candidates keep 1.0. Multiplicative
  with a cap — NOT additive score fusion (the F1 hybrid null).
- Hebbian only: semantic edges would double-count the vector similarity
  already in the base score; declared edges are unmeasured.
- Composes with the reinforce re-rank via the `_rerank_factor` stamp
  (bounded × bounded stays bounded).
- Fail-safe: any failure preserves the original ranking.
- Per-user edge scope (same discipline as `activation.py`): only the
  CALLER's own co-activation history moves their ranking — in a shared
  context, another member's (forgeable-by-co-recall) hebbian edges cannot.
- The env flag is deployment-global, not per-context: enable it only on
  single-tenant / eval-rig deployments. Per-context graduation (a config
  column) is exactly what the gate below decides.

## The gate (pre-declared, deterministic)

`tests/eval/graph_boost_gate.py::evaluate_gate` — primary metric P@5, BCa
CIs (`stats.paired_bca_ci`, 10k resamples, seed 1213):

| Contract | Condition | On failure |
|---|---|---|
| powered | n_probes ≥ 50 (`MIN_PROBES`) | `underpowered` — deltas reported, ship refused |
| beats no-graph | boosted_real − unboosted: BCa CI low > 0 (companion probes) | `no_effect` — valid close, not shipped |
| beats placebo | boosted_real − boosted_on_rewired: BCa CI low > 0 for EVERY pre-declared rewire seed (42/43/44) | `density_artifact` — does NOT ship even though it beat no-graph |
| non-inferiority | non-graph-query P@5 delta ≥ −0.01 (pre-declared ε) | `regression` — vetoes even a winner (fusion-dilution lesson) |

`ship` requires all four. Verdicts are recorded either way in the results
JSON — a clean "does not beat placebo" is a valid close per the issue.

The runner uses the frozen **kagura_l** corpus (300 docs, 60 multi-gold
cross-source probes) — not the 5-probe golden corpus, which is below the
inferential floor. The placebo rewires **only the hebbian edges** (the boost
reads only hebbian; rewiring semantic/declared edges too would make the null
model inconsistent with the mechanism under test).

## Running it

```bash
make up
make eval-graph-boost   # writes backend/tests/eval/results/graph-boost-<date>.json
```

The runner (`tests/eval/graph_boost_runner.py`) builds one warm graph
(provisional τ → replay → sleep, the placebo_runner procedure), measures the
four arms with `ENABLE_NEURAL_MEMORY=false` (no Hebbian writes between
paired arms), swaps in a degree-preserving rewired graph for the placebo arm
(one measurement per pre-declared rewire seed) and restores the snapshot
afterwards.

**Known limitation (pre-declared)**: the non-inferiority slice is the
corpus's replay queries — the same queries the warm build replayed, so the
boost re-ranks exactly their co-activated results. That slice is
leaky-optimistic, not conservative. A pass here is necessary but NOT
sufficient; the frozen held-out retrieval slice is the honest
non-inferiority check before any per-context graduation.

## Cohort protection

All existing eval arms pin `KAGURA_GRAPH_BOOST_ENABLED=false`
(`runner._score_arm`, `replay_runner` measurement + `_replay` warm build,
`reinforce_runner` arms) so an operator-exported env can never contaminate
the frozen-corpus baselines the #1210 CI gates compare against.
