"""Integration tests for AgentStateService (#889) — DB-backed.

Exercises the PostgreSQL-specific behaviour the unit tests can't: the
``ON CONFLICT`` upsert, the lazy TTL filter + opportunistic sweep, list
exclusion of expired rows, delete, and the TTL clamp. The handler
access-control/dispatch contract is unit-tested in
tests/mcp_server/test_state_tools.py.
"""

import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from services.agent_state_service import MAX_TTL_SECONDS, AgentStateService
from utils.datetime import utcnow


@pytest_asyncio.fixture
async def context_id(db_session):
    """A minimal workspace + context so the agent_states FK resolves."""
    ws = uuid.uuid4()
    ctx = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": ws, "name": "ws-889", "owner": "u-889"},
    )
    await db_session.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) "
            "VALUES (:id, :ws, :name, :by)"
        ),
        {"id": ctx, "ws": ws, "name": "ctx-889", "by": "u-889"},
    )
    await db_session.commit()
    return ctx


async def _expires_at(db_session, context_id, key):
    return (
        await db_session.execute(
            text("SELECT expires_at FROM agent_states WHERE context_id=:c AND key=:k"),
            {"c": context_id, "k": key},
        )
    ).scalar_one()


async def _row_count(db_session, context_id):
    return (
        await db_session.execute(
            text("SELECT count(*) FROM agent_states WHERE context_id=:c"),
            {"c": context_id},
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_set_get_roundtrip(db_session, context_id):
    svc = AgentStateService(db_session)
    await svc.set_state(context_id, "task", {"step": 1})
    assert await svc.get_state(context_id, "task") == {"step": 1}


@pytest.mark.asyncio
async def test_upsert_overwrites_in_place(db_session, context_id):
    svc = AgentStateService(db_session)
    await svc.set_state(context_id, "k", {"v": 1})
    await svc.set_state(context_id, "k", {"v": 2})
    assert await svc.get_state(context_id, "k") == {"v": 2}
    # Upsert, not insert: exactly one row for the key.
    assert await _row_count(db_session, context_id) == 1


@pytest.mark.asyncio
async def test_no_ttl_persists_with_null_expiry(db_session, context_id):
    svc = AgentStateService(db_session)
    await svc.set_state(context_id, "durable", "v")
    assert await _expires_at(db_session, context_id, "durable") is None


@pytest.mark.asyncio
async def test_expired_entry_is_absent_and_swept(db_session, context_id):
    svc = AgentStateService(db_session)
    await svc.set_state(context_id, "ephem", {"x": 1}, ttl_seconds=60)
    # Back-date the TTL so the entry is expired on the next read.
    await db_session.execute(
        text("UPDATE agent_states SET expires_at=:past WHERE context_id=:c AND key='ephem'"),
        {"past": utcnow() - timedelta(seconds=1), "c": context_id},
    )
    await db_session.commit()

    assert await svc.get_state(context_id, "ephem") is None
    # Lazy sweep removed the tombstone.
    assert await _row_count(db_session, context_id) == 0


@pytest.mark.asyncio
async def test_list_excludes_expired(db_session, context_id):
    svc = AgentStateService(db_session)
    await svc.set_state(context_id, "live", 1)
    await svc.set_state(context_id, "dead", 2, ttl_seconds=60)
    await db_session.execute(
        text("UPDATE agent_states SET expires_at=:past WHERE context_id=:c AND key='dead'"),
        {"past": utcnow() - timedelta(seconds=1), "c": context_id},
    )
    await db_session.commit()

    assert await svc.list_state(context_id) == {"live": 1}


@pytest.mark.asyncio
async def test_delete_returns_whether_row_removed(db_session, context_id):
    svc = AgentStateService(db_session)
    await svc.set_state(context_id, "k", 1)
    assert await svc.delete_state(context_id, "k") is True
    assert await svc.delete_state(context_id, "k") is False
    assert await svc.get_state(context_id, "k") is None


@pytest.mark.asyncio
async def test_ttl_clamped_to_max(db_session, context_id):
    svc = AgentStateService(db_session)
    await svc.set_state(context_id, "k", 1, ttl_seconds=MAX_TTL_SECONDS * 100)
    expires = await _expires_at(db_session, context_id, "k")
    # Clamped to MAX_TTL, not 100x it.
    assert (expires - utcnow()).total_seconds() <= MAX_TTL_SECONDS + 5
