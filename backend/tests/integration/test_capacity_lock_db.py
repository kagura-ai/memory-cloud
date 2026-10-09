"""DB-level pins for the capacity-over lock (#1941).

The predicate and its call sites are unit-tested with stubbed reads; this file
runs the real queries: the live memory COUNT, the storage counter, the
deleted-context file exclusion, and the context → workspace subquery with the
candidate filter. Limits are patched small so a handful of rows is "over".
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import (
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from models.file_objects import FileObject, WorkspaceStorageUsage
from models.memory import Memory
from services.capacity_lock import (
    capacity_lock_state,
    ensure_context_not_capacity_locked,
    ensure_contexts_not_capacity_locked,
)
from utils.datetime import utcnow
from utils.exceptions import CapacityLockedError

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


def _memory(ws, ctx, owner) -> Memory:
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


def _file(ws, ctx, owner, size: int) -> FileObject:
    sha = uuid.uuid4().hex + uuid.uuid4().hex
    return FileObject(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        context_id=ctx.id,
        sha256=sha,
        size_bytes=size,
        content_type="application/octet-stream",
        filename="f.bin",
        storage_backend="r2",
        storage_key=f"{ws.id}/{sha[:2]}/{sha}",
        status="uploaded",
        created_by=owner,
    )


@pytest_asyncio.fixture
async def free_ws(db_session: AsyncSession):
    owner = make_user()
    db_session.add(owner)
    await db_session.flush()
    ws = make_workspace(owner_user_id=owner.user_id, plan_name="free")
    ws.entitlement_source = ENTITLEMENT_SOURCE_EXTERNAL_BILLING
    db_session.add(ws)
    await db_session.flush()
    ctx = make_context(workspace_id=ws.id, created_by=owner.user_id, is_private=True)
    db_session.add(ctx)
    await db_session.commit()
    return ws, ctx, owner.user_id


@pytest.mark.asyncio
async def test_one_memory_over_locks_and_deleting_one_unlocks(db_session, free_ws) -> None:
    ws, ctx, owner = free_ws
    memories = [_memory(ws, ctx, owner) for _ in range(MEMORY_LIMIT + 1)]
    db_session.add_all(memories)
    await db_session.commit()

    lock = await capacity_lock_state(db_session, ws)
    assert lock is not None
    assert (lock.memory_count, lock.over_memories) == (MEMORY_LIMIT + 1, 1)

    memories[0].deleted_at = utcnow()
    await db_session.commit()
    assert await capacity_lock_state(db_session, ws) is None


@pytest.mark.asyncio
async def test_files_of_a_deleted_context_do_not_hold_the_lock(db_session, free_ws) -> None:
    ws, ctx, owner = free_ws
    gone = make_context(workspace_id=ws.id, created_by=owner, is_private=True)
    db_session.add(gone)
    await db_session.flush()
    db_session.add_all([_file(ws, ctx, owner, 100), _file(ws, gone, owner, 200)])
    db_session.add(WorkspaceStorageUsage(workspace_id=ws.id, used_bytes=300, file_count=2))
    await db_session.commit()

    # Over the 200-byte limit while the second context is live…
    lock = await capacity_lock_state(db_session, ws)
    assert lock is not None and lock.over_bytes == 100

    # …and not once it is soft-deleted: its files are invisible to the owner.
    gone.deleted_at = utcnow()
    await db_session.commit()
    assert await capacity_lock_state(db_session, ws) is None


@pytest.mark.asyncio
async def test_the_context_subquery_finds_the_locked_workspace(db_session, free_ws) -> None:
    ws, ctx, owner = free_ws
    db_session.add_all([_memory(ws, ctx, owner) for _ in range(MEMORY_LIMIT + 1)])
    await db_session.commit()

    with pytest.raises(CapacityLockedError) as exc:
        await ensure_context_not_capacity_locked(db_session, ctx.id)
    assert exc.value.details["over_memories"] == 1
    with pytest.raises(CapacityLockedError):
        await ensure_contexts_not_capacity_locked(db_session, [uuid.uuid4(), ctx.id])


@pytest.mark.asyncio
async def test_the_candidate_filter_skips_a_paid_workspace(db_session, free_ws) -> None:
    ws, ctx, owner = free_ws
    db_session.add_all([_memory(ws, ctx, owner) for _ in range(MEMORY_LIMIT + 5)])
    ws.plan_name = "pro"
    await db_session.commit()
    await ensure_context_not_capacity_locked(db_session, ctx.id)
    assert await capacity_lock_state(db_session, ws) is None


@pytest.mark.asyncio
async def test_bulk_delete_unlocks_on_the_real_schema(db_session, free_ws) -> None:
    """forget_bulk's chunk statements (UPDATE … RETURNING, the shared-point
    read, the edge DELETE) against real tables, and the lock lifting after."""
    from unittest.mock import AsyncMock

    from services.memory_service import MemoryService

    ws, ctx, owner = free_ws
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    db_session.add_all([_memory(ws, ctx, owner) for _ in range(MEMORY_LIMIT + 3)])
    await db_session.commit()
    assert await capacity_lock_state(db_session, ws) is not None

    service = MemoryService(db_session)
    with (
        patch("db.qdrant.delete_points_from_qdrant", AsyncMock()) as points,
        patch(
            "services.memory_service.resolve_collection_name",
            AsyncMock(return_value="kagura_memories"),
        ),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
    ):
        count, remaining, _ = await service.forget_bulk(
            owner, context_id=ctx.id, created_before=utcnow(), dry_run=False, chunk_size=2
        )
    assert (count, remaining) == (MEMORY_LIMIT + 3, False)
    assert points.await_count == 3
    assert await capacity_lock_state(db_session, ws) is None
