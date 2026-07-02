"""Deterministic tests for the Day-2 placebo kill-shot pure layer.

Everything here is infrastructure-free (no DB/Qdrant) and runs in normal CI.
The live orchestration is exercised by test_placebo_live.py (gated behind
KAGURA_EVAL_LIVE=1).
"""

from __future__ import annotations

from collections import Counter

import pytest

from tests.eval.placebo import Edge, degree_preserving_rewire


def _edges(pairs, *, weight=0.5, origin="hebbian", edge_type="neural_association"):
    return [Edge(s, d, weight, origin, confidence=1.0, edge_type=edge_type) for s, d in pairs]


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
    assert Counter((e.weight, e.origin, e.confidence, e.edge_type) for e in out) == Counter(
        (e.weight, e.origin, e.confidence, e.edge_type) for e in edges
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
