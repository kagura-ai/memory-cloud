"""Deterministic retrieval-quality metrics (Issue #344).

Pure functions over a ranked list of retrieved doc IDs and a set of relevant
(gold) doc IDs. No I/O, no infra — unit-tested in ``test_metrics.py`` and reused
by the live-stack harness in ``test_retrieval_quality.py``.

All metrics treat relevance as **binary** (a doc is relevant or not), matching
the corpus label model. ``ranked`` is the system's output order (most relevant
first); ``relevant`` is the gold set.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-``k`` retrieved docs that are relevant.

    Denominator is ``k`` (not ``min(k, len(ranked))``) — under-retrieving is a
    real quality loss and should depress the score, not be hidden. Returns 0.0
    for ``k <= 0``.
    """
    if k <= 0:
        return 0.0
    top = ranked[:k]
    hits = sum(1 for doc_id in top if doc_id in relevant)
    return hits / k


def reciprocal_rank_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Reciprocal rank of the first relevant doc within the top ``k`` (else 0.0).

    The per-query term of MRR@k. Rank is 1-based, so a relevant doc at position
    1 scores 1.0, at position 2 scores 0.5, etc.
    """
    if k <= 0:
        return 0.0
    for idx, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in relevant:
            return 1.0 / idx
    return 0.0


def mrr_at_k(
    rankings: Sequence[tuple[Sequence[str], set[str]]],
    k: int,
) -> float:
    """Mean reciprocal rank @k across a set of (ranked, relevant) query results.

    Returns 0.0 for an empty input rather than raising — an empty bucket is a
    corpus-construction problem the schema test catches, not a metric error.
    """
    if not rankings:
        return 0.0
    return sum(reciprocal_rank_at_k(r, rel, k) for r, rel in rankings) / len(rankings)


def mean_precision_at_k(
    rankings: Sequence[tuple[Sequence[str], set[str]]],
    k: int,
) -> float:
    """Mean P@k across a set of (ranked, relevant) query results."""
    if not rankings:
        return 0.0
    return sum(precision_at_k(r, rel, k) for r, rel in rankings) / len(rankings)


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Normalized discounted cumulative gain @k under binary relevance.

    DCG@k discounts each relevant hit by ``1/log2(rank+1)``; the ideal DCG
    places relevant docs in the top ``min(k, len(relevant))`` positions, so a
    ranking that fills the top-``k`` with relevant docs scores 1.0 even when
    the gold set is larger than ``k``. Returns 0.0 for ``k <= 0`` or an empty
    gold set (a corpus-construction problem, not a metric error).
    """
    if k <= 0 or not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(idx + 1)
        for idx, doc_id in enumerate(ranked[:k], start=1)
        if doc_id in relevant
    )
    ideal = sum(1.0 / math.log2(idx + 1) for idx in range(1, min(k, len(relevant)) + 1))
    return dcg / ideal


def mean_ndcg_at_k(
    rankings: Sequence[tuple[Sequence[str], set[str]]],
    k: int,
) -> float:
    """Mean nDCG@k across a set of (ranked, relevant) query results."""
    if not rankings:
        return 0.0
    return sum(ndcg_at_k(r, rel, k) for r, rel in rankings) / len(rankings)


def source_recall_share(ranked_sources: Sequence[str], k: int) -> dict[str, float]:
    """Share of the top-``k`` results contributed by each source kind.

    ``ranked_sources`` is the per-position source label (e.g. ``"memory"`` /
    ``"resource"``) aligned with the ranking. Used to surface the #new-A drift
    signal (memory_share vs resource_share). Returns shares over ``min(k, len)``
    so an under-filled result set reports the realized mix, not a diluted one.
    """
    top = list(ranked_sources[:k])
    if not top:
        return {}
    counts: dict[str, int] = {}
    for src in top:
        counts[src] = counts.get(src, 0) + 1
    total = len(top)
    return {src: n / total for src, n in counts.items()}
