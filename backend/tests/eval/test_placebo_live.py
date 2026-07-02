"""Live-stack Day-2 placebo kill-shot driver (directional de-risk).

Needs the full stack (Postgres + Qdrant + Redis + embedding provider + Sudachi),
so it is skip-guarded behind KAGURA_EVAL_LIVE=1 exactly like the compounding
live test (the ``make eval-placebo`` target sets it).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KAGURA_EVAL_LIVE") != "1",
    reason="live placebo eval requires the full stack; set KAGURA_EVAL_LIVE=1 (make eval-placebo)",
)


@pytest.mark.asyncio
async def test_placebo_live():
    from tests.eval.placebo_runner import run_placebo_eval

    results = await run_placebo_eval(seeds=(1, 2))

    assert results["experiment"] == "day2-placebo"
    assert results["real_warm"]["n"] > 0
    assert len(results["per_seed"]) == 2
    for block in results["per_seed"]:
        assert block["arms"]["shuffled_gold"]["n"] > 0
        # random_edge is n==0 only when the graph is too sparse to rewire
        assert "random_edge" in block["arms"]
    assert "tau" in results
    assert results["directional_read"] in {"alive", "edge_spray", "inconclusive"}
