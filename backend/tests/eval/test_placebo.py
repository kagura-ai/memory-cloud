"""Deterministic tests for the Day-2 placebo kill-shot pure layer.

Everything here is infrastructure-free (no DB/Qdrant) and runs in normal CI.
The live orchestration is exercised by test_placebo_live.py (gated behind
KAGURA_EVAL_LIVE=1).
"""

from __future__ import annotations

from collections import Counter

import pytest

from tests.eval.compounding import ProbeSpec
from tests.eval.placebo import (
    Edge,
    degree_preserving_rewire,
    median_cross_topic_gold_pair_cosine,
    paired_delta_bootstrap,
    permute_gold,
    recovery_from_rankings,
)


def _edges(pairs, *, origin="hebbian", edge_type="neural_association"):
    # Distinct weight per edge so a test can verify the non-dst attribute
    # bundle travels with its OWN edge through a rewire (only dst changes).
    return [
        Edge(s, d, 0.1 * (i + 1), origin, confidence=1.0, edge_type=edge_type)
        for i, (s, d) in enumerate(pairs)
    ]


def _src_deg(edges):
    return Counter(e.src for e in edges)


def _dst_deg(edges):
    return Counter(e.dst for e in edges)


def test_rewire_preserves_degree_count_and_attribute_multisets():
    edges = _edges([("a", "b"), ("b", "c"), ("c", "d"), ("d", "a"), ("a", "c"), ("b", "d")])
    out = degree_preserving_rewire(edges, seed=7)

    assert len(out) == len(edges)
    assert _src_deg(out) == _src_deg(edges)
    assert _dst_deg(out) == _dst_deg(edges)
    # Non-dst attributes travel with their own edge (only dst changes): the
    # (src, weight, origin, confidence, edge_type) bundle multiset is invariant.
    # Distinct per-edge weights make this discriminating, not tautological.
    assert Counter((e.src, e.weight, e.origin, e.confidence, e.edge_type) for e in out) == Counter(
        (e.src, e.weight, e.origin, e.confidence, e.edge_type) for e in edges
    )


def test_rewire_has_no_self_loops_or_parallel_edges():
    edges = _edges([("a", "b"), ("b", "c"), ("c", "d"), ("d", "a"), ("a", "c"), ("b", "d")])
    out = degree_preserving_rewire(edges, seed=3)

    assert all(e.src != e.dst for e in out)
    assert len({(e.src, e.dst) for e in out}) == len(out)


def test_rewire_actually_moves_endpoints():
    edges = _edges([("a", "b"), ("b", "c"), ("c", "d"), ("d", "a"), ("a", "c"), ("b", "d")])
    out = degree_preserving_rewire(edges, seed=1)

    assert {(e.src, e.dst) for e in out} != {(e.src, e.dst) for e in edges}


def test_rewire_is_deterministic_under_seed():
    edges = _edges([("a", "b"), ("b", "c"), ("c", "d"), ("d", "a"), ("a", "c"), ("b", "d")])
    assert degree_preserving_rewire(edges, seed=42) == degree_preserving_rewire(edges, seed=42)


def test_rewire_raises_below_two_edges():
    with pytest.raises(ValueError):
        degree_preserving_rewire(_edges([("a", "b")]), seed=1)


def test_rewire_returns_pure_star_unchanged():
    """A pure out-star has exactly one graph with its degree sequence, so a
    degree-preserving rewire correctly returns it unchanged — this is correct
    behavior (a unique realization), not a failure to mix."""
    star = _edges([("h", x) for x in "abcdef"])
    out = degree_preserving_rewire(star, seed=1)
    assert {(e.src, e.dst) for e in out} == {(e.src, e.dst) for e in star}


def _probe(qid, seed_doc, companions):
    return ProbeSpec(
        query_id=qid,
        text=qid,
        bucket="cross-source",
        seed_doc=seed_doc,
        companion_docs=tuple(companions),
    )


def test_permute_gold_preserves_sizes_and_is_a_derangement_within_a_group():
    probes = (
        _probe("q1", "s1", ["c1a", "c1b"]),
        _probe("q2", "s2", ["c2a", "c2b"]),
        _probe("q3", "s3", ["c3a", "c3b"]),
    )
    out = permute_gold(probes, seed=5)

    for p in probes:
        assert len(out[p.query_id]) == len(p.companion_docs)  # size preserved
        assert set(out[p.query_id]) != set(p.companion_docs)  # not own gold


def test_permute_gold_is_deterministic_under_seed():
    probes = (
        _probe("q1", "s1", ["c1a", "c1b"]),
        _probe("q2", "s2", ["c2a", "c2b"]),
        _probe("q3", "s3", ["c3a", "c3b"]),
    )
    assert permute_gold(probes, seed=9) == permute_gold(probes, seed=9)


def test_permute_gold_singleton_size_class_draws_excluding_own():
    # q3 has a unique companion count (1) -> singleton class -> pool-draw path.
    probes = (
        _probe("q1", "s1", ["c1a", "c1b"]),
        _probe("q2", "s2", ["c2a", "c2b"]),
        _probe("q3", "s3", ["c3a"]),
    )
    out = permute_gold(probes, seed=2)

    assert len(out["q3"]) == 1
    assert "c3a" not in out["q3"]
    assert "s3" not in out["q3"]


def test_permute_gold_raises_when_singleton_pool_too_small():
    # q2's size-3 singleton class cannot be satisfied: after excluding q2's own
    # 3 companions + seed, only {s1, c1a} (2 < 3) remain in the foreign pool.
    probes = (
        _probe("q1", "s1", ["c1a"]),
        _probe("q2", "s2", ["c2a", "c2b", "c2c"]),
    )
    with pytest.raises(ValueError, match="corpus too small"):
        permute_gold(probes, seed=1)


def test_bootstrap_point_estimate_is_the_paired_mean():
    a = [1.0, 0.8, 0.6, 0.4]
    b = [0.0, 0.2, 0.1, 0.0]
    out = paired_delta_bootstrap(a, b, seed=1, resamples=2000)
    assert out["delta"] == pytest.approx(
        sum(x - y for x, y in zip(a, b, strict=True)) / len(a), abs=1e-9
    )
    assert out["n"] == 4
    assert out["lo"] <= out["delta"] <= out["hi"]
    assert out["lo"] < out["hi"]  # non-degenerate data -> real interval width (not a stub)


def test_bootstrap_is_deterministic_under_seed():
    a = [0.9, 0.7, 0.5, 0.3, 0.6]
    b = [0.1, 0.0, 0.2, 0.1, 0.0]
    assert paired_delta_bootstrap(a, b, seed=11, resamples=1500) == paired_delta_bootstrap(
        a, b, seed=11, resamples=1500
    )


def test_bootstrap_interval_reacts_to_seed():
    # Different seeds must produce different resampled intervals — guards against
    # a degenerate implementation that ignores the resampling loop.
    a = [0.9, 0.7, 0.5, 0.3, 0.6]
    b = [0.1, 0.0, 0.2, 0.1, 0.0]
    out1 = paired_delta_bootstrap(a, b, seed=1, resamples=2000)
    out2 = paired_delta_bootstrap(a, b, seed=2, resamples=2000)
    assert out1["delta"] == out2["delta"]  # point estimate is seed-independent
    assert (out1["lo"], out1["hi"]) != (out2["lo"], out2["hi"])  # interval is seed-driven


def test_bootstrap_raises_on_length_mismatch_or_empty():
    with pytest.raises(ValueError):
        paired_delta_bootstrap([0.1, 0.2], [0.1], seed=1)
    with pytest.raises(ValueError):
        paired_delta_bootstrap([], [], seed=1)


def test_median_cross_topic_ignores_same_source_pairs():
    doc_vec = {"a": [1.0], "b": [2.0], "c": [3.0]}

    # cosine_fn returns the product of the single components (a stand-in metric)
    def cos(u, v):
        return u[0] * v[0]

    source = {"a": "memory", "b": "resource", "c": "memory"}
    gold_pairs = {frozenset(("a", "b")), frozenset(("a", "c")), frozenset(("b", "c"))}
    # cross-topic pairs: a-b (1*2=2), b-c (2*3=6); a-c is same-source -> skipped
    out = median_cross_topic_gold_pair_cosine(doc_vec, gold_pairs, source, cosine_fn=cos)
    assert out == pytest.approx((2.0 + 6.0) / 2)


def test_median_cross_topic_returns_none_when_no_cross_topic_pair():
    doc_vec = {"a": [1.0], "b": [2.0]}

    def cos(u, v):
        return u[0] * v[0]

    source = {"a": "memory", "b": "memory"}
    gold_pairs = {frozenset(("a", "b"))}
    assert median_cross_topic_gold_pair_cosine(doc_vec, gold_pairs, source, cosine_fn=cos) is None


def test_median_cross_topic_skips_pair_missing_a_vector():
    doc_vec = {"a": [1.0]}  # b has no vector -> the only gold pair is skipped

    def cos(u, v):
        return u[0] * v[0]

    source = {"a": "memory", "b": "resource"}
    gold_pairs = {frozenset(("a", "b"))}
    assert median_cross_topic_gold_pair_cosine(doc_vec, gold_pairs, source, cosine_fn=cos) is None


def test_median_cross_topic_skips_degenerate_single_element_pair():
    # A gold "pair" that collapsed to one doc (seed == companion) must be skipped, not crash.
    doc_vec = {"a": [1.0]}

    def cos(u, v):
        return u[0] * v[0]

    source = {"a": "memory"}
    gold_pairs = {frozenset(("a",))}
    assert median_cross_topic_gold_pair_cosine(doc_vec, gold_pairs, source, cosine_fn=cos) is None


def test_recovery_from_rankings_scores_against_the_gold_map():
    # probe q1: gold {b, c}; ranked [b, x, c] -> recovery@10 = 2/2 = 1.0
    # probe q2: gold {d};    ranked [x, y]    -> recovery@10 = 0/1 = 0.0
    rankings = [("q1", ["b", "x", "c"]), ("q2", ["x", "y"])]
    gold_map = {"q1": ("b", "c"), "q2": ("d",)}
    out = recovery_from_rankings(rankings, gold_map)

    assert out["n"] == 2
    assert out["recovery@10"] == pytest.approx((1.0 + 0.0) / 2)
    assert out["per_probe@10"] == [pytest.approx(1.0), pytest.approx(0.0)]
