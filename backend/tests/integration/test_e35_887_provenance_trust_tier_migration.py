"""Integration pins for #887 — DB-level provenance + trust_tier behaviour.

Covers what the AST/CHECK-literal unit tests cannot: the actual PostgreSQL
DEFAULT backfill, the NOT NULL constraint, and the CHECK constraints rejecting
out-of-set values at the engine level, for both ``memories.source_type`` and
``contexts.trust_tier``.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_MEM_COLS = (
    "id, user_id, summary, content, type, importance, confidence, scope, "
    "embedding_status, client, source, long_term, use_count, access_count"
)
_MEM_VALS = (
    ":id, 'u1', 's', 'c', 'note', 0.5, 1.0, 'working', "
    "'success', 'test', 'mcp_remember', false, 0, 0"
)


@pytest.mark.asyncio
async def test_source_type_defaults_to_manual(db_session):
    """A row inserted WITHOUT source_type backfills to 'manual' (server DEFAULT)."""
    mem_id = uuid.uuid4()
    await db_session.execute(
        text(f"INSERT INTO memories ({_MEM_COLS}) VALUES ({_MEM_VALS})"),
        {"id": mem_id},
    )
    row = (
        await db_session.execute(
            text("SELECT source_type FROM memories WHERE id = :id"), {"id": mem_id}
        )
    ).one()
    assert row.source_type == "manual"


@pytest.mark.asyncio
@pytest.mark.parametrize("stype", ["file", "url", "vault", "api", "manual", "connector"])
async def test_source_type_accepts_valid_values(db_session, stype):
    mem_id = uuid.uuid4()
    await db_session.execute(
        text(f"INSERT INTO memories ({_MEM_COLS}, source_type) VALUES ({_MEM_VALS}, :stype)"),
        {"id": mem_id, "stype": stype},
    )
    row = (
        await db_session.execute(
            text("SELECT source_type FROM memories WHERE id = :id"), {"id": mem_id}
        )
    ).one()
    assert row.source_type == stype


@pytest.mark.asyncio
async def test_source_type_check_rejects_unknown_value(db_session):
    mem_id = uuid.uuid4()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(f"INSERT INTO memories ({_MEM_COLS}, source_type) VALUES ({_MEM_VALS}, 'bogus')"),
            {"id": mem_id},
        )


async def _make_workspace(db_session):
    ws = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :n, :o)"),
        {"id": ws, "n": "ws-887", "o": "u-887"},
    )
    return ws


@pytest.mark.asyncio
async def test_context_trust_tier_defaults_to_trusted(db_session):
    ws = await _make_workspace(db_session)
    ctx = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) VALUES (:id, :ws, :n, :by)"
        ),
        {"id": ctx, "ws": ws, "n": "ctx-887", "by": "u-887"},
    )
    row = (
        await db_session.execute(
            text("SELECT trust_tier FROM contexts WHERE id = :id"), {"id": ctx}
        )
    ).one()
    assert row.trust_tier == "trusted"


@pytest.mark.asyncio
async def test_context_trust_tier_accepts_external(db_session):
    ws = await _make_workspace(db_session)
    ctx = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by, trust_tier) "
            "VALUES (:id, :ws, :n, :by, 'external')"
        ),
        {"id": ctx, "ws": ws, "n": "ctx-887b", "by": "u-887"},
    )
    row = (
        await db_session.execute(
            text("SELECT trust_tier FROM contexts WHERE id = :id"), {"id": ctx}
        )
    ).one()
    assert row.trust_tier == "external"


@pytest.mark.asyncio
async def test_context_trust_tier_check_rejects_unknown_value(db_session):
    ws = await _make_workspace(db_session)
    ctx = uuid.uuid4()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO contexts (id, workspace_id, name, created_by, trust_tier) "
                "VALUES (:id, :ws, :n, :by, 'bogus')"
            ),
            {"id": ctx, "ws": ws, "n": "ctx-887c", "by": "u-887"},
        )
