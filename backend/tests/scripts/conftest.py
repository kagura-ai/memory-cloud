"""Shared fixtures for scripts tests (Issue #722)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest_asyncio

from models.auth import Context, Workspace
from models.memory import Memory


@pytest_asyncio.fixture
async def small_context_30_memories(db_session) -> UUID:
    """Create a workspace + context with 30 alive memories.

    Returns the context UUID. Used to test the 'below_memory_floor' skip path.
    Uses a unique user_id to avoid leakage across tests (Task 6 pattern).
    """
    uid = f"backfill-{uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid4(),
        name=f"backfill-small-ws-{uuid4().hex[:8]}",
        owner_user_id=uid,
    )
    db_session.add(ws)
    await db_session.flush()

    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"backfill-small-{uuid4().hex[:8]}",
        created_by=uid,
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    for i in range(30):
        db_session.add(
            Memory(
                id=uuid4(),
                user_id=uid,
                workspace_id=ws.id,
                context_id=ctx.id,
                summary=f"small context memory {i}",
                content=f"content {i}",
                type="note",
                client="test",
            )
        )
    await db_session.flush()
    return ctx.id


@pytest_asyncio.fixture
async def large_context_60_memories(db_session) -> dict:
    """Create a workspace + context with 60 alive memories.

    Returns a dict with:
        - "context_id": UUID of the context
        - "memory_ids": list of UUID for all 60 memories (deterministic order)
        - "user_id": the owner user_id string
        - "workspace_id": UUID of the workspace

    Used to test the happy-path and dry-run paths.
    Uses a unique user_id to avoid leakage across tests.
    """
    uid = f"backfill-{uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid4(),
        name=f"backfill-large-ws-{uuid4().hex[:8]}",
        owner_user_id=uid,
    )
    db_session.add(ws)
    await db_session.flush()

    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"backfill-large-{uuid4().hex[:8]}",
        created_by=uid,
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    memory_ids: list[UUID] = []
    for i in range(60):
        mid = uuid4()
        memory_ids.append(mid)
        db_session.add(
            Memory(
                id=mid,
                user_id=uid,
                workspace_id=ws.id,
                context_id=ctx.id,
                summary=f"large context memory {i}",
                content=f"content {i}",
                type="note",
                client="test",
            )
        )
    await db_session.flush()

    return {
        "context_id": ctx.id,
        "memory_ids": memory_ids,
        "user_id": uid,
        "workspace_id": ws.id,
    }
