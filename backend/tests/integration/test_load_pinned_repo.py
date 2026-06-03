"""Integration pins for the deterministic always-load query (#886).

MemoryRepository.list_pinned is the determinism-critical read path: it must be
complete (no ranking), bounded (LIMIT cap + accurate total), and fully ordered
down to the tie-break. These properties only hold against a real PostgreSQL
engine, so they are pinned here rather than with mocks.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from repositories.memory import MemoryRepository


def _dt(value):
    """asyncpg binds timestamp params as datetime objects, not ISO strings."""
    return datetime.fromisoformat(value) if value is not None else None


async def _seed_workspace(db_session, workspace_id, owner="user-886"):
    await db_session.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :owner, 'free')"
        ),
        {"id": workspace_id, "name": f"ws-{workspace_id.hex[:8]}", "owner": owner},
    )


async def _seed_context(db_session, workspace_id, context_id, owner="user-886"):
    await db_session.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by, is_private, is_public) "
            "VALUES (:id, :ws, :name, :owner, false, false)"
        ),
        {"id": context_id, "ws": workspace_id, "name": f"ctx-{context_id.hex[:8]}", "owner": owner},
    )


async def _insert_memory(
    db_session,
    *,
    mem_id,
    workspace_id,
    context_id,
    delivery_mode="always",
    importance=0.5,
    created_at="2026-06-01T00:00:00",
    deleted_at=None,
    owner="user-886",
):
    await db_session.execute(
        text(
            """
            INSERT INTO memories
              (id, user_id, workspace_id, context_id, summary, context_summary, content,
               type, importance, confidence, scope, delivery_mode, embedding_status,
               client, source, long_term, use_count, access_count, created_at, deleted_at)
            VALUES
              (:id, :owner, :ws, :ctx, :summary, :ctxsum, :content,
               'note', :imp, 1.0, 'persistent', :dm, 'success',
               'test', 'mcp_remember', false, 0, 0, :created, :deleted)
            """
        ),
        {
            "id": mem_id,
            "owner": owner,
            "ws": workspace_id,
            "ctx": context_id,
            "summary": f"summary-{mem_id.hex[:6]}",
            "ctxsum": f"ctxsum-{mem_id.hex[:6]}",
            "content": "FULL CONTENT should never be in the pinned read",
            "imp": importance,
            "dm": delivery_mode,
            "created": _dt(created_at),
            "deleted": _dt(deleted_at),
        },
    )


@pytest.mark.asyncio
async def test_list_pinned_returns_only_always_in_context(db_session):
    ws, ctx_a, ctx_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_workspace(db_session, ws)
    await _seed_context(db_session, ws, ctx_a)
    await _seed_context(db_session, ws, ctx_b)
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    await _insert_memory(db_session, mem_id=a1, workspace_id=ws, context_id=ctx_a, delivery_mode="always")
    await _insert_memory(db_session, mem_id=a2, workspace_id=ws, context_id=ctx_a, delivery_mode="always")
    await _insert_memory(db_session, mem_id=uuid.uuid4(), workspace_id=ws, context_id=ctx_a, delivery_mode="on_recall")
    await _insert_memory(db_session, mem_id=uuid.uuid4(), workspace_id=ws, context_id=ctx_a, delivery_mode="on_trigger")
    # always memory in a DIFFERENT context must not leak in
    await _insert_memory(db_session, mem_id=uuid.uuid4(), workspace_id=ws, context_id=ctx_b, delivery_mode="always")

    repo = MemoryRepository(db_session)
    rows, total = await repo.list_pinned(ws, ctx_a, limit=100)
    assert total == 2
    assert {r.id for r in rows} == {a1, a2}


@pytest.mark.asyncio
async def test_list_pinned_deterministic_order(db_session):
    """importance DESC, then created_at ASC, then id ASC (full tie-break)."""
    ws, ctx = uuid.uuid4(), uuid.uuid4()
    await _seed_workspace(db_session, ws)
    await _seed_context(db_session, ws, ctx)
    high = uuid.uuid4()
    early = uuid.uuid4()
    late = uuid.uuid4()
    await _insert_memory(db_session, mem_id=late, workspace_id=ws, context_id=ctx, importance=0.5, created_at="2026-06-02T00:00:00")
    await _insert_memory(db_session, mem_id=early, workspace_id=ws, context_id=ctx, importance=0.5, created_at="2026-06-01T00:00:00")
    await _insert_memory(db_session, mem_id=high, workspace_id=ws, context_id=ctx, importance=0.9, created_at="2026-06-03T00:00:00")

    repo = MemoryRepository(db_session)
    rows, total = await repo.list_pinned(ws, ctx, limit=100)
    assert total == 3
    # highest importance first; among equal importance, earlier created_at first
    assert [r.id for r in rows] == [high, early, late]


@pytest.mark.asyncio
async def test_list_pinned_excludes_soft_deleted(db_session):
    ws, ctx = uuid.uuid4(), uuid.uuid4()
    await _seed_workspace(db_session, ws)
    await _seed_context(db_session, ws, ctx)
    live = uuid.uuid4()
    await _insert_memory(db_session, mem_id=live, workspace_id=ws, context_id=ctx)
    await _insert_memory(
        db_session, mem_id=uuid.uuid4(), workspace_id=ws, context_id=ctx,
        deleted_at="2026-06-01T00:00:00",
    )
    repo = MemoryRepository(db_session)
    rows, total = await repo.list_pinned(ws, ctx, limit=100)
    assert total == 1
    assert [r.id for r in rows] == [live]


@pytest.mark.asyncio
async def test_list_pinned_cap_bounds_rows_but_total_is_accurate(db_session):
    ws, ctx = uuid.uuid4(), uuid.uuid4()
    await _seed_workspace(db_session, ws)
    await _seed_context(db_session, ws, ctx)
    for i in range(5):
        await _insert_memory(
            db_session, mem_id=uuid.uuid4(), workspace_id=ws, context_id=ctx,
            importance=0.5, created_at=f"2026-06-0{i + 1}T00:00:00",
        )
    repo = MemoryRepository(db_session)
    rows, total = await repo.list_pinned(ws, ctx, limit=3)
    assert len(rows) == 3
    assert total == 5
