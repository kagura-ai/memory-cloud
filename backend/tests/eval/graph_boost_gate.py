"""Pure logic for the #1213 graph-boost placebo gate.

The recall graph-boost (``MemoryService._maybe_graph_boost``) ships as an
env-gated EXPERIMENT (default off). This module defines the pre-declared gate
that decides whether it may graduate to a per-context feature — the exact
protocol from issue #1213:

- **beats no-graph** — boosted recall must beat unboosted recall on
  companion-carrying queries (paired BCa CI for the mean per-query delta must
  exclude 0 from below);
- **beats placebo** — boosted-on-the-real-graph must beat the same boost
  computed on a degree-matched rewired graph. Beating no-graph but not the
  placebo = density artifact = does NOT ship (the §6 kill-shot logic);
- **non-inferiority** — held-out P@5 on non-graph queries must not regress by
  more than the pre-declared epsilon (the F1 fusion-dilution lesson).

Everything here is deterministic and infrastructure-free (unit-tested in
``test_graph_boost_gate.py``). The live orchestration that produces the arm
rankings lives in ``tests.eval.graph_boost_runner``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tests.eval.metrics import mean_precision_at_k, mrr_at_k, precision_at_k
from tests.eval.stats import paired_bca_ci

#: One query result: ranked doc-ids + gold set (the #344 metrics shape).
Ranking = tuple[Sequence[str], set[str]]

#: The gate's primary metric on companion queries. P@5 matches the
#: pre-registered headline metric of the retrieval eval.
PRIMARY_K = 5

#: Pre-declared non-inferiority margin on non-graph queries (absolute P@5).
NON_INFERIORITY_EPSILON = 0.01

#: BCa resamples (matches the eval program's inferential runs).
BCA_RESAMPLES = 10_000

GATE_SHIP = "ship"
GATE_DENSITY_ARTIFACT = "density_artifact"
GATE_NO_EFFECT = "no_effect"
GATE_REGRESSION = "regression"


def per_query_deltas(arm_a: Sequence[Ranking], arm_b: Sequence[Ranking], k: int) -> list[float]:
    """Paired per-query P@k differences (a - b). Raises on length mismatch."""
    if len(arm_a) != len(arm_b):
        raise ValueError(f"paired arms differ in length: {len(arm_a)} vs {len(arm_b)}")
    return [
        precision_at_k(ranked_a, gold_a, k) - precision_at_k(ranked_b, gold_b, k)
        for (ranked_a, gold_a), (ranked_b, gold_b) in zip(arm_a, arm_b, strict=True)
    ]


def arm_metrics(rankings: Sequence[Ranking]) -> dict[str, float | int]:
    """Summary metric block for one arm (reuses the #344 binary metrics)."""
    return {
        "n": len(rankings),
        "p@5": round(mean_precision_at_k(rankings, 5), 4),
        "mrr@10": round(mrr_at_k(rankings, 10), 4),
    }


def evaluate_gate(
    *,
    boosted_real: Sequence[Ranking],
    unboosted: Sequence[Ranking],
    boosted_rewired: Sequence[Ranking],
    nongraph_boosted: Sequence[Ranking],
    nongraph_unboosted: Sequence[Ranking],
    seed: int,
) -> dict[str, Any]:
    """Apply the pre-declared #1213 contracts to the measured arm rankings.

    Args:
        boosted_real: Companion queries, graph boost ON, real warm graph.
        unboosted: Same queries, boost OFF (paired).
        boosted_rewired: Same queries, boost ON, degree-matched rewired graph
            (paired).
        nongraph_boosted / nongraph_unboosted: Held-out non-graph queries,
            boost ON vs OFF (paired) — the fusion-dilution check.
        seed: Bootstrap seed (pre-declared in the run config).

    Returns:
        JSON-shaped verdict: per-arm metrics, both BCa intervals, the
        non-inferiority delta, and ``verdict`` in {ship, density_artifact,
        no_effect, regression}. A clean "does not beat placebo, not shipped"
        is a valid close (the issue's acceptance wording).
    """
    vs_unboosted = paired_bca_ci(
        per_query_deltas(boosted_real, unboosted, PRIMARY_K),
        n_resamples=BCA_RESAMPLES,
        seed=seed,
    )
    vs_placebo = paired_bca_ci(
        per_query_deltas(boosted_real, boosted_rewired, PRIMARY_K),
        n_resamples=BCA_RESAMPLES,
        seed=seed,
    )
    nongraph_delta = round(
        mean_precision_at_k(nongraph_boosted, PRIMARY_K)
        - mean_precision_at_k(nongraph_unboosted, PRIMARY_K),
        4,
    )

    beats_unboosted = vs_unboosted["ci_low"] > 0.0
    beats_placebo = vs_placebo["ci_low"] > 0.0
    non_inferior = nongraph_delta >= -NON_INFERIORITY_EPSILON

    if not non_inferior:
        verdict = GATE_REGRESSION
    elif beats_unboosted and beats_placebo:
        verdict = GATE_SHIP
    elif beats_unboosted:
        verdict = GATE_DENSITY_ARTIFACT
    else:
        verdict = GATE_NO_EFFECT

    return {
        "primary_metric": f"p@{PRIMARY_K}",
        "arms": {
            "boosted_real": arm_metrics(boosted_real),
            "unboosted": arm_metrics(unboosted),
            "boosted_rewired": arm_metrics(boosted_rewired),
        },
        "vs_unboosted": vs_unboosted,
        "vs_placebo": vs_placebo,
        "nongraph": {
            "boosted_p@5": round(mean_precision_at_k(nongraph_boosted, PRIMARY_K), 4),
            "unboosted_p@5": round(mean_precision_at_k(nongraph_unboosted, PRIMARY_K), 4),
            "delta": nongraph_delta,
            "epsilon": NON_INFERIORITY_EPSILON,
            "non_inferior": non_inferior,
        },
        "contracts": {
            "beats_unboosted": beats_unboosted,
            "beats_placebo": beats_placebo,
            "non_inferior": non_inferior,
        },
        "verdict": verdict,
        "ships": verdict == GATE_SHIP,
    }
