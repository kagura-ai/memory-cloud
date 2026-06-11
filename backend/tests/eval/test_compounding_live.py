"""Live-stack compounding (cold→warm) experiment driver (Issue #969).

Tier B companion to ``test_retrieval_quality.py`` (#967). Needs the full stack
(Postgres + Qdrant + Redis + an embedding provider + Sudachi), so it is
**skip-guarded** behind ``KAGURA_EVAL_LIVE=1`` exactly like the Tier A live
test (the ``make eval-compounding`` target sets it).

What it does when enabled, once per replay mode (exclude/include probes), in
an isolated throwaway workspace each:

1. Ingests the frozen golden corpus (corpus held fixed — growth ≠ more data).
2. **Cold checkpoint**: graph-lane companion recovery on the 5 held-out
   multi-gold probes (explore/activation-spreading from the seed gold doc)
   + recall-lane P@5/MRR/nDCG control (neural off — read-only measurement).
3. **Replay**: drives the replay workload through ``recall()`` with neural
   memory enabled for N rounds, so co-activation accumulates Hebbian edges.
4. **warm_replay checkpoint**, then one Sleep ``edges_only`` consolidation
   run (no-LLM auto-accept judge), then the **warm_sleep checkpoint**.
5. Writes ``results/compounding-<YYYY-MM-DD>.json`` with the per-lane lift
   tables. NUMBERS ARE NEVER FABRICATED — only a real run produces this file.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KAGURA_EVAL_LIVE") != "1",
    reason="live compounding eval requires the full stack; set KAGURA_EVAL_LIVE=1 (make eval-compounding)",
)


@pytest.mark.asyncio
async def test_compounding_live():
    """Run the cold→replay→warm experiment and write the lift-table JSON."""
    from tests.eval.compounding import MODE_EXCLUDE_PROBES, MODE_INCLUDE_PROBES
    from tests.eval.replay_runner import run_compounding_eval

    results = await run_compounding_eval()

    assert results["experiment"] == "compounding"
    assert set(results["modes"]) == {MODE_EXCLUDE_PROBES, MODE_INCLUDE_PROBES}

    for mode_block in results["modes"].values():
        assert mode_block["probe_count"] == 5
        checkpoints = mode_block["checkpoints"]
        assert set(checkpoints) == {"cold", "warm_replay", "warm_sleep"}
        for checkpoint in checkpoints.values():
            assert "graph_lane" in checkpoint
            assert "recall_lane" in checkpoint
            assert "edge_stats" in checkpoint
        # The mechanism itself must have fired: replay traffic with neural
        # memory enabled writes Hebbian edges. This asserts the experiment
        # actually exercised the learned layer — NOT a quality gate on lift.
        hebbian_after_replay = (
            checkpoints["warm_replay"]["edge_stats"].get("hebbian", {}).get("count", 0)
        )
        assert hebbian_after_replay > 0, "replay produced no Hebbian edges — warming did not run"

        # The gate audit explains the graph-lane numbers: it classifies every
        # co-activatable pair of the replay traffic against the edge-formation
        # gates, with the probe gold pairs called out.
        audit = mode_block["gate_audit"]
        assert audit["pair_observations"] > 0
        assert set(audit["thresholds"]) >= {"min_similarity_for_edge", "prune_threshold"}
        # #982 / Gate1: the success criterion measures the NOISE side too, not
        # just recovery. A recalibration that lifts recovery@10 by also forming
        # spurious non-gold edges is a regression, not a win — these keys make
        # edge precision / false-edge rate visible in every run's report.
        assert {
            "formed_total",
            "formed_gold",
            "formed_non_gold",
            "non_gold_pair_count",
            "edge_precision",
            "non_gold_form_rate",
        } <= set(audit)

        lift = mode_block["lift"]
        assert set(lift) == {"graph_lane", "recall_lane"}
        for lane in lift.values():
            assert set(lane) == {
                "cold_to_warm_replay",
                "warm_replay_to_warm_sleep",
                "cold_to_warm_sleep",
            }
        assert "recovery@10" in lift["graph_lane"]["cold_to_warm_sleep"]
        assert "ndcg@10" in lift["recall_lane"]["cold_to_warm_sleep"]
