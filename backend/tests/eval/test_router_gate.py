"""Unit tests for the #1220 router calibration gate (deterministic)."""

import pytest

from tests.eval.router_gate import (
    GATE_BUCKET_REGRESSION,
    GATE_FLIP_READY,
    GATE_NO_EFFECT,
    GATE_REGRESSION,
    GATE_UNDERPOWERED,
    MIN_BUCKET_QUERIES,
    MIN_QUERIES,
    bucket_indices,
    evaluate_router_gate,
    per_query_deltas,
)

# A "hit" ranking has the single gold doc at rank 1 (P@5 = 0.2, RR@10 = 1.0);
# a "miss" ranking does not contain it at all.
_HIT = (["g", "x1", "x2", "x3", "x4"], {"g"})
_MISS = (["x1", "x2", "x3", "x4", "x5"], {"g"})


def _arm(hits_by_index: dict[int, bool], total: int) -> list[tuple[list[str], set[str]]]:
    """Build one arm from an index → hit? map (missing indices miss)."""
    return [_HIT if hits_by_index.get(i, False) else _MISS for i in range(total)]


def _uniform(hits: int, total: int) -> list[tuple[list[str], set[str]]]:
    return [_HIT] * hits + [_MISS] * (total - hits)


def _routed(components: dict[str, list], lanes: list[str]) -> list[tuple[list[str], set[str]]]:
    """The runner's construction: query i is answered by its lane's arm."""
    return [components[lane][i] for i, lane in enumerate(lanes)]


class TestPerQueryDeltas:
    def test_precision_deltas(self) -> None:
        deltas = per_query_deltas(_uniform(60, 60), _uniform(0, 60), 5)
        assert len(deltas) == 60
        assert all(d == pytest.approx(0.2) for d in deltas)

    def test_reciprocal_rank_deltas(self) -> None:
        deltas = per_query_deltas(_uniform(60, 60), _uniform(0, 60), 10, metric="rr")
        assert all(d == pytest.approx(1.0) for d in deltas)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="differ in length"):
            per_query_deltas(_uniform(5, 10), _uniform(5, 11), 5)


class TestBucketIndices:
    def test_groups_by_lane(self) -> None:
        assert bucket_indices(["keyword", "semantic", "keyword"]) == {
            "keyword": [0, 2],
            "semantic": [1],
        }


class TestVerdicts:
    def test_flip_ready_when_router_picks_winners(self) -> None:
        """Keyword bucket: keyword component wins; semantic bucket: tie by
        construction (routed IS semantic there). Overall routed >> semantic."""
        total = 60
        lanes = ["keyword"] * 30 + ["semantic"] * 30
        semantic = _arm(dict.fromkeys(range(30, 60), True), total)  # 6/30 → 0 on kw bucket
        keyword = _arm(dict.fromkeys(range(30), True), total)
        components = {"semantic": semantic, "keyword": keyword}
        routed = _routed(components, lanes)

        result = evaluate_router_gate(routed=routed, components=components, lanes=lanes, seed=42)

        assert result["contracts"]["powered"] is True
        assert result["contracts"]["beats_semantic_overall"] is True
        assert result["contracts"]["all_buckets_beat_strongest_component"] is True
        assert result["verdict"] == GATE_FLIP_READY
        assert result["flip_ready"] is True
        assert result["buckets"]["semantic"]["beats_strongest_component"] is True  # tie passes

    def test_bucket_regression_when_hybrid_lane_loses_to_a_component(self) -> None:
        """Hybrid bucket: hybrid beats semantic (overall win) but loses to
        keyword (the strongest single component) → bucket_regression."""
        total = 60
        lanes = ["hybrid"] * 30 + ["semantic"] * 30
        semantic = _arm(dict.fromkeys(range(30, 60), True), total)  # 0 hits on hybrid bucket
        keyword = _arm(dict.fromkeys(range(25), True), total)  # 25/30 on hybrid bucket
        hybrid = _arm(dict.fromkeys(range(15), True) | dict.fromkeys(range(30, 60), True), total)
        components = {"semantic": semantic, "keyword": keyword, "hybrid": hybrid}
        routed = _routed(components, lanes)

        result = evaluate_router_gate(routed=routed, components=components, lanes=lanes, seed=42)

        assert result["contracts"]["beats_semantic_overall"] is True
        bucket = result["buckets"]["hybrid"]
        assert bucket["strongest_component"] == "keyword"
        assert bucket["beats_strongest_component"] is False
        assert result["verdict"] == GATE_BUCKET_REGRESSION
        assert result["flip_ready"] is False

    def test_no_effect_when_router_always_picks_semantic(self) -> None:
        total = 60
        lanes = ["semantic"] * total
        semantic = _uniform(30, total)
        components = {"semantic": semantic, "keyword": _uniform(0, total)}
        routed = _routed(components, lanes)

        result = evaluate_router_gate(routed=routed, components=components, lanes=lanes, seed=42)

        assert result["contracts"]["beats_semantic_overall"] is False
        assert result["verdict"] == GATE_NO_EFFECT

    def test_regression_when_routed_loses_overall(self) -> None:
        total = 60
        lanes = ["keyword"] * total
        semantic = _uniform(40, total)
        keyword = _uniform(10, total)
        components = {"semantic": semantic, "keyword": keyword}
        routed = _routed(components, lanes)

        result = evaluate_router_gate(routed=routed, components=components, lanes=lanes, seed=42)

        assert result["verdict"] == GATE_REGRESSION

    def test_underpowered_overall(self) -> None:
        total = MIN_QUERIES - 1
        lanes = ["semantic"] * total
        components = {"semantic": _uniform(10, total), "keyword": _uniform(0, total)}
        routed = _routed(components, lanes)

        result = evaluate_router_gate(routed=routed, components=components, lanes=lanes, seed=42)

        assert result["verdict"] == GATE_UNDERPOWERED

    def test_underpowered_when_a_used_bucket_is_unmeasured(self) -> None:
        """A lane the router actually uses must not graduate on n < floor."""
        total = 60
        thin = MIN_BUCKET_QUERIES - 1
        lanes = ["keyword"] * thin + ["semantic"] * (total - thin)
        semantic = _arm(dict.fromkeys(range(thin, total), True), total)
        keyword = _arm(dict.fromkeys(range(thin), True), total)
        components = {"semantic": semantic, "keyword": keyword}
        routed = _routed(components, lanes)

        result = evaluate_router_gate(routed=routed, components=components, lanes=lanes, seed=42)

        assert result["buckets"]["keyword"]["measured"] is False
        assert result["verdict"] == GATE_UNDERPOWERED

    def test_missing_component_arm_raises(self) -> None:
        with pytest.raises(ValueError, match="keyword"):
            evaluate_router_gate(
                routed=_uniform(5, 60),
                components={"semantic": _uniform(5, 60)},
                lanes=["semantic"] * 60,
                seed=42,
            )

    def test_lane_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="lanes and routed"):
            evaluate_router_gate(
                routed=_uniform(5, 60),
                components={"semantic": _uniform(5, 60), "keyword": _uniform(5, 60)},
                lanes=["semantic"] * 59,
                seed=42,
            )
