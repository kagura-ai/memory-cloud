"""DB-free orchestration tests for the #1213 graph-boost runner.

Sibling of ``test_reinforce_runner.py``: the live pieces (ingest, recall,
sleep, edge swap) are faked; what is pinned here is the CONTROL FLOW the
gate's validity depends on — env toggling per arm, warm build before
measurement, rewire→measure→restore ordering, and the arm pairing handed to
``evaluate_gate``.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tests.eval.graph_boost_runner import _env, _measure_arms, _recall_rankings


class TestEnvContextManager:
    def test_sets_and_restores_prior_value(self) -> None:
        os.environ["GB_TEST_KEY"] = "prior"
        try:
            with _env("GB_TEST_KEY", "inner"):
                assert os.environ["GB_TEST_KEY"] == "inner"
            assert os.environ["GB_TEST_KEY"] == "prior"
        finally:
            os.environ.pop("GB_TEST_KEY", None)

    def test_unset_key_is_removed_after(self) -> None:
        os.environ.pop("GB_TEST_KEY", None)
        with _env("GB_TEST_KEY", "inner"):
            assert os.environ["GB_TEST_KEY"] == "inner"
        assert "GB_TEST_KEY" not in os.environ

    def test_restores_on_exception(self) -> None:
        os.environ["GB_TEST_KEY"] = "prior"
        try:
            # Nested (not comma-combined) so static analysis models
            # pytest.raises swallowing the exception — the trailing assert
            # was flagged unreachable under the combined form (CodeQL).
            with pytest.raises(RuntimeError):
                with _env("GB_TEST_KEY", "inner"):
                    raise RuntimeError("boom")
            assert os.environ["GB_TEST_KEY"] == "prior"
        finally:
            os.environ.pop("GB_TEST_KEY", None)


class TestRecallRankings:
    @pytest.mark.asyncio
    async def test_maps_memory_ids_to_doc_ids_and_carries_gold(self) -> None:
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

        rankings = await _recall_rankings(
            svc, [("query text", {"doc-b"})], id_map, "owner", uuid4(), uuid4()
        )

        assert rankings == [(["doc-a", "doc-b"], {"doc-b"})]
        request = svc.recall.await_args.kwargs["request"]
        assert request.query == "query text"
        assert request.search_mode == "hybrid"


def _fake_world():
    """Corpus/plan/handles for a two-probe, two-replay-query world."""
    probes = (
        SimpleNamespace(text="probe one", companion_docs=("c1",)),
        SimpleNamespace(text="probe two", companion_docs=("c2",)),
    )
    queries = (
        SimpleNamespace(id="q1", text="replay one", relevant=("r1",)),
        SimpleNamespace(id="q2", text="replay two", relevant=("r2",)),
        SimpleNamespace(id="p-held-out", text="probe one", relevant=("c1",)),
    )
    plan = SimpleNamespace(probes=probes, replay_query_ids=("q1", "q2"))
    corpus = SimpleNamespace(queries=queries)
    ctx = SimpleNamespace(id=uuid4())
    ws = SimpleNamespace(id=uuid4())
    return corpus, plan, ctx, ws


_HIT = (["c1", "x"], {"c1"})


class TestMeasureArmsOrchestration:
    @pytest.mark.asyncio
    async def test_arm_env_sequencing_and_rewire_restore_order(self) -> None:
        corpus, plan, ctx, ws = _fake_world()
        snapshot = [
            {
                "src_id": str(uuid4()),
                "dst_id": str(uuid4()),
                "weight": 1.0,
                "origin": "hebbian",
                "confidence": 0.9,
                "edge_type": "related",
            }
            for _ in range(3)
        ]
        env_per_call: list[tuple[str | None, str | None]] = []
        replace_calls: list[str] = []

        async def fake_recall_rankings(svc, queries, id_map, owner, ctx_id, ws_id):
            env_per_call.append(
                (
                    os.environ.get("KAGURA_GRAPH_BOOST_ENABLED"),
                    os.environ.get("ENABLE_NEURAL_MEMORY"),
                )
            )
            return [_HIT] * len(queries)

        async def fake_replace_edges(db, ctx_id, rows):
            replace_calls.append("rewired" if rows is not snapshot else "snapshot")

        def fake_rewire(edges, seed):
            return list(edges), 5

        with (
            patch(
                "tests.eval.graph_boost_runner._seed_provisional_tau",
                new=AsyncMock(return_value={"seeded": True}),
            ),
            patch(
                "tests.eval.graph_boost_runner._replay", new=AsyncMock(return_value=16)
            ) as replay,
            patch(
                "tests.eval.graph_boost_runner._run_sleep", new=AsyncMock(return_value={"ok": True})
            ),
            patch(
                "tests.eval.graph_boost_runner._snapshot_edges",
                new=AsyncMock(return_value=snapshot),
            ),
            patch("tests.eval.graph_boost_runner._replace_edges", new=fake_replace_edges),
            patch(
                "tests.eval.graph_boost_runner.degree_preserving_rewire_with_stats",
                new=fake_rewire,
            ),
            patch("tests.eval.graph_boost_runner._recall_rankings", new=fake_recall_rankings),
        ):
            result = await _measure_arms(
                SimpleNamespace(), SimpleNamespace(), corpus, plan, {}, "owner", ctx, ws
            )

        # Warm build ran once before any measurement.
        replay.assert_awaited_once()
        # Seven measurement passes: unboosted probes, non-graph OFF, boosted
        # probes, non-graph ON, then one rewired-placebo pass per pre-declared
        # rewire seed — all with neural pinned false, boost toggled per arm.
        assert env_per_call == [
            ("false", "false"),
            ("false", "false"),
            ("true", "false"),
            ("true", "false"),
            ("true", "false"),
            ("true", "false"),
            ("true", "false"),
        ]
        # One swap-in per rewire seed, and the true snapshot is ALWAYS
        # restored afterwards (exactly once, in the finally).
        assert replace_calls == ["rewired", "rewired", "rewired", "snapshot"]
        assert result["warm_build"]["edge_snapshot"] == {
            "n": 3,
            "n_hebbian": 3,
            "rewire_swaps_by_seed": {42: 5, 43: 5, 44: 5},
        }
        assert result["n_probes"] == 2
        assert result["n_nongraph"] == 2  # held-out probe query excluded
        # Two fake probes are far below MIN_PROBES — the gate must refuse to
        # render an inferential verdict on this synthetic world.
        assert result["gate"]["verdict"] == "underpowered"

    @pytest.mark.asyncio
    async def test_sparse_graph_skips_rewire_and_reuses_unboosted(self) -> None:
        corpus, plan, ctx, ws = _fake_world()

        async def fake_recall_rankings(svc, queries, id_map, owner, ctx_id, ws_id):
            return [_HIT] * len(queries)

        replace = AsyncMock()
        with (
            patch(
                "tests.eval.graph_boost_runner._seed_provisional_tau",
                new=AsyncMock(return_value={}),
            ),
            patch("tests.eval.graph_boost_runner._replay", new=AsyncMock(return_value=0)),
            patch("tests.eval.graph_boost_runner._run_sleep", new=AsyncMock(return_value={})),
            patch("tests.eval.graph_boost_runner._snapshot_edges", new=AsyncMock(return_value=[])),
            patch("tests.eval.graph_boost_runner._replace_edges", new=replace),
            patch("tests.eval.graph_boost_runner._recall_rankings", new=fake_recall_rankings),
        ):
            result = await _measure_arms(
                SimpleNamespace(), SimpleNamespace(), corpus, plan, {}, "owner", ctx, ws
            )

        replace.assert_not_awaited()
        assert result["warm_build"]["edge_snapshot"] == {
            "n": 0,
            "n_hebbian": 0,
            "rewire_swaps_by_seed": {},
        }
