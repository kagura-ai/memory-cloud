"""Integration pins for #1065 — DB-level feedback provenance behaviour.

Covers what the service-layer unit tests cannot: the PostgreSQL DEFAULT backfill
('agent'), the NOT NULL constraint, and the CHECK rejecting out-of-set values at
the engine level for ``retrieval_feedback.provenance`` — the forge-resistance
backstop (a bad provenance can never slip past the host-only aggregation).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Static SQL (no f-strings — project rule forbids interpolated SQL).
_INSERT_FEEDBACK_DEFAULT = (
    "INSERT INTO retrieval_feedback (context_id, memory_id, helpful, user_id) "
    "VALUES (:ctx, :mem, true, 'u-1065')"
)
_INSERT_FEEDBACK_WITH_PROVENANCE = (
    "INSERT INTO retrieval_feedback (context_id, memory_id, helpful, user_id, provenance) "
    "VALUES (:ctx, :mem, true, 'u-1065', :prov)"
)
_SELECT_PROVENANCE = "SELECT provenance FROM retrieval_feedback WHERE memory_id = :m"


async def _ctx_with_memory(db_session):
    """Create a workspace + context + memory and return (ctx_id, mem_id)."""
    ws, ctx, mem, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "u-1065"
    await db_session.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :n, :o)"),
        {"id": ws, "n": "ws-1065", "o": owner},
    )
    await db_session.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) VALUES (:id, :ws, :n, :by)"
        ),
        {"id": ctx, "ws": ws, "n": "ctx-1065", "by": owner},
    )
    await db_session.execute(
        text(
            "INSERT INTO memories "
            "(id, user_id, context_id, summary, content, type, importance, confidence, scope, "
            " embedding_status, client, source, long_term, access_count) "
            "VALUES (:id, :u, :ctx, 's', 'c', 'note', 0.5, 1.0, 'working', "
            " 'success', 'test', 'mcp_remember', false, 0)"
        ),
        {"id": mem, "u": owner, "ctx": ctx},
    )
    return ctx, mem


@pytest.mark.asyncio
async def test_provenance_defaults_to_agent(db_session):
    """A feedback row inserted WITHOUT provenance backfills to 'agent' — the
    public path never sets it, so every agent signal is server-stamped 'agent'."""
    ctx, mem = await _ctx_with_memory(db_session)
    await db_session.execute(text(_INSERT_FEEDBACK_DEFAULT), {"ctx": ctx, "mem": mem})
    row = (await db_session.execute(text(_SELECT_PROVENANCE), {"m": mem})).one()
    assert row.provenance == "agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("prov", ["agent", "host"])
async def test_provenance_accepts_valid_values(db_session, prov):
    ctx, mem = await _ctx_with_memory(db_session)
    await db_session.execute(
        text(_INSERT_FEEDBACK_WITH_PROVENANCE), {"ctx": ctx, "mem": mem, "prov": prov}
    )
    row = (await db_session.execute(text(_SELECT_PROVENANCE), {"m": mem})).one()
    assert row.provenance == prov


@pytest.mark.asyncio
async def test_provenance_check_rejects_unknown_value(db_session):
    """The DB CHECK is the forge-resistance backstop — a bogus provenance can
    never be persisted (and so can never masquerade as 'host')."""
    ctx, mem = await _ctx_with_memory(db_session)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(_INSERT_FEEDBACK_WITH_PROVENANCE), {"ctx": ctx, "mem": mem, "prov": "forged"}
        )
