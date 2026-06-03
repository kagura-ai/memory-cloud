"""Integration pins for #886 delivery_mode column DB-level behavior.

Covers what the AST drift guard and create_all parity test cannot: the actual
PostgreSQL DEFAULT backfill and the ``valid_delivery_mode`` CHECK rejecting
out-of-set values at the engine level.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _insert_memory_sql(delivery_mode_clause: str) -> str:
    return f"""
        INSERT INTO memories
          (id, user_id, summary, content, type, importance, confidence,
           scope, embedding_status, client, source, long_term, use_count,
           access_count{delivery_mode_clause[0]})
        VALUES
          (:id, 'u1', 's', 'c', 'note', 0.5, 1.0,
           'working', 'success', 'test', 'mcp_remember', false, 0, 0{delivery_mode_clause[1]})
    """


@pytest.mark.asyncio
async def test_delivery_mode_defaults_to_on_recall(db_session):
    """A row inserted WITHOUT delivery_mode backfills to 'on_recall' (the
    server-side DEFAULT), so existing/legacy write paths are unaffected."""
    mem_id = uuid.uuid4()
    await db_session.execute(
        text(_insert_memory_sql(("", ""))),
        {"id": mem_id},
    )
    row = (
        await db_session.execute(
            text("SELECT delivery_mode FROM memories WHERE id = :id"),
            {"id": mem_id},
        )
    ).one()
    assert row.delivery_mode == "on_recall"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["always", "on_recall", "on_trigger"])
async def test_delivery_mode_accepts_valid_modes(db_session, mode):
    """All three orthogonal delivery modes are accepted by the CHECK."""
    mem_id = uuid.uuid4()
    await db_session.execute(
        text(_insert_memory_sql((", delivery_mode", ", :mode"))),
        {"id": mem_id, "mode": mode},
    )
    row = (
        await db_session.execute(
            text("SELECT delivery_mode FROM memories WHERE id = :id"),
            {"id": mem_id},
        )
    ).one()
    assert row.delivery_mode == mode


@pytest.mark.asyncio
async def test_delivery_mode_check_rejects_unknown_value(db_session):
    """``valid_delivery_mode`` CHECK rejects a value outside the enum set."""
    mem_id = uuid.uuid4()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(_insert_memory_sql((", delivery_mode", ", :mode"))),
            {"id": mem_id, "mode": "eventually"},
        )
