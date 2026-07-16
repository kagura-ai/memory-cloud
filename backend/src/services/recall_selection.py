"""Evaluation-only recall selection with truthful inclusion probabilities.

The production ranker remains the source of the ordered candidate pool.  This
module only chooses the returned ``top_k`` from that already-ranked pool; it
does not score, hydrate, or authorize memories.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RecallSelectionConfig:
    """Registered selection policy for one reproducible evaluation call."""

    seed: int
    exploration_floor: float
    candidate_pool_k: int


@dataclass(frozen=True)
class RecallSelectionPlan:
    """Selected identities plus exact marginal inclusion probabilities."""

    selected_ids: tuple[str, ...]
    selection_probabilities: dict[str, float]
    policy: dict[str, object]


def plan_recall_selection(
    eligible_ids: tuple[str, ...],
    *,
    top_k: int,
    config: RecallSelectionConfig,
) -> RecallSelectionPlan:
    """Select from a ranked eligible pool and report exact inclusion chances.

    A non-zero floor mixes the deterministic top-k policy with a uniform
    sample-without-replacement of the same size.  For ``n`` candidates and
    ``m=min(k,n)``, mixing with probability ``floor*n/m`` gives every
    deterministic non-top-k candidate marginal inclusion probability exactly
    ``floor``.  The top-k candidates receive the remaining deterministic mass.
    """

    if top_k < 1:
        raise ValueError("top_k must be positive")
    if config.candidate_pool_k < top_k:
        raise ValueError("candidate_pool_k must be at least top_k")
    if not 0.0 <= config.exploration_floor <= 1.0:
        raise ValueError("exploration_floor must be between 0 and 1")

    # One memory can be addressable by both its row id and embedding point id.
    # The caller supplies canonical row UUIDs; preserve the first ranked hit.
    ranked = tuple(dict.fromkeys(eligible_ids))
    n = len(ranked)
    selected_count = min(top_k, n)
    deterministic = ranked[:selected_count]

    mixture_probability = 0.0
    selected = deterministic
    if n and selected_count:
        maximum_feasible_floor = selected_count / n
        if config.exploration_floor > maximum_feasible_floor + 1e-12:
            raise ValueError(
                "exploration_floor exceeds the maximum feasible floor "
                f"{maximum_feasible_floor:.12g} for top_k={top_k}, eligible_count={n}"
            )
        if config.exploration_floor > 0.0 and selected_count < n:
            # The feasibility guard above admits floors up to
            # maximum_feasible_floor + 1e-12, where floor*n/m computes to just
            # over 1.0. Clamp so the reported marginals stay mathematically
            # consistent with the actual procedure (p=1 → always uniform arm).
            mixture_probability = min(1.0, config.exploration_floor * n / selected_count)
            rng = random.Random(config.seed)
            if rng.random() < mixture_probability:
                chosen = set(rng.sample(range(n), selected_count))
                # Uniformly choose the subset, then retain production rank order
                # inside it.  This makes replay stable without changing marginals.
                selected = tuple(value for index, value in enumerate(ranked) if index in chosen)

    if n <= selected_count:
        probabilities = dict.fromkeys(ranked, 1.0)
    elif config.exploration_floor == 0.0:
        selected_set = set(deterministic)
        probabilities = {
            memory_id: 1.0 if memory_id in selected_set else 0.0 for memory_id in ranked
        }
    else:
        deterministic_set = set(deterministic)
        top_probability = 1.0 - mixture_probability + config.exploration_floor
        probabilities = {
            memory_id: (
                top_probability if memory_id in deterministic_set else config.exploration_floor
            )
            for memory_id in ranked
        }

    minimum_probability = min(probabilities.values()) if probabilities else None
    policy_name = (
        "deterministic_uniform_mixture_v1"
        if config.exploration_floor > 0.0 and selected_count < n
        else "deterministic_top_k_v1"
    )
    return RecallSelectionPlan(
        selected_ids=selected,
        selection_probabilities=probabilities,
        policy={
            "name": policy_name,
            "version": 1,
            "evaluation_seed": config.seed,
            "replay_identity": f"bootstrap-recall-v1:{config.seed}",
            "exploration_floor": config.exploration_floor,
            "uniform_mixture_probability": mixture_probability,
            "candidate_pool_k": config.candidate_pool_k,
            "eligible_count": n,
            "selected_count": selected_count,
            "minimum_selection_probability": minimum_probability,
        },
    )
