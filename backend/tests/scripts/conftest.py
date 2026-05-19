"""Shared fixtures for scripts tests (Issue #722)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.memory import Memory


async def _make_context_with_n_memories(db: AsyncSession, n: int, prefix: str) -> dict:
    """Build a fresh workspace + context + ``n`` alive memories under a unique user_id.

    Unique user_id prevents fixture data from leaking across tests that share
    the integration DB (see Task 6 isolation pattern).
    """
    uid = f"backfill-{uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid4(),
        name=f"backfill-{prefix}-ws-{uuid4().hex[:8]}",
        owner_user_id=uid,
    )
    db.add(ws)
    await db.flush()

    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"backfill-{prefix}-{uuid4().hex[:8]}",
        created_by=uid,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()

    memory_ids: list[UUID] = []
    for i in range(n):
        mid = uuid4()
        memory_ids.append(mid)
        db.add(
            Memory(
                id=mid,
                user_id=uid,
                workspace_id=ws.id,
                context_id=ctx.id,
                summary=f"{prefix} memory {i}",
                content=f"content {i}",
                type="note",
                client="test",
            )
        )
    await db.flush()

    return {
        "context_id": ctx.id,
        "memory_ids": memory_ids,
        "user_id": uid,
        "workspace_id": ws.id,
    }


@pytest_asyncio.fixture
async def small_context_30_memories(db_session) -> UUID:
    """Context with 30 alive memories — exercises the 'below_memory_floor' skip path."""
    data = await _make_context_with_n_memories(db_session, 30, "small")
    return data["context_id"]


@pytest_asyncio.fixture
async def large_context_60_memories(db_session) -> dict:
    """Context with 60 alive memories — exercises the happy-path and dry-run paths.

    Returns a dict with ``context_id``, ``memory_ids`` (deterministic order),
    ``user_id``, and ``workspace_id`` so tests can mock Qdrant against the
    same set of memory UUIDs the fixture wrote.
    """
    return await _make_context_with_n_memories(db_session, 60, "large")
