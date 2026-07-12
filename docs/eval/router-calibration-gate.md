# Router calibration gate (#1220 — stage 3-4 of #1212)

The query-intent router (#1212) ships experiment-gated: `routing_mode`
defaults to `off` and may only become a default after clearing this gate —
the same bar fixed-weight hybrid failed in the eval program.

## Running the gate

```bash
make up                # the gate needs the live stack
make eval-router-gate  # writes backend/tests/eval/results/router-calibration-<date>.json
```

The runner (`tests.eval.router_gate_runner`) ingests the frozen `kagura_l`
corpus once, measures three paired lane arms over ALL corpus queries —
`semantic`, `keyword`, `hybrid`, each pinned via an explicit `search_mode` —
and **constructs** the routed arm: the classifier is deterministic, so query
*i*'s routed ranking is `arms[classify_query(q_i).lane][i]`. No config flip
is needed and no fourth live arm is measured.

## Contracts (pre-declared, `tests.eval.router_gate`)

| Contract | Definition |
|---|---|
| beats semantic overall | paired BCa 95% CI of the per-query routed−semantic delta excludes 0 from below, on **both** P@5 and MRR@10 |
| every bucket wins | on the queries routed to lane L, the routed arm is at least as good (mean P@5) as the strongest single component (`semantic` / `keyword`). Ties pass — on a component-lane bucket the routed rankings ARE that component's rankings, so the contract reads "the router picked the winning component" |
| powered | ≥ 50 paired queries overall AND ≥ 10 queries in every non-empty bucket |

Verdicts: `flip_ready` / `bucket_regression` / `no_effect` / `regression` /
`underpowered`. Only `flip_ready` permits changing the `routing_mode`
default — and per the issue, real-traffic `log_only` telemetry
(`query_router_decision`) should corroborate the corpus lane mix first.

## Stage 4 — calibration store

Each gate run upserts per-bucket arm performance into the
`router_calibrations` table at fleet-default scope (`context_id IS NULL`).
Per-context rows (from live-traffic measurement, `source='live_traffic'`)
let managed-cloud tuning diverge from self-host defaults; reads via
`RouterCalibrationRepository.get_for_context` never mix scopes — a
context's own rows win, else the fleet defaults.
