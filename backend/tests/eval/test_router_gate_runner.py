"""DB-free control-flow tests for the #1220 router-gate runner.

Pins the runner's mechanics without a live stack: the env pins around arm
measurement, the explicit search_mode per lane arm, the routed-arm
construction (routed[i] == components[lane_i][i]), and the stage-4
persistence fan-out (one upsert per bucket × arm, fleet-default scope).
The live end-to-end run is `make eval-router-gate` (KAGURA_EVAL_LIVE=1).
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tests.eval.router_gate_runner import (
    _env,
    _lane_rankings,
    _persist_calibrations,
)


class TestEnvPin:
    def test_sets_and_restores(self) -> None:
        os.environ["RG_TEST_KEY"] = "prior"
        try:
            with _env("RG_TEST_KEY", "inner"):
                assert os.environ["RG_TEST_KEY"] == "inner"
            assert os.environ["RG_TEST_KEY"] == "prior"
        finally:
            os.environ.pop("RG_TEST_KEY", None)

    def test_unset_key_is_removed_after(self) -> None:
        os.environ.pop("RG_TEST_KEY", None)
        with _env("RG_TEST_KEY", "inner"):
            assert os.environ["RG_TEST_KEY"] == "inner"
        assert "RG_TEST_KEY" not in os.environ


class TestLaneRankings:
    @pytest.mark.asyncio
    async def test_passes_explicit_search_mode_and_maps_doc_ids(self) -> None:
        mem_a, mem_b = uuid4(), uuid4()
        id_map = {str(mem_a): "doc-a", str(mem_b): "doc-b"}
        svc = SimpleNamespace(
            recall=AsyncMock(
                return_value=SimpleNamespace(
                    results=[
                        SimpleNamespace(memory_id=mem_a),
                        SimpleNamespace(memory_id=mem_b),
                    ]
                )
            )
        )

        rankings = await _lane_rankings(
            svc, [("find the doc", {"doc-a"})], id_map, "u", uuid4(), uuid4(), "keyword"
        )

        assert rankings == [(["doc-a", "doc-b"], {"doc-a"})]
        request = svc.recall.await_args.kwargs["request"]
        assert request.search_mode == "keyword"  # the lane pin IS the arm


class TestPersistCalibrations:
    @pytest.mark.asyncio
    async def test_one_upsert_per_bucket_arm_at_fleet_scope(self) -> None:
        hit = (["g", "x1", "x2", "x3", "x4"], {"g"})
        miss = (["x1", "x2", "x3", "x4", "x5"], {"g"})
        components = {
            "semantic": [miss, hit],
            "keyword": [hit, miss],
            "hybrid": [hit, hit],
        }
        lanes = ["keyword", "semantic"]
        routed = [components[lane][i] for i, lane in enumerate(lanes)]
        repo = MagicMock()
        repo.upsert = AsyncMock()
        db = AsyncMock()

        with patch("repositories.config_repository.RouterCalibrationRepository", return_value=repo):
            written = await _persist_calibrations(db, components, routed, lanes)

        # 2 buckets x 4 arms (semantic/keyword/hybrid/routed).
        assert written == 8
        assert repo.upsert.await_count == 8
        for call in repo.upsert.await_args_list:
            assert call.kwargs["context_id"] is None  # fleet-default scope
            assert call.kwargs["n_queries"] == 1
            assert call.kwargs["bucket"] in ("keyword", "semantic")
            assert call.kwargs["arm"] in ("semantic", "keyword", "hybrid", "routed")
        # The routed arm on the keyword bucket is the keyword arm's ranking.
        routed_kw = next(
            c.kwargs
            for c in repo.upsert.await_args_list
            if c.kwargs["arm"] == "routed" and c.kwargs["bucket"] == "keyword"
        )
        keyword_kw = next(
            c.kwargs
            for c in repo.upsert.await_args_list
            if c.kwargs["arm"] == "keyword" and c.kwargs["bucket"] == "keyword"
        )
        assert routed_kw["p_at_5"] == keyword_kw["p_at_5"]
        db.commit.assert_awaited_once()
