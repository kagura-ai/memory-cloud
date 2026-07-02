"""Deterministic tests for the Day-2 placebo kill-shot pure layer.

Everything here is infrastructure-free (no DB/Qdrant) and runs in normal CI.
The live orchestration is exercised by test_placebo_live.py (gated behind
KAGURA_EVAL_LIVE=1).
"""

from __future__ import annotations

from collections import Counter

import pytest

from tests.eval.compounding import ProbeSpec
from tests.eval.placebo import Edge, degree_preserving_rewire, permute_gold


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
