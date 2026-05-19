"""Backfill script tests (Issue #722)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from models.memory import EDGE_ORIGIN_SEMANTIC, NeuralMemoryEdge

# Scripts are not an importable package — add backend/scripts to sys.path so
# pytest can import the CLI module. This mirrors the sys.path manipulation in
# test_measure_embedding_threshold.py.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import backfill_semantic_edges as bse  # noqa: E402


@pytest.mark.asyncio
async def test_skips_contexts_below_floor(db_session, small_context_30_memories):
    ctx_id = small_context_30_memories
    fake_qdrant = MagicMock()
    result = await bse.backfill_context(
        db_session,
        fake_qdrant,  # type: ignore[arg-type]
        ctx_id,
        min_memories=50,
        sim_threshold=0.7,
        top_k=10,
    )
    assert result["skipped"] is True
    assert result["reason"] == "below_memory_floor"


@pytest.mark.asyncio
async def test_inserts_semantic_edges_for_pairs_above_threshold(
    db_session, large_context_60_memories
):
    ctx_id = large_context_60_memories["context_id"]
    memory_ids = large_context_60_memories["memory_ids"]

    async def mock_query_neighbors(memory_id, context_id, user_id, workspace_id, top_k):
        out = []
        for i, mid in enumerate(memory_ids[: top_k + 1]):
            if mid == memory_id:
                continue
            score = max(0.95 - 0.02 * i, 0.0)
            out.append((mid, score))
        return out[:top_k]

    fake_qdrant = MagicMock()
    fake_qdrant.query_neighbors = AsyncMock(side_effect=mock_query_neighbors)

    result = await bse.backfill_context(
        db_session,
        fake_qdrant,  # type: ignore[arg-type]
        ctx_id,
        min_memories=50,
        sim_threshold=0.7,
        top_k=10,
    )
    assert result["skipped"] is False
    assert result["edges_inserted"] > 0

    # Inspect the persisted edges
    edges = (
        (
            await db_session.execute(
                select(NeuralMemoryEdge).where(NeuralMemoryEdge.context_id == ctx_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(edges) == result["edges_inserted"]
    for e in edges:
        assert e.origin == EDGE_ORIGIN_SEMANTIC
        assert 0.7 <= e.weight <= 1.0

    # Re-run should be idempotent
    result2 = await bse.backfill_context(
        db_session,
        fake_qdrant,  # type: ignore[arg-type]
        ctx_id,
        min_memories=50,
        sim_threshold=0.7,
        top_k=10,
    )
    assert result2["edges_inserted"] == 0


@pytest.mark.asyncio
async def test_dry_run_does_not_insert(db_session, large_context_60_memories):
    ctx_id = large_context_60_memories["context_id"]
    memory_ids = large_context_60_memories["memory_ids"]

    async def mock_query_neighbors(memory_id, context_id, user_id, workspace_id, top_k):
        out = []
        for i, mid in enumerate(memory_ids[: top_k + 1]):
            if mid == memory_id:
                continue
            out.append((mid, max(0.95 - 0.02 * i, 0.0)))
        return out[:top_k]

    fake_qdrant = MagicMock()
    fake_qdrant.query_neighbors = AsyncMock(side_effect=mock_query_neighbors)

    result = await bse.backfill_context(
        db_session,
        fake_qdrant,  # type: ignore[arg-type]
        ctx_id,
        min_memories=50,
        sim_threshold=0.7,
        top_k=10,
        dry_run=True,
    )
    assert result["edges_inserted"] == 0  # dry run reports 0 actual inserts
    assert result.get("pairs_would_insert", 0) > 0  # but tracks would-be count

    # Verify nothing landed in the DB
    edges = (
        (
            await db_session.execute(
                select(NeuralMemoryEdge).where(NeuralMemoryEdge.context_id == ctx_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(edges) == 0
