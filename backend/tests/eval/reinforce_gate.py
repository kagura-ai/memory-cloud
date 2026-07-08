"""Pure logic for the #1069 reinforce ON-vs-OFF eval gate.

Tier-C companion to the #344 static harness (``runner.py``) and the #969
compounding harness (``replay_runner.py``). The bounded reinforce re-rank (#1048)
is **default-ON since #1207** (new/materialized config rows start enabled; only a
stored explicit ``false`` opts out); this module defines the gate that decides
whether enabling it on an explicitly-opted-out context is safe — the eval the
rollout (#1069) was gated on:

- **current-fact** queries (the canonical answer has been adopted + confirmed
  helpful) must **improve** — that is the whole point of reinforce;
- **rare-but-correct / historical** queries (gold is a zero-adoption memory) must
  **not regress** — reinforce must not bury the long tail;
- **popularity bias is measured, not assumed away** — the zero-adoption surfacing
  rate (share of the top-k slots held by never-adopted memories) must be retained,
  so the cold-start floor still lets brand-new memories through.

Everything here is deterministic and infrastructure-free (unit-tested in
``test_reinforce_gate.py``). The live ingest → seed-adoption → OFF/ON → score
orchestration that actually drives ``recall()`` lives in
``tests.eval.reinforce_runner``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from tests.eval.metrics import mean_ndcg_at_k, mean_precision_at_k, mrr_at_k

#: Query strata (Query.population in the reinforce corpus).
POPULATION_CURRENT_FACT = "current_fact"
POPULATION_RARE = "rare"
POPULATIONS = (POPULATION_CURRENT_FACT, POPULATION_RARE)

#: The metric the gate decides on. MRR@10 is the most sensitive to a single
#: canonical answer moving a few ranks — exactly the reinforce effect.
PRIMARY_METRIC = "mrr@10"

#: One query result: the ranked doc-ids and its gold set. Matches the shape the
#: live runner builds (and the #344 metrics module consumes).
Ranking = tuple[Sequence[str], set[str]]


def population_metrics(rankings: Sequence[Ranking]) -> dict[str, float | int]:
    """The metric block for one population (reuses the #344 binary-relevance
    metrics). ``n`` is the query count (int); the rest are the gate-comparable
    float scores.
    """
    return {
        "n": len(rankings),
        "p@5": round(mean_precision_at_k(rankings, 5), 4),
        "mrr@10": round(mrr_at_k(rankings, 10), 4),
        "ndcg@10": round(mean_ndcg_at_k(rankings, 10), 4),
    }


def zero_adoption_surfacing_rate(
    rankings: Sequence[Ranking], adopted_ids: set[str], k: int
) -> float:
    """Share of the realized top-``k`` slots, across all queries, filled by a
    NEVER-ADOPTED (zero-adoption) doc.

    The popularity-bias guard from #1069: if enabling reinforce makes adopted
    memories crowd the surface, this rate falls vs the OFF arm — brand-new
    memories stop getting through. Counts realized slots (``min(k, len)``) so an
    under-filled result set is measured honestly, not diluted by empty positions.
    Returns 0.0 for an empty input.
    """
    surfaced = 0
    zero = 0
    for ranked, _relevant in rankings:
        top = list(ranked[:k])
        surfaced += len(top)
        zero += sum(1 for d in top if d not in adopted_ids)
    return round(zero / surfaced, 4) if surfaced else 0.0


@dataclass(frozen=True)
class GateThresholds:
    """The rollout gate's decision thresholds (all on ``PRIMARY_METRIC``).

    Conservative defaults: the gate PASSES when reinforce does not *regress* the
    current-fact population, does not regress the rare population beyond a small
    tolerance, and retains most of the zero-adoption surfacing rate. An
    experiment wanting a stronger "improves" claim can raise
    ``min_current_fact_uplift`` above 0 (the verdict always reports the strict
    ``improved`` flag separately, so the distinction is never hidden).
    """

    #: ON − OFF on the current-fact population must be >= this (0.0 = no regression).
    min_current_fact_uplift: float = 0.0
    #: OFF − ON on the rare population must be <= this (the long-tail no-regress band).
    max_rare_regression: float = 0.02
    #: ON zero-adoption surfacing must be >= this fraction of OFF (cold-start retention).
    min_zero_adoption_retention: float = 0.90


@dataclass(frozen=True)
class ArmBlock:
    """One arm's scored result: per-population metric blocks + the corpus-level
    zero-adoption surfacing rate. Both the OFF and ON arms are this shape."""

    populations: dict[str, dict[str, float | int]]
    zero_adoption_surfacing_rate: float


def _primary(arm: ArmBlock, population: str) -> float:
    block = arm.populations.get(population)
    if block is None:
        raise ValueError(f"arm is missing the {population!r} population block")
    return float(block[PRIMARY_METRIC])


def evaluate_reinforce_gate(
    off: ArmBlock,
    on: ArmBlock,
    *,
    thresholds: GateThresholds | None = None,
) -> dict[str, Any]:
    """Compare an OFF-arm and ON-arm block and decide the rollout gate.

    Returns a verdict dict with the per-population deltas on ``PRIMARY_METRIC``,
    the popularity-bias retention, a boolean ``passed``, and human-readable
    ``reasons`` for every failed condition. Pure — no I/O, no global state.
    """
    th = thresholds or GateThresholds()

    cf_off, cf_on = _primary(off, POPULATION_CURRENT_FACT), _primary(on, POPULATION_CURRENT_FACT)
    rare_off, rare_on = _primary(off, POPULATION_RARE), _primary(on, POPULATION_RARE)
    za_off = off.zero_adoption_surfacing_rate
    za_on = on.zero_adoption_surfacing_rate

    uplift = cf_on - cf_off
    regression = rare_off - rare_on
    # If OFF surfaced no zero-adoption docs at all there is nothing to "retain" —
    # report None and do not fail the gate on a 0/0 ratio.
    retention = round(za_on / za_off, 4) if za_off > 0 else None

    reasons: list[str] = []
    if uplift < th.min_current_fact_uplift - 1e-9:
        reasons.append(
            f"current-fact {PRIMARY_METRIC} uplift {uplift:+.4f} < required "
            f"{th.min_current_fact_uplift:+.4f}"
        )
    if regression > th.max_rare_regression + 1e-9:
        reasons.append(
            f"rare {PRIMARY_METRIC} regressed {regression:+.4f} > allowed "
            f"{th.max_rare_regression:+.4f}"
        )
    if retention is not None and retention < th.min_zero_adoption_retention - 1e-9:
        reasons.append(
            f"zero-adoption surfacing retention {retention:.4f} < required "
            f"{th.min_zero_adoption_retention:.4f}"
        )

    return {
        "primary_metric": PRIMARY_METRIC,
        "current_fact": {
            "off": round(cf_off, 4),
            "on": round(cf_on, 4),
            "uplift": round(uplift, 4),
            "improved": uplift > 1e-9,
        },
        "rare": {
            "off": round(rare_off, 4),
            "on": round(rare_on, 4),
            "regression": round(regression, 4),
        },
        "zero_adoption_surfacing": {
            "off": round(za_off, 4),
            "on": round(za_on, 4),
            "retention": retention,
        },
        "thresholds": asdict(th),
        "passed": not reasons,
        "reasons": reasons,
    }
