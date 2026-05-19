"""Monthly semantic_edge_reverify cron (Issue #722)."""

import pytest

from models.memory import EDGE_ORIGIN_HEBBIAN, EDGE_ORIGIN_SEMANTIC, NeuralMemoryEdge
from tasks.semantic_edge_reverify import semantic_edge_reverify_run
from utils.datetime import utcnow


@pytest.mark.asyncio
async def test_reverify_drops_edges_whose_endpoint_is_soft_deleted(
    db_session, sample_memory_pair, two_edges_one_hebbian_one_semantic
):
    """Soft-delete dst → semantic edge gone, hebbian sister untouched."""
    src, dst = sample_memory_pair
    hebbian, semantic = two_edges_one_hebbian_one_semantic
    dst.deleted_at = utcnow()
    await db_session.flush()

    result = await semantic_edge_reverify_run(db_session)
    assert result["semantic_edges_deleted"] == 1

    refetch_hebbian = await db_session.get(NeuralMemoryEdge, hebbian.id)
    assert refetch_hebbian is not None  # hebbian sister survives — not this task's domain


@pytest.mark.asyncio
async def test_reverify_keeps_edges_with_live_endpoints(
    db_session, two_edges_one_hebbian_one_semantic
):
    """Both endpoints alive → no deletion."""
    result = await semantic_edge_reverify_run(db_session)
    assert result["semantic_edges_deleted"] == 0
