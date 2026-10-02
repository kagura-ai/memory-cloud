"""#1798: deleting a context removes its points, after the commit, best-effort."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from db.point_writer_lock import hold_point_writer_lock, wait_for_point_writers
from models.auth import Context, Workspace, WorkspaceMember, WorkspaceRole
from services.context_service import ContextService
from utils.exceptions import QdrantError

_USER = "points_user"


async def _seed(db_session) -> Context:
    workspace = Workspace(id=uuid4(), name="Points", owner_user_id=_USER)
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=_USER, role=WorkspaceRole.OWNER)
    )
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"points_{uuid4().hex[:8]}",
        display_name="Points",
        created_by=_USER,
        is_private=False,
    )
    db_session.add(context)
    await db_session.flush()
    return context


@pytest.mark.asyncio
async def test_delete_context_removes_points_after_the_commit(db_session):
    context = await _seed(db_session)
    order: list[str] = []
    db_session.commit = AsyncMock(side_effect=lambda: order.append("commit"))

    async def remove(workspace_id, context_id):
        order.append("points")
        assert (workspace_id, context_id) == (str(context.workspace_id), str(context.id))
        return {"kagura_memories": 4}

    with patch("db.qdrant.delete_context_points_everywhere", remove):
        await ContextService(db_session).delete_context(_USER, context.id)

    assert order == ["commit", "points"]


@pytest.mark.asyncio
async def test_vector_store_outage_does_not_fail_the_delete(db_session):
    context = await _seed(db_session)
    db_session.commit = AsyncMock()

    with (
        patch(
            "db.qdrant.delete_context_points_everywhere",
            AsyncMock(side_effect=QdrantError("down")),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        deleted = await ContextService(db_session).delete_context(_USER, context.id)

    assert deleted.deleted_at is not None
    assert any(entry["event"] == "context_points_delete_failed" for entry in logs)


@pytest.mark.asyncio
async def test_caller_owned_transaction_leaves_points_to_the_caller(db_session):
    """merge_contexts commits later; removing points before that would outrun a rollback."""
    context = await _seed(db_session)
    remove = AsyncMock(return_value={})

    with patch("db.qdrant.delete_context_points_everywhere", remove):
        await ContextService(db_session).delete_context(_USER, context.id, _commit=False)

    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_point_writer_lock_keeps_the_sweep_out_until_the_writer_ends(
    db_session, async_engine
):
    """A writer holds the lock shared while its points have no committed row;
    the sweep's exclusive request waits for that transaction."""
    await hold_point_writer_lock(db_session)
    async with AsyncSession(async_engine) as sweeper:
        waiting = asyncio.ensure_future(wait_for_point_writers(sweeper, timeout_seconds=30))
        try:
            done, _ = await asyncio.wait({waiting}, timeout=0.5)
            assert not done, "the sweep took the lock while a writer was mid-transaction"

            await db_session.rollback()

            assert await asyncio.wait_for(waiting, timeout=5) is True
        finally:
            waiting.cancel()
            await db_session.rollback()
            await sweeper.rollback()


@pytest.mark.asyncio
async def test_sweep_gives_up_on_a_writer_that_outlasts_the_timeout(db_session, async_engine):
    await hold_point_writer_lock(db_session)
    try:
        async with AsyncSession(async_engine) as sweeper:
            assert await wait_for_point_writers(sweeper, timeout_seconds=0.2) is False
    finally:
        await db_session.rollback()


@pytest.mark.asyncio
async def test_writers_do_not_block_each_other(db_session, async_engine):
    await hold_point_writer_lock(db_session)
    try:
        async with AsyncSession(async_engine) as other_writer:
            await asyncio.wait_for(hold_point_writer_lock(other_writer), timeout=5)
            await other_writer.rollback()
    finally:
        await db_session.rollback()
