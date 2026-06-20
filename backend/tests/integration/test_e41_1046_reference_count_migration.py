"""Integration pins for #1046 — reference_count adoption signal at the DB level.

Covers what unit tests cannot: the actual PostgreSQL DEFAULT backfill of the new
``reference_count`` column, and that the dead ``use_count`` column + its stale
``idx_consolidation`` index were dropped by migration e41_1046_reference_count.
"""

import uuid

import pytest
from sqlalchemy import text

# memories columns at head (post-e41): no ``use_count``; ``reference_count`` is
# omitted so the server DEFAULT backfill is exercised.
_MEM_COLS = (
    "id, user_id, summary, content, type, importance, confidence, scope, "
    "embedding_status, client, source, long_term, access_count"
)
_MEM_VALS = (
    ":id, 'u1', 's', 'c', 'note', 0.5, 1.0, 'working', 'success', 'test', 'mcp_remember', false, 0"
)


@pytest.mark.asyncio
async def test_reference_count_defaults_to_zero(db_session):
    """A row inserted WITHOUT reference_count backfills to 0 (server DEFAULT)."""
    mem_id = uuid.uuid4()
    await db_session.execute(
        text(f"INSERT INTO memories ({_MEM_COLS}) VALUES ({_MEM_VALS})"),
        {"id": mem_id},
    )
    row = (
        await db_session.execute(
            text("SELECT reference_count FROM memories WHERE id = :id"), {"id": mem_id}
        )
    ).one()
    assert row.reference_count == 0


@pytest.mark.asyncio
async def test_dead_use_count_column_dropped(db_session):
    """The dead ``use_count`` column no longer exists on memories."""
    row = (
        await db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'memories' AND column_name = 'use_count'"
            )
        )
    ).fetchall()
    assert row == []


@pytest.mark.asyncio
async def test_stale_idx_consolidation_dropped(db_session):
    """The stale ``idx_consolidation`` index (indexed the dead use_count) is gone."""
    row = (
        await db_session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'memories' AND indexname = 'idx_consolidation'"
            )
        )
    ).fetchall()
    assert row == []
