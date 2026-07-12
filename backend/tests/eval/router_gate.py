"""Pure logic for the #1220 router calibration gate (stage 3 of #1212).

The query-intent router (#1212) ships experiment-gated: ``routing_mode``
stays ``off`` by default until the router clears THIS gate on the frozen
corpus — the same bar fixed-weight hybrid failed. The pre-declared contracts:

- **beats semantic overall** — routed recall must beat semantic-only recall
  on held-out P@5 AND MRR@10 (each paired BCa CI for the mean per-query
  delta must exclude 0 from below);
- **each routed lane wins its bucket** — on the queries the classifier
  routes to lane L, the routed arm must be at least as good as the strongest
  SINGLE component (semantic-only / keyword-only) on that bucket. Ties are a
  pass: on a bucket routed to a component lane the routed rankings ARE that
  component's rankings, so "beat" degenerates to "the router picked the
  winning component". Hybrid is a fusion, not a single component, so it sets
  no bar here (it already failed the overall bar in the eval program);
- **powered** — the overall decision needs ``MIN_QUERIES`` paired queries,
  and every non-empty bucket needs ``MIN_BUCKET_QUERIES`` before the gate
  will render a flip-ready verdict (a lane the router uses on real traffic
  must not graduate unmeasured).

Everything here is deterministic and infrastructure-free (unit-tested in
``test_router_gate.py``). The live orchestration that produces the arm
rankings lives in ``tests.eval.router_gate_runner``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tests.eval.metrics import (
    mean_precision_at_k,
    mrr_at_k,
    precision_at_k,
    reciprocal_rank_at_k,
)
from tests.eval.stats import paired_bca_ci

#: One query result: ranked doc-ids + gold set (the #344 metrics shape).
Ranking = tuple[Sequence[str], set[str]]

#: Primary metric (matches the pre-registered headline metric).
PRIMARY_K = 5
#: Secondary metric depth.
MRR_K = 10

#: BCa resamples (matches the eval program's inferential runs).
BCA_RESAMPLES = 10_000

#: Minimum paired queries for the overall inferential decision (same floor
#: as graph_boost_gate.MIN_PROBES — n=5 golden-corpus probes must not drive
#: a BCa flip call).
MIN_QUERIES = 50

#: Minimum queries per non-empty lane bucket before its component
#: comparison is considered measured rather than anecdotal.
MIN_BUCKET_QUERIES = 10

#: Single retrieval components a routed lane must not lose to on its own
#: bucket. Hybrid is a fusion of these two, not a single component.
COMPONENT_ARMS = ("semantic", "keyword")

GATE_FLIP_READY = "flip_ready"
GATE_NO_EFFECT = "no_effect"
GATE_REGRESSION = "regression"
GATE_BUCKET_REGRESSION = "bucket_regression"
GATE_UNDERPOWERED = "underpowered"


def per_query_deltas(
    arm_a: Sequence[Ranking], arm_b: Sequence[Ranking], k: int, *, metric: str = "p"
) -> list[float]:
    """Paired per-query deltas (a - b) on P@k or RR@k. Raises on length mismatch."""
    if len(arm_a) != len(arm_b):
        raise ValueError(f"paired arms differ in length: {len(arm_a)} vs {len(arm_b)}")
    score = precision_at_k if metric == "p" else reciprocal_rank_at_k
    return [
        score(ranked_a, gold_a, k) - score(ranked_b, gold_b, k)
        for (ranked_a, gold_a), (ranked_b, gold_b) in zip(arm_a, arm_b, strict=True)
    ]


def arm_metrics(rankings: Sequence[Ranking]) -> dict[str, float | int]:
    """Summary metric block for one arm (reuses the #344 binary metrics)."""
    return {
        "n": len(rankings),
        "p@5": round(mean_precision_at_k(rankings, PRIMARY_K), 4),
        "mrr@10": round(mrr_at_k(rankings, MRR_K), 4),
    }


def bucket_indices(lanes: Sequence[str]) -> dict[str, list[int]]:
    """Group query indices by the classifier's lane decision."""
    buckets: dict[str, list[int]] = {}
    for i, lane in enumerate(lanes):
        buckets.setdefault(lane, []).append(i)
    return buckets


def _slice(arm: Sequence[Ranking], indices: Sequence[int]) -> list[Ranking]:
    return [arm[i] for i in indices]


def evaluate_router_gate(
    *,
    routed: Sequence[Ranking],
    components: dict[str, Sequence[Ranking]],
    lanes: Sequence[str],
    seed: int,
) -> dict[str, Any]:
    """Apply the pre-declared #1220 contracts to the measured arm rankings.

    Args:
        routed: Per-query rankings under the router's lane decisions
            (query i answered with ``components[lanes[i]][i]`` — the router
            is deterministic, so the routed arm is constructed, not
            re-measured).
        components: Full paired arms keyed by search mode. MUST contain
            ``semantic`` and ``keyword`` (the single components); ``hybrid``
            is included in the report when present.
        lanes: The classifier's lane per query, parallel to ``routed``.
        seed: Bootstrap seed (pre-declared in the run config).

    Returns:
        JSON-shaped verdict: per-arm metrics, both overall BCa intervals,
        per-bucket component comparisons, and ``verdict`` in {flip_ready,
        bucket_regression, no_effect, regression, underpowered}. A clean
        "does not beat semantic, stays opt-in" is a valid close.
    """
    for required in COMPONENT_ARMS:
        if required not in components:
            raise ValueError(f"components must include the '{required}' arm")
    if len(lanes) != len(routed):
        raise ValueError(f"lanes and routed differ in length: {len(lanes)} vs {len(routed)}")
    semantic = components["semantic"]

    vs_semantic_p = paired_bca_ci(
        per_query_deltas(routed, semantic, PRIMARY_K, metric="p"),
        n_resamples=BCA_RESAMPLES,
        seed=seed,
    )
    vs_semantic_mrr = paired_bca_ci(
        per_query_deltas(routed, semantic, MRR_K, metric="rr"),
        n_resamples=BCA_RESAMPLES,
        seed=seed,
    )

    buckets_report: dict[str, dict[str, Any]] = {}
    all_buckets_measured = True
    all_buckets_won = True
    for lane, indices in sorted(bucket_indices(lanes).items()):
        routed_slice = _slice(routed, indices)
        routed_mean = round(mean_precision_at_k(routed_slice, PRIMARY_K), 4)
        component_means = {
            arm: round(mean_precision_at_k(_slice(components[arm], indices), PRIMARY_K), 4)
            for arm in COMPONENT_ARMS
        }
        strongest_arm = max(component_means, key=lambda a: component_means[a])
        measured = len(indices) >= MIN_BUCKET_QUERIES
        # Ties pass: on a component-lane bucket the routed slice IS that
        # component's slice, so ">=" asks "did the router pick the winner".
        won = routed_mean >= component_means[strongest_arm]
        all_buckets_measured &= measured
        all_buckets_won &= won or not measured
        buckets_report[lane] = {
            "n": len(indices),
            "measured": measured,
            "routed_p@5": routed_mean,
            "components_p@5": component_means,
            "strongest_component": strongest_arm,
            "beats_strongest_component": won,
        }

    beats_semantic = vs_semantic_p["ci_low"] > 0.0 and vs_semantic_mrr["ci_low"] > 0.0
    regressed = vs_semantic_p["ci_high"] < 0.0 or vs_semantic_mrr["ci_high"] < 0.0
    powered = len(routed) >= MIN_QUERIES and all_buckets_measured

    if not powered:
        verdict = GATE_UNDERPOWERED
    elif regressed:
        verdict = GATE_REGRESSION
    elif beats_semantic and all_buckets_won:
        verdict = GATE_FLIP_READY
    elif beats_semantic:
        verdict = GATE_BUCKET_REGRESSION
    else:
        verdict = GATE_NO_EFFECT

    arms_report = {"routed": arm_metrics(routed)}
    for arm, rankings in components.items():
        arms_report[arm] = arm_metrics(rankings)

    return {
        "primary_metric": f"p@{PRIMARY_K}",
        "secondary_metric": f"mrr@{MRR_K}",
        "arms": arms_report,
        "vs_semantic": {"p@5": vs_semantic_p, "mrr@10": vs_semantic_mrr},
        "buckets": buckets_report,
        "contracts": {
            "powered": powered,
            "min_queries": MIN_QUERIES,
            "min_bucket_queries": MIN_BUCKET_QUERIES,
            "beats_semantic_overall": beats_semantic,
            "all_buckets_beat_strongest_component": all_buckets_won,
        },
        "verdict": verdict,
        "flip_ready": verdict == GATE_FLIP_READY,
    }
