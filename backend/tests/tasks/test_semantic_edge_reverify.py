"""Monthly semantic_edge_reverify cron (Issue #722).

The task sweeps every user's semantic edges, and the test database is shared
with other test modules that commit edges of their own (e.g. ``tests/cli``).
The assertions therefore look only at the edges these tests create, never at
the task's global deletion count (#2385).
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import NeuralMemoryEdge
from tasks.semantic_edge_reverify import semantic_edge_reverify_run
from utils.datetime import utcnow


async def _edge_exists(db: AsyncSession, edge_id: int) -> bool:
    """Read the row from the database, not from the session's identity map."""
    found = await db.execute(select(NeuralMemoryEdge.id).where(NeuralMemoryEdge.id == edge_id))
    return found.scalar_one_or_none() is not None


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

    result = await semantic_edge_reverify_run(db_session)
    # Other modules' leftover edges may be swept too, so only a lower bound.
    assert result["semantic_edges_deleted"] >= 1

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

    await semantic_edge_reverify_run(db_session)

    assert await _edge_exists(db_session, semantic_id)
    assert await _edge_exists(db_session, hebbian_id)
