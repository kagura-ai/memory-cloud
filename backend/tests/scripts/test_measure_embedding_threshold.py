"""Tests for scripts/measure_embedding_threshold.py (Issue #240 Phase A).

Split across:
  * Pure function unit tests (percentiles, floor, pair cosine, report builder).
  * Mocked-integration tests for the orchestrated top-k measurement path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import numpy as np
import pytest

# Scripts are not an importable package — add backend/scripts to sys.path so
# pytest can import the CLI module. This mirrors the sys.path manipulation the
# script itself performs for src/ at import time.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import measure_embedding_threshold as met  # noqa: E402

# ---------------------------------------------------------------------------
# compute_percentiles
# ---------------------------------------------------------------------------


class TestComputePercentiles:
    def test_returns_all_expected_keys(self):
        values = [0.1 * i for i in range(1, 101)]  # 0.01..1.00
        result = met.compute_percentiles(values)
        assert set(result.keys()) == {"p25", "p50", "p75", "p90", "p95", "p99"}

    def test_matches_known_values(self):
        # range(100) → pre-computed numpy percentiles. Hardcoding the expected
        # values means this test would catch a future regression that swaps
        # the percentile method, type-coerces the output, or reorders keys —
        # which re-deriving them via np.percentile inside the test cannot.
        result = met.compute_percentiles(list(range(100)))
        assert result["p25"] == pytest.approx(24.75)
        assert result["p50"] == pytest.approx(49.5)
        assert result["p75"] == pytest.approx(74.25)
        assert result["p90"] == pytest.approx(89.1)
        assert result["p95"] == pytest.approx(94.05)
        assert result["p99"] == pytest.approx(98.01)

    def test_monotonically_non_decreasing(self):
        values = np.random.default_rng(42).uniform(0, 1, size=500).tolist()
        result = met.compute_percentiles(values)
        ordered = [result[f"p{p}"] for p in (25, 50, 75, 90, 95, 99)]
        assert ordered == sorted(ordered)

    def test_empty_returns_empty_dict(self):
        assert met.compute_percentiles([]) == {}


# ---------------------------------------------------------------------------
# suggest_threshold
# ---------------------------------------------------------------------------


class TestSuggestThreshold:
    def test_p90_above_floor_wins(self):
        result = met.suggest_threshold(0.42, floor=0.3)
        assert result == {"percentile_p90": 0.42, "floor": 0.3, "effective": 0.42}

    def test_floor_wins_when_p90_below(self):
        result = met.suggest_threshold(0.15, floor=0.3)
        assert result == {"percentile_p90": 0.15, "floor": 0.3, "effective": 0.3}

    def test_p90_equal_to_floor(self):
        result = met.suggest_threshold(0.3, floor=0.3)
        assert result["effective"] == 0.3

    def test_default_floor_is_runtime_floor_constant(self):
        assert met.RUNTIME_FLOOR == 0.3
        result = met.suggest_threshold(0.25)
        assert result["floor"] == 0.3
        assert result["effective"] == 0.3


# ---------------------------------------------------------------------------
# measure_random_pair
# ---------------------------------------------------------------------------


class TestMeasureRandomPair:
    def test_identical_vectors_give_cosine_1(self):
        v = [1.0, 0.0, 0.0]
        # Four copies so max_pairs=2
        sims = met.measure_random_pair([v, v, v, v], n_pairs=2, seed=0)
        assert len(sims) == 2
        for s in sims:
            assert s == pytest.approx(1.0)

    def test_orthogonal_vectors_give_cosine_0(self):
        # All 4 vectors are mutually orthogonal unit vectors, so every pair
        # produced by any permutation has cosine == 0. This lets the test
        # assert strict 0.0 instead of the loose "0 or 1" form that earlier
        # allowed duplicate-pair permutations to pass silently.
        vectors = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        sims = met.measure_random_pair(vectors, n_pairs=2, seed=0)
        assert len(sims) == 2
        for s in sims:
            assert s == pytest.approx(0.0)

    def test_empty_returns_empty(self):
        assert met.measure_random_pair([], n_pairs=5) == []

    def test_single_vector_has_no_pairs(self):
        assert met.measure_random_pair([[1.0, 0.0]], n_pairs=5) == []

    def test_clamps_to_max_available_pairs(self):
        # 5 vectors → max 2 pairs regardless of n_pairs request
        vectors = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.5, 0.5], [0.2, 0.8]]
        sims = met.measure_random_pair(vectors, n_pairs=100, seed=0)
        assert len(sims) == 2

    def test_reproducible_with_seed(self):
        vectors = [[float(i), float(i + 1), float(i - 1)] for i in range(20)]
        run_a = met.measure_random_pair(vectors, n_pairs=5, seed=123)
        run_b = met.measure_random_pair(vectors, n_pairs=5, seed=123)
        assert run_a == run_b


# ---------------------------------------------------------------------------
# check_bootstrap_gate
# ---------------------------------------------------------------------------


def _report(memories: int, observations: int) -> dict:
    return {
        "sample": {"memories": memories, "observations_total": observations},
    }


class TestCheckBootstrapGate:
    def test_both_above_threshold_no_warnings(self):
        r = _report(memories=200, observations=10_000)
        assert met.check_bootstrap_gate(r) == []

    def test_memories_above_alone_satisfies_or_gate(self):
        # D3: OR-gate — memories ≥ 200 alone is enough even if observations
        # is tiny. Operators with a small top_k setting should NOT be warned.
        r = _report(memories=200, observations=1_000)
        assert met.check_bootstrap_gate(r) == []

    def test_observations_above_alone_satisfies_or_gate(self):
        # D3: OR-gate — ≥ 10k observations alone is enough even if memories
        # is below. A small-context run with high top_k should NOT be warned.
        r = _report(memories=50, observations=10_000)
        assert met.check_bootstrap_gate(r) == []

    def test_both_below_triggers_warnings(self):
        r = _report(memories=50, observations=1_000)
        warnings = met.check_bootstrap_gate(r)
        assert any("sample_size_below_bootstrap_gate" in w for w in warnings)
        assert any("observations_below_bootstrap_gate" in w for w in warnings)


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_populates_all_sections(self):
        ctx = uuid4()
        report = met.build_report(
            context_id=ctx,
            model_name="test-model",
            dimensions=128,
            collection="kagura_memories_test_128",
            sampled_memories=10,
            top_k_requested=5,
            random_pairs_requested=20,
            top_k_scores=[0.4 + 0.001 * i for i in range(50)],
            random_pair_scores=[0.1 + 0.001 * i for i in range(20)],
        )

        assert report["context_id"] == str(ctx)
        assert report["model"] == {
            "name": "test-model",
            "dimensions": 128,
            "collection": "kagura_memories_test_128",
        }
        assert report["sample"]["memories"] == 10
        assert report["sample"]["observations_total"] == 50
        assert report["sample"]["random_pair_observations"] == 20

        assert "p90" in report["top_k_distribution"]
        assert "p90" in report["random_pair_distribution"]

        # p90 on top-k data is ~0.4 + 0.001*45 = 0.445 ≥ floor; effective = p90
        assert report["suggested_threshold"]["effective"] == pytest.approx(
            report["top_k_distribution"]["p90"]
        )

    def test_floor_wins_when_p90_low(self):
        ctx = uuid4()
        report = met.build_report(
            context_id=ctx,
            model_name="m",
            dimensions=1,
            collection="c",
            sampled_memories=5,
            top_k_requested=1,
            random_pairs_requested=0,
            top_k_scores=[0.05, 0.10, 0.15, 0.20, 0.25],
            random_pair_scores=[],
        )
        assert report["suggested_threshold"]["effective"] == 0.3  # floor

    def test_empty_observations_returns_null_threshold(self):
        ctx = uuid4()
        report = met.build_report(
            context_id=ctx,
            model_name="m",
            dimensions=1,
            collection="c",
            sampled_memories=0,
            top_k_requested=0,
            random_pairs_requested=0,
            top_k_scores=[],
            random_pair_scores=[],
        )
        assert report["top_k_distribution"] == {}
        assert report["suggested_threshold"] is None


# ---------------------------------------------------------------------------
# measure_top_k (mocked integration)
# ---------------------------------------------------------------------------


def _fake_memory(*, user_id=None, workspace_id=None, context_id=None):
    m = MagicMock()
    m.id = uuid4()
    m.user_id = uuid4() if user_id is None else user_id
    m.workspace_id = uuid4() if workspace_id is None else workspace_id
    m.context_id = uuid4() if context_id is None else context_id
    return m


class TestMeasureTopK:
    @pytest.mark.asyncio
    async def test_excludes_self_hit(self, monkeypatch):
        mem = _fake_memory()
        self_id_str = str(mem.id)

        # Qdrant returns the query memory as the top hit (score=1.0) plus 3 real neighbors.
        async def fake_search(**_kwargs):
            return [
                {"id": self_id_str, "score": 1.0},
                {"id": str(uuid4()), "score": 0.8},
                {"id": str(uuid4()), "score": 0.6},
                {"id": str(uuid4()), "score": 0.4},
            ]

        monkeypatch.setattr(met, "search_memories_qdrant", fake_search)

        vectors = {self_id_str: [0.1, 0.2, 0.3]}
        scores = await met.measure_top_k([mem], vectors, "test_collection", top_k=3)
        assert scores == [0.8, 0.6, 0.4]

    @pytest.mark.asyncio
    async def test_respects_top_k_cap(self, monkeypatch):
        mem = _fake_memory()
        self_id_str = str(mem.id)

        async def fake_search(**_kwargs):
            # 10 non-self neighbors returned (plus self-hit at top)
            return [{"id": self_id_str, "score": 1.0}] + [
                {"id": str(uuid4()), "score": 0.9 - 0.05 * i} for i in range(10)
            ]

        monkeypatch.setattr(met, "search_memories_qdrant", fake_search)
        vectors = {self_id_str: [0.1]}
        scores = await met.measure_top_k([mem], vectors, "c", top_k=5)
        assert len(scores) == 5
        assert scores[0] == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_uses_per_memory_isolation_params(self, monkeypatch):
        """Runtime parity: each memory queries with its own user/workspace/context."""
        mem = _fake_memory()
        self_id_str = str(mem.id)
        captured = {}

        async def fake_search(**kwargs):
            captured.update(kwargs)
            return [{"id": self_id_str, "score": 1.0}]

        monkeypatch.setattr(met, "search_memories_qdrant", fake_search)
        vectors = {self_id_str: [1.0, 2.0, 3.0]}
        await met.measure_top_k([mem], vectors, "col", top_k=5)

        assert captured["user_id"] == str(mem.user_id)
        assert captured["workspace_id"] == str(mem.workspace_id)
        assert captured["context_id"] == str(mem.context_id)
        assert captured["collection_name"] == "col"
        assert captured["limit"] == 6  # top_k + 1 for self-hit slot
        assert captured["query_vector"] == [1.0, 2.0, 3.0]

    @pytest.mark.asyncio
    async def test_skips_memory_missing_vector(self, monkeypatch):
        mem = _fake_memory()

        calls = []

        async def fake_search(**kwargs):
            calls.append(kwargs)
            return []

        monkeypatch.setattr(met, "search_memories_qdrant", fake_search)
        # No vector for this memory in the dict
        scores = await met.measure_top_k([mem], {}, "c", top_k=5)
        assert scores == []
        assert calls == []  # never called

    @pytest.mark.asyncio
    async def test_swallows_per_memory_search_error(self, monkeypatch):
        """A single memory's failed search should not abort the whole run."""
        mem_a = _fake_memory()
        mem_b = _fake_memory()

        async def fake_search(**kwargs):
            if kwargs["user_id"] == str(mem_a.user_id):
                raise RuntimeError("transient qdrant hiccup")
            return [{"id": str(uuid4()), "score": 0.7}]

        monkeypatch.setattr(met, "search_memories_qdrant", fake_search)
        vectors = {str(mem_a.id): [0.1], str(mem_b.id): [0.1]}
        scores = await met.measure_top_k([mem_a, mem_b], vectors, "c", top_k=1)
        assert scores == [0.7]  # mem_b's neighbor only


# ---------------------------------------------------------------------------
# fetch_vectors (mocked)
# ---------------------------------------------------------------------------


class TestFetchVectors:
    @pytest.mark.asyncio
    async def test_returns_id_to_vector_map(self):
        mid_a, mid_b = uuid4(), uuid4()
        point_a = MagicMock()
        point_a.id = str(mid_a)
        point_a.vector = {met.KAGURA_MEMORIES_VECTOR_NAME: [0.1, 0.2]}
        point_b = MagicMock()
        point_b.id = str(mid_b)
        point_b.vector = {met.KAGURA_MEMORIES_VECTOR_NAME: [0.3, 0.4]}

        qdrant = MagicMock()
        qdrant.retrieve = AsyncMock(return_value=[point_a, point_b])

        result = await met.fetch_vectors(qdrant, "col", [mid_a, mid_b])
        assert result == {str(mid_a): [0.1, 0.2], str(mid_b): [0.3, 0.4]}

    @pytest.mark.asyncio
    async def test_empty_input_skips_qdrant_call(self):
        qdrant = MagicMock()
        qdrant.retrieve = AsyncMock()
        result = await met.fetch_vectors(qdrant, "col", [])
        assert result == {}
        qdrant.retrieve.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_points_without_dense_vector(self):
        mid = uuid4()
        bad_point = MagicMock()
        bad_point.id = str(mid)
        bad_point.vector = None  # Qdrant returned no vector field
        qdrant = MagicMock()
        qdrant.retrieve = AsyncMock(return_value=[bad_point])
        result = await met.fetch_vectors(qdrant, "col", [mid])
        assert result == {}
