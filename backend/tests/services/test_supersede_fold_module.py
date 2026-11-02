"""The pure half of the supersede fold, without MemoryService (#2257 item 5).

``services/supersede_fold.py`` (#2131) is driven end to end through the
service in ``test_supersede_fold.py`` with a mocked resolver. These cases pin
each function on its own: plain rows, no database, no awaits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from models.schemas import SupersedeCandidate
from services.reranker_service import RERANK_UNSCORED_KEY
from services.supersede_fold import (
    FOLD_RANK_KEYS,
    FoldNodes,
    fold_linked_versions,
    fold_links,
    fold_nodes,
    judged_supersedes,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _mem(age_days: int, *, verdict: str | None = None) -> SimpleNamespace:
    """A hydrated row; a larger ``age_days`` is an OLDER version."""
    candidate = None
    if verdict is not None:
        candidate = {
            "memory_id": str(uuid4()),
            "similarity": 0.8,
            "verification": {"verdict": verdict, "confidence": 0.9},
        }
    return SimpleNamespace(
        id=uuid4(), created_at=T0 - timedelta(days=age_days), supersede_candidate=candidate
    )


def _pool(*rows: tuple[SimpleNamespace, float]) -> tuple[list[dict], dict[str, SimpleNamespace]]:
    search_results = [{"id": str(m.id), "score": score, "hybrid_score": score} for m, score in rows]
    return search_results, {str(m.id): m for m, _ in rows}


def _cand(target_id: UUID, *, verified: bool | None = True) -> SupersedeCandidate:
    return SupersedeCandidate(
        memory_id=target_id, summary="older", similarity=0.8, verified=verified
    )


def _nodes(pool=(), bridges=()) -> FoldNodes:
    pool_map = {m.id: m for m in pool}
    bridge_map = {m.id: m for m in bridges}
    nodes = {**pool_map, **bridge_map}
    return FoldNodes(pool=pool_map, bridges=bridge_map, nodes=nodes, judged={})


class TestJudgedSupersedes:
    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            (None, False),
            ({"memory_id": "x", "similarity": 0.9}, False),
            ({"memory_id": "x", "verification": {"verdict": "parallel"}}, False),
            ({"memory_id": "x", "verification": {"verdict": "unclear"}}, False),
            ({"memory_id": "x", "verification": {"verdict": "supersedes"}}, True),
        ],
    )
    def test_only_a_supersedes_verdict_counts(self, candidate, expected):
        assert judged_supersedes(SimpleNamespace(supersede_candidate=candidate)) is expected


class TestFoldNodes:
    def test_pool_bridges_nodes_and_judged(self):
        judged = _mem(0, verdict="supersedes")
        plain = _mem(1)
        bridge = _mem(2, verdict="supersedes")
        search_results, memories = _pool((judged, 0.9), (plain, 0.8))
        search_results.append({"id": "not-hydrated", "score": 0.7})
        memories[str(bridge.id)] = bridge

        nodes = fold_nodes(search_results, memories, {bridge.id: judged.id})

        assert set(nodes.pool) == {judged.id, plain.id}
        assert set(nodes.bridges) == {bridge.id}
        assert set(nodes.nodes) == {judged.id, plain.id, bridge.id}
        assert set(nodes.judged) == {judged.id, bridge.id}

    def test_a_shadowed_id_still_in_the_pool_is_not_a_bridge(self):
        row = _mem(0)
        search_results, memories = _pool((row, 0.9))

        nodes = fold_nodes(search_results, memories, {row.id: uuid4()})

        assert set(nodes.pool) == {row.id}
        assert nodes.bridges == {}

    def test_a_row_keyed_twice_is_one_node(self):
        # The hydrated map keys a row by its id and its point id (#972).
        row = _mem(0)
        search_results, memories = _pool((row, 0.9))
        memories["point-id"] = row
        search_results.append({"id": "point-id", "score": 0.5})

        nodes = fold_nodes(search_results, memories, None)

        assert list(nodes.pool) == [row.id]


class TestFoldLinks:
    def test_a_shadow_edge_links_only_into_a_bridge_from_a_node(self):
        head, bridge, outsider = _mem(0), _mem(1), _mem(2)
        fold = _nodes(pool=[head], bridges=[bridge])

        links = fold_links(fold, {}, {bridge.id: head.id, head.id: outsider.id, uuid4(): head.id})

        assert links == {head.id: {bridge.id}}

    def test_verified_candidates_link_and_the_rest_do_not(self):
        newer, older, other, gone = _mem(0), _mem(1), _mem(2), uuid4()
        fold = _nodes(pool=[newer, older, other])

        links = fold_links(
            fold,
            {
                newer.id: _cand(older.id),
                other.id: _cand(older.id, verified=None),  # cosine-only
                older.id: _cand(gone),  # target outside the nodes
            },
            None,
        )

        assert links == {newer.id: {older.id}}

    def test_a_self_target_is_ignored(self):
        row = _mem(0)

        assert fold_links(_nodes(pool=[row]), {row.id: _cand(row.id)}, None) == {}


class TestFoldLinkedVersions:
    @pytest.mark.parametrize("order", ["oldest_first", "newest_first"])
    def test_a_chain_folds_under_the_newest_in_either_order(self, order):
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        rows = [(v1, 0.9), (v2, 0.8), (v3, 0.7)]
        if order == "newest_first":
            rows = [(v3, 0.9), (v2, 0.8), (v1, 0.7)]
        search_results, memories = _pool(*rows)
        pool = {m.id: m for m in (v1, v2, v3)}

        fold_map = fold_linked_versions(
            search_results, memories, pool, {v3.id: {v2.id}, v2.id: {v1.id}}
        )

        assert fold_map == {v3.id: [v2.id, v1.id]}
        assert [r["id"] for r in search_results] == [str(v3.id)]
        assert search_results[0]["score"] == 0.9

    def test_the_head_takes_the_best_rows_place_and_rank_keys(self):
        older, other, newer = _mem(1), _mem(5), _mem(0)
        search_results = [
            {"id": str(older.id), "score": 0.9, "rerank_score": 0.95, "semantic_score_raw": 0.6},
            {"id": str(other.id), "score": 0.85},
            {
                "id": str(newer.id),
                "score": 0.5,
                "hybrid_score": 0.5,
                RERANK_UNSCORED_KEY: True,
                "semantic_score_raw": 0.7,
                "extra": "kept",
            },
        ]
        memories = {str(m.id): m for m in (older, other, newer)}
        pool = {m.id: m for m in (older, other, newer)}

        fold_linked_versions(search_results, memories, pool, {newer.id: {older.id}})

        head, rest = search_results
        assert head["id"] == str(newer.id) and rest["id"] == str(other.id)
        assert head["score"] == 0.9
        assert head["rerank_score"] == 0.95
        # Rank keys the best row lacks are dropped, other keys stay.
        for key in set(FOLD_RANK_KEYS) - {"score", "rerank_score"}:
            assert key not in head
        assert head["extra"] == "kept"
        # The raw cosine is the group maximum, not the best row's.
        assert head["semantic_score_raw"] == 0.7

    def test_two_newer_of_one_older_both_stay(self):
        older, first, second = _mem(2), _mem(1), _mem(0)
        search_results, memories = _pool((older, 0.9), (first, 0.8), (second, 0.7))
        pool = {m.id: m for m in (older, first, second)}

        fold_map = fold_linked_versions(
            search_results, memories, pool, {first.id: {older.id}, second.id: {older.id}}
        )

        assert fold_map == {second.id: [older.id]}
        assert [r["id"] for r in search_results] == [str(second.id), str(first.id)]

    def test_a_cycle_without_a_head_folds_nothing(self):
        a, b = _mem(1), _mem(0)
        search_results, memories = _pool((a, 0.9), (b, 0.8))
        before = [dict(r) for r in search_results]

        with patch("services.supersede_fold.logger") as log:
            fold_map = fold_linked_versions(
                search_results, memories, {a.id: a, b.id: b}, {a.id: {b.id}, b.id: {a.id}}
            )

        assert fold_map == {}
        assert search_results == before
        log.info.assert_not_called()

    def test_no_links_returns_empty_without_logging(self):
        a, b = _mem(1), _mem(0)
        search_results, memories = _pool((a, 0.9), (b, 0.8))

        with patch("services.supersede_fold.logger") as log:
            assert fold_linked_versions(search_results, memories, {a.id: a, b.id: b}, {}) == {}

        assert len(search_results) == 2
        log.info.assert_not_called()

    def test_a_fold_logs_how_many_rows_and_groups(self):
        v1, v2, v3, w1, w2 = _mem(2), _mem(1), _mem(0), _mem(4), _mem(3)
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7), (w1, 0.6), (w2, 0.5))
        pool = {m.id: m for m in (v1, v2, v3, w1, w2)}

        with patch("services.supersede_fold.logger") as log:
            fold_linked_versions(
                search_results,
                memories,
                pool,
                {v3.id: {v2.id}, v2.id: {v1.id}, w2.id: {w1.id}},
            )

        log.info.assert_called_once_with("supersede_fold_applied", folded=3, groups=2)

    def test_a_link_through_a_bridge_folds_the_pool_row_beyond_it(self):
        # v2 is a bridge (not in the pool): it carries the link v3→v2→v1.
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        search_results, memories = _pool((v1, 0.9), (v3, 0.8))

        fold_map = fold_linked_versions(
            search_results, memories, {v1.id: v1, v3.id: v3}, {v3.id: {v2.id}, v2.id: {v1.id}}
        )

        assert fold_map == {v3.id: [v1.id]}
        assert [r["id"] for r in search_results] == [str(v3.id)]
