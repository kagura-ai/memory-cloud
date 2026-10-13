"""#1969: the daily sweep stamps / clears ``workspaces.capacity_locked_since``
and purges a workspace over the Free capacity for 30 days — real queries.

The test database is shared and not truncated between tests, so the sweep also
visits Free, billing-managed workspaces other tests left behind. Assertions
therefore pin THIS test's workspace and treat the run counts as lower bounds.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import (
    ENTITLEMENT_SOURCE_ADMIN_GRANT,
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from models.memory import Memory
from services.capacity_purge import stamp_capacity_locks
from utils.datetime import utcnow

from ._admin_helpers import make_context, make_user, make_workspace

MEMORY_LIMIT = 2
STORAGE_LIMIT = 200


@pytest.fixture(autouse=True)
def _small_limits():
    with (
        patch.object(Workspace, "effective_memory_limit", property(lambda self: MEMORY_LIMIT)),
        patch.object(
            Workspace, "effective_storage_limit_bytes", property(lambda self: STORAGE_LIMIT)
        ),
    ):
        yield


def _memory(ws, ctx, owner: str) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        user_id=owner,
        workspace_id=ws.id,
        context_id=ctx.id,
        summary=f"mem-{uuid.uuid4().hex[:6]}",
        content="x",
        type="note",
        client="test",
        scope="working",
    )


@pytest_asyncio.fixture
async def free_ws(db_session: AsyncSession):
    """A Free, billing-managed workspace with its owner as a member and one context."""
    owner = make_user()
    db_session.add(owner)
    await db_session.flush()
    ws = make_workspace(owner_user_id=owner.user_id, plan_name="free")
    ws.entitlement_source = ENTITLEMENT_SOURCE_EXTERNAL_BILLING
    db_session.add(ws)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=owner.user_id, role=WorkspaceRole.OWNER)
    )
    ctx = make_context(workspace_id=ws.id, created_by=owner.user_id)
    db_session.add(ctx)
    await db_session.flush()
    return ws, ctx, owner


async def _over_cap(db_session: AsyncSession, ws, ctx, owner) -> None:
    for _ in range(MEMORY_LIMIT + 1):
        db_session.add(_memory(ws, ctx, owner.user_id))
    await db_session.flush()


@pytest.mark.asyncio
async def test_stamp_sets_once_and_keeps_the_first_date(db_session, free_ws) -> None:
    ws, ctx, owner = free_ws
    await _over_cap(db_session, ws, ctx, owner)
    t0 = utcnow().replace(microsecond=0)
    counts = await stamp_capacity_locks(db_session, now=t0)
    assert counts.stamped >= 1
    await db_session.refresh(ws)
    assert ws.capacity_locked_since == t0

    counts = await stamp_capacity_locks(db_session, now=t0 + timedelta(days=1))
    assert counts.kept >= 1
    await db_session.refresh(ws)
    assert ws.capacity_locked_since == t0  # the clock is not restarted


@pytest.mark.asyncio
async def test_stamp_clears_when_within_capacity(db_session, free_ws) -> None:
    ws, ctx, owner = free_ws
    ws.capacity_locked_since = utcnow() - timedelta(days=10)
    await db_session.flush()
    counts = await stamp_capacity_locks(db_session, now=utcnow())
    assert counts.cleared >= 1
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is None


@pytest.mark.asyncio
async def test_stamp_clears_when_no_longer_candidate(db_session, free_ws) -> None:
    """Re-subscribed (paid) while stamped: un-stamped without counting anything."""
    ws, ctx, owner = free_ws
    await _over_cap(db_session, ws, ctx, owner)
    ws.capacity_locked_since = utcnow() - timedelta(days=10)
    ws.plan_name = "pro"
    await db_session.flush()
    counts = await stamp_capacity_locks(db_session, now=utcnow())
    assert counts.cleared >= 1
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is None


@pytest.mark.asyncio
async def test_stamp_ignores_admin_managed_free(db_session, free_ws) -> None:
    ws, ctx, owner = free_ws
    await _over_cap(db_session, ws, ctx, owner)
    ws.entitlement_source = ENTITLEMENT_SOURCE_ADMIN_GRANT
    await db_session.flush()
    await stamp_capacity_locks(db_session, now=utcnow())
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is None


@pytest.mark.asyncio
async def test_stamp_skips_soft_deleted_workspace(db_session, free_ws) -> None:
    ws, ctx, owner = free_ws
    await _over_cap(db_session, ws, ctx, owner)
    ws.deleted_at = utcnow()
    await db_session.flush()
    await stamp_capacity_locks(db_session, now=utcnow())
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is None


@pytest.mark.asyncio
async def test_stamp_commits_in_batches(db_session, free_ws) -> None:
    """With a batch of 1 every workspace is still evaluated and the stamp lands."""
    ws, ctx, owner = free_ws
    await _over_cap(db_session, ws, ctx, owner)
    t0 = utcnow().replace(microsecond=0)
    with patch("services.capacity_purge.STAMP_COMMIT_EVERY", 1):
        counts = await stamp_capacity_locks(db_session, now=t0)
    assert counts.evaluated >= 1
    assert counts.evaluated >= counts.stamped + counts.cleared + counts.kept
    await db_session.refresh(ws)
    assert ws.capacity_locked_since == t0
