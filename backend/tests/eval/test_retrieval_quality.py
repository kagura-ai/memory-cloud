"""Live-stack retrieval-quality measurement (Issue #344).

This is the ONLY part of the harness that needs the full stack (Postgres +
Qdrant + an embedding provider + Sudachi), which the issue notes is not currently
CI-realistic (#336). It is therefore **skip-guarded**: it runs only when
``KAGURA_EVAL_LIVE=1`` is set (the ``make eval-retrieval`` target sets it). In
normal CI it is collected but skipped, so it never blocks on infra.

What it does when enabled:
1. Provisions a throwaway workspace + context.
2. Ingests every golden-corpus document as a memory, recording memory_id →
   (corpus_doc_id, source).
3. Runs ``recall`` for each query (hybrid, k=10), maps results back to corpus
   doc ids, and computes P@5 / P@10 / MRR@10 per bucket and overall, plus the
   memory-vs-resource source-recall share.
4. Writes ``results/<YYYY-MM-DD>.json`` (stamped with the Sudachi version) for
   trend tracking. NUMBERS ARE NEVER FABRICATED — this file is the only place a
   baseline is produced, and only from a real run.

Because this cannot run in this environment, the committed baseline is produced
by a maintainer running ``make eval-retrieval`` locally (see README.md). Do not
hand-edit results JSON.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KAGURA_EVAL_LIVE") != "1",
    reason="live retrieval eval requires the full stack; set KAGURA_EVAL_LIVE=1 (make eval-retrieval)",
)

# The reported k values (P@5/P@10, MRR@10, recall@10) live in tests.eval.runner,
# which computes the metrics; this thin wrapper only asserts the result shape.


@pytest.mark.asyncio
async def test_retrieval_quality_live():
    """Run the live harness and write a results JSON snapshot."""
    from tests.eval.runner import run_retrieval_eval

    results = await run_retrieval_eval()
    # Minimal sanity assertions — the value is the committed JSON, not a gate.
    assert results["query_count"] >= 30
    assert "overall" in results
    assert set(results["per_bucket"]), "per-bucket metrics must be populated"
