"""Monthly semantic_edge_reverify cron (Issue #722).

The task sweeps every user's semantic edges, and the test database is shared
with other test modules that commit edges of their own (e.g. ``tests/cli``).
So the tests check their own edges row by row, and compare the task's count
with the number of dead-endpoint semantic edges present just before the run
instead of a fixed number (#2385).
"""

import pytest
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import EDGE_ORIGIN_SEMANTIC, Memory, NeuralMemoryEdge
from tasks.semantic_edge_reverify import semantic_edge_reverify_run
from utils.datetime import utcnow


async def _edge_exists(db: AsyncSession, edge_id: int) -> bool:
    """Read the row from the database, not from the session's identity map."""
    found = await db.execute(select(NeuralMemoryEdge.id).where(NeuralMemoryEdge.id == edge_id))
    return found.scalar_one_or_none() is not None


async def _dead_semantic_edge_count(db: AsyncSession) -> int:
    """Semantic edges with a soft-deleted endpoint, across every user."""
    dead = select(Memory.id).where(Memory.deleted_at.is_not(None))
    count = await db.execute(
        select(func.count())
        .select_from(NeuralMemoryEdge)
        .where(
            NeuralMemoryEdge.origin == EDGE_ORIGIN_SEMANTIC,
            or_(NeuralMemoryEdge.src_id.in_(dead), NeuralMemoryEdge.dst_id.in_(dead)),
        )
    )
    return count.scalar_one()


@pytest.mark.asyncio
async def test_reverify_drops_edges_whose_endpoint_is_soft_deleted(
    db_session, sample_memory_pair, two_edges_one_hebbian_one_semantic
):
    """Soft-delete dst → semantic edge gone, hebbian sister untouched."""
    src, dst = sample_memory_pair
    hebbian, semantic = two_edges_one_hebbian_one_semantic
    semantic_id, hebbian_id = semantic.id, hebbian.id
    dst.deleted_at = utcnow()
    await db_session.flush()
    expected = await _dead_semantic_edge_count(db_session)
    assert expected >= 1  # this test's semantic edge, plus any leftovers

    result = await semantic_edge_reverify_run(db_session)

    assert result["semantic_edges_deleted"] == expected
    assert await _dead_semantic_edge_count(db_session) == 0
    assert not await _edge_exists(db_session, semantic_id)
    # hebbian sister survives — not this task's domain
    assert await _edge_exists(db_session, hebbian_id)


@pytest.mark.asyncio
async def test_reverify_keeps_edges_with_live_endpoints(
    db_session, two_edges_one_hebbian_one_semantic
):
    """Both endpoints alive → neither of this test's edges is deleted."""
    hebbian, semantic = two_edges_one_hebbian_one_semantic
    semantic_id, hebbian_id = semantic.id, hebbian.id
    expected = await _dead_semantic_edge_count(db_session)  # leftovers only

    result = await semantic_edge_reverify_run(db_session)

    assert result["semantic_edges_deleted"] == expected
    assert await _edge_exists(db_session, semantic_id)
    assert await _edge_exists(db_session, hebbian_id)
