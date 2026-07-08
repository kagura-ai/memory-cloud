"""Tests for MemoryService._maybe_graph_boost (#1213 flagged experiment).

Pins the acceptance contract: flag default OFF means bit-identical recall
behavior (no edge query, no reorder), the boost is bounded multiplicative
([1, 1+b], boost-only), it composes with the reinforce re-rank through the
``_rerank_factor`` stamp, and any failure preserves the original ranking
(fail-safe).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.memory_service import MemoryService

_CTX = uuid4()


def _service(edge_rows=None, execute_error=None) -> MemoryService:
    svc = MemoryService.__new__(MemoryService)
    svc.db = AsyncMock()
    if execute_error is not None:
        svc.db.execute = AsyncMock(side_effect=execute_error)
    else:
        result = MagicMock()
        result.all.return_value = edge_rows or []
        svc.db.execute = AsyncMock(return_value=result)
    return svc


def _pool(*scores: float):
    """Build (search_results, memories) with stable per-index memory ids."""
    memories = {}
    results = []
    for i, score in enumerate(scores):
        mem_id = uuid4()
        key = f"r{i}"
        memories[key] = SimpleNamespace(id=mem_id)
        results.append({"id": key, "hybrid_score": score})
    return results, memories


def _mid(memories, key):
    return memories[key].id


class TestOffPath:
    @pytest.mark.asyncio
    async def test_flag_unset_is_bit_identical(self, monkeypatch) -> None:
        """Acceptance criterion 1: default off -> no DB query, no reorder."""
        monkeypatch.delenv("KAGURA_GRAPH_BOOST_ENABLED", raising=False)
        svc = _service()
        results, memories = _pool(0.9, 0.8, 0.7)
        before = [r["id"] for r in results]

        await svc._maybe_graph_boost(results, memories, _CTX, "u1", top_k=3)

        assert [r["id"] for r in results] == before
        svc.db.execute.assert_not_called()
        assert all("_rerank_factor" not in r for r in results)

    @pytest.mark.asyncio
    async def test_flag_false_is_bit_identical(self, monkeypatch) -> None:
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "false")
        svc = _service()
        results, memories = _pool(0.9, 0.8)
        await svc._maybe_graph_boost(results, memories, _CTX, "u1", top_k=2)
        svc.db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_cross_context_none_is_noop(self, monkeypatch) -> None:
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "true")
        svc = _service()
        results, memories = _pool(0.9, 0.8)
        await svc._maybe_graph_boost(results, memories, None, "u1", top_k=2)
        svc.db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_user_id_is_noop(self, monkeypatch) -> None:
        """Per-user scope (gate2/CSO): no caller identity, no boost."""
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "true")
        svc = _service()
        results, memories = _pool(0.9, 0.8)
        await svc._maybe_graph_boost(results, memories, _CTX, None, top_k=2)
        svc.db.execute.assert_not_called()


class TestBoost:
    @pytest.mark.asyncio
    async def test_connected_candidate_overtakes_within_bound(self, monkeypatch) -> None:
        """A strongly connected runner-up overtakes a close leader — but the
        factor is capped, so a distant leader cannot be overtaken."""
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "true")
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_MAX", "0.15")
        results, memories = _pool(0.90, 0.88, 0.5)
        # r1 <-> r2 edge: r1 and r2 each get conn=2.0 (max), r0 isolated.
        svc = _service(edge_rows=[(_mid(memories, "r1"), _mid(memories, "r2"), 2.0)])

        await svc._maybe_graph_boost(results, memories, _CTX, "u1", top_k=3)

        # r1: 0.88 * 1.15 = 1.012 > r0: 0.90 * 1.0 — overtakes.
        # r2: 0.5 * 1.15 = 0.575 < 0.90 — the cap keeps it below the leader.
        assert [r["id"] for r in results] == ["r1", "r0", "r2"]

    @pytest.mark.asyncio
    async def test_isolated_candidates_keep_factor_one(self, monkeypatch) -> None:
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "true")
        results, memories = _pool(0.9, 0.8)
        svc = _service(edge_rows=[(_mid(memories, "r0"), _mid(memories, "r1"), 1.0)])

        await svc._maybe_graph_boost(results, memories, _CTX, "u1", top_k=2)

        # Both connected equally -> both factor 1+b -> order unchanged, and
        # the stamp records the applied factor.
        assert [r["id"] for r in results] == ["r0", "r1"]
        assert results[0]["_rerank_factor"] == pytest.approx(1.15)

    @pytest.mark.asyncio
    async def test_no_edges_means_no_reorder_and_no_stamp(self, monkeypatch) -> None:
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "true")
        svc = _service(edge_rows=[])
        results, memories = _pool(0.9, 0.8)

        await svc._maybe_graph_boost(results, memories, _CTX, "u1", top_k=2)

        assert [r["id"] for r in results] == ["r0", "r1"]
        assert all("_rerank_factor" not in r for r in results)

    @pytest.mark.asyncio
    async def test_composes_with_reinforce_factor(self, monkeypatch) -> None:
        """A prior bounded re-rank's stamp is honored: the graph boost sorts
        by base x reinforce_factor x graph_factor, never by raw base."""
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "true")
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_MAX", "0.15")
        results, memories = _pool(0.90, 0.88, 0.86)
        # Reinforce demoted r0 and boosted r2 (stamped factors).
        results[0]["_rerank_factor"] = 0.85
        results[2]["_rerank_factor"] = 1.15
        # No edges at all would return early — give r1 a weak edge to r2.
        svc = _service(edge_rows=[(_mid(memories, "r1"), _mid(memories, "r2"), 1.0)])

        await svc._maybe_graph_boost(results, memories, _CTX, "u1", top_k=3)

        # adjusted: r0 = 0.90*0.85 = 0.765; r1 = 0.88*1.0*1.15 = 1.012;
        #           r2 = 0.86*1.15*1.15 ≈ 1.137 → order r2, r1, r0.
        assert [r["id"] for r in results] == ["r2", "r1", "r0"]
        # Cumulative stamp: reinforce factor x graph factor.
        assert results[0]["_rerank_factor"] == pytest.approx(1.15 * 1.15)

    @pytest.mark.asyncio
    async def test_max_boost_env_clamped(self, monkeypatch) -> None:
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "true")
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_MAX", "9.0")
        enabled, max_boost = MemoryService._graph_boost_settings()
        assert enabled is True
        assert max_boost == 0.5

    @pytest.mark.asyncio
    async def test_invalid_max_env_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "1")
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_MAX", "banana")
        enabled, max_boost = MemoryService._graph_boost_settings()
        assert enabled is True
        assert max_boost == 0.15


class TestFailSafe:
    @pytest.mark.asyncio
    async def test_db_failure_preserves_ranking(self, monkeypatch) -> None:
        monkeypatch.setenv("KAGURA_GRAPH_BOOST_ENABLED", "true")
        svc = _service(execute_error=RuntimeError("edges table on fire"))
        results, memories = _pool(0.9, 0.8, 0.7)

        await svc._maybe_graph_boost(results, memories, _CTX, "u1", top_k=3)

        assert [r["id"] for r in results] == ["r0", "r1", "r2"]
