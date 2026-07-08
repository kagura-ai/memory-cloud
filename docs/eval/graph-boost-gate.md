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

## The gate (pre-declared, deterministic)

`tests/eval/graph_boost_gate.py::evaluate_gate` — primary metric P@5, BCa
CIs (`stats.paired_bca_ci`, 10k resamples, seed 1213):

| Contract | Condition | On failure |
|---|---|---|
| beats no-graph | boosted_real − unboosted: BCa CI low > 0 (companion probes) | `no_effect` — valid close, not shipped |
| beats placebo | boosted_real − boosted_on_rewired: BCa CI low > 0 | `density_artifact` — does NOT ship even though it beat no-graph |
| non-inferiority | non-graph-query P@5 delta ≥ −0.01 (pre-declared ε) | `regression` — vetoes even a winner (fusion-dilution lesson) |

`ship` requires all three. Verdicts are recorded either way in the results
JSON — a clean "does not beat placebo" is a valid close per the issue.

## Running it

```bash
make up
make eval-graph-boost   # writes backend/tests/eval/results/graph-boost-<date>.json
```

The runner (`tests/eval/graph_boost_runner.py`) builds one warm graph
(provisional τ → replay → sleep, the placebo_runner procedure), measures the
four arms with `ENABLE_NEURAL_MEMORY=false` (no Hebbian writes between
paired arms), swaps in a degree-preserving rewired graph for the placebo arm
(seed 42) and restores the snapshot afterwards.

## Cohort protection

All existing eval arms pin `KAGURA_GRAPH_BOOST_ENABLED=false`
(`runner._score_arm`, `replay_runner` measurement + `_replay` warm build,
`reinforce_runner` arms) so an operator-exported env can never contaminate
the frozen-corpus baselines the #1210 CI gates compare against.
