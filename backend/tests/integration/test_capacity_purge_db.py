"""#1969: the daily sweep stamps / clears ``workspaces.capacity_locked_since``
and purges a workspace over the Free capacity for 30 days — real queries.

The test database is shared and not truncated between tests, so the sweep also
visits Free, billing-managed workspaces other tests left behind. Assertions
therefore pin THIS test's workspace and treat the run counts as lower bounds.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import (
    ENTITLEMENT_SOURCE_ADMIN_GRANT,
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Context,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from models.file_objects import FileObject
from models.memory import Memory
from services.capacity_purge import stamp_capacity_locks
from services.context_service import CAPACITY_PURGE_ACTOR, ContextService
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


# ---------------------------------------------------------------------------
# ContextService.purge_workspace_data (Task 6)
# ---------------------------------------------------------------------------


def _file(ws, ctx, owner: str, *, size: int, status: str = "uploaded") -> FileObject:
    """Same kwargs as ``test_context_delete_files._file``."""
    sha = uuid.uuid4().hex + uuid.uuid4().hex
    return FileObject(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        context_id=ctx.id if ctx is not None else None,
        sha256=sha,
        size_bytes=size,
        content_type="application/octet-stream",
        filename="f.bin",
        storage_backend="r2",
        storage_key=f"{ws.id}/{sha[:2]}/{sha}",
        status=status,
        created_by=owner,
    )


async def _live_contexts(db_session: AsyncSession, ws) -> list[Context]:
    rows = await db_session.execute(
        select(Context).where(Context.workspace_id == ws.id, Context.deleted_at.is_(None))
    )
    return list(rows.scalars().all())


async def _live_memories(db_session: AsyncSession, ws) -> list[Memory]:
    rows = await db_session.execute(
        select(Memory).where(Memory.workspace_id == ws.id, Memory.deleted_at.is_(None))
    )
    return list(rows.scalars().all())


async def _live_files(db_session: AsyncSession, ws) -> list[FileObject]:
    rows = await db_session.execute(
        select(FileObject).where(FileObject.workspace_id == ws.id, FileObject.deleted_at.is_(None))
    )
    return list(rows.scalars().all())


@pytest.fixture
def purge_side_effects():
    """Vector removal and the Redis release are post-commit side effects."""
    with (
        patch.object(ContextService, "_remove_context_points", new=AsyncMock()) as points,
        patch("services.storage_quota_service.release_storage_bytes", new=AsyncMock()) as release,
    ):
        yield points, release


@pytest.mark.asyncio
async def test_purge_workspace_data_locked_private_and_default(
    db_session, free_ws, purge_side_effects
) -> None:
    """Every live context goes (locked and private included); the default
    context stays, empty; workspace-scoped files go too; the report is exact."""
    points, release = purge_side_effects
    ws, ctx, owner = free_ws
    default = Context(workspace_id=ws.id, name="default", created_by=owner.user_id)
    locked = make_context(workspace_id=ws.id, created_by=owner.user_id)
    locked.is_locked = True
    private = make_context(workspace_id=ws.id, created_by="someone-else", is_private=True)
    db_session.add_all([default, locked, private])
    await db_session.flush()
    for c in (default, ctx, locked, private):
        db_session.add(_memory(ws, c, owner.user_id))
    db_session.add(_file(ws, ctx, owner.user_id, size=50))
    db_session.add(_file(ws, None, owner.user_id, size=60))  # workspace-scoped
    db_session.add(_file(ws, locked, owner.user_id, size=5, status="reserved"))  # in flight
    await db_session.flush()

    report = await ContextService(db_session).purge_workspace_data(
        ws.id, actor=CAPACITY_PURGE_ACTOR
    )

    assert report.contexts_deleted == 3
    assert report.contexts_emptied == 1
    assert report.memories_deleted == 4
    assert report.files_deleted == 2  # uploaded rows; the cancelled reservation is not a file
    assert report.bytes_released == 50 + 60 + 5
    assert report.aborted is False
    assert [c.name for c in await _live_contexts(db_session, ws)] == ["default"]
    for c in (ctx, locked, private):
        await db_session.refresh(c)
        assert c.deleted_at is not None and c.deleted_by == CAPACITY_PURGE_ACTOR
    await db_session.refresh(default)
    assert default.deleted_at is None
    assert await _live_memories(db_session, ws) == []
    tombstoned = (
        (await db_session.execute(select(Memory).where(Memory.workspace_id == ws.id)))
        .scalars()
        .all()
    )
    assert {m.deleted_by for m in tombstoned} == {CAPACITY_PURGE_ACTOR}
    assert await _live_files(db_session, ws) == []
    assert points.await_count == 4  # every context, the default one included
    assert {c.args[1] for c in points.await_args_list} == {
        str(c.id) for c in (default, ctx, locked, private)
    }
    released = sum(c.kwargs["size_bytes"] for c in release.await_args_list)
    assert released == 50 + 60 + 5


@pytest.mark.asyncio
async def test_purge_chunks_memories(db_session, free_ws) -> None:
    """The purge path tombstones memories in bulk UPDATE chunks, one commit each."""
    ws, ctx, owner = free_ws
    for _ in range(5):
        db_session.add(_memory(ws, ctx, owner.user_id))
    await db_session.flush()
    t = utcnow()
    commits = 0
    real_commit = db_session.commit

    async def counting_commit() -> None:
        nonlocal commits
        commits += 1
        await real_commit()

    with patch.object(db_session, "commit", counting_commit):
        total = await ContextService(db_session)._tombstone_memories_chunked(
            ctx, actor=CAPACITY_PURGE_ACTOR, deleted_at=t, chunk=2
        )

    assert total == 5
    assert commits == 3  # 2 + 2 + 1
    rows = (
        (await db_session.execute(select(Memory).where(Memory.context_id == ctx.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 5
    for m in rows:
        await db_session.refresh(m)
        assert (m.deleted_at, m.deleted_by) == (t, CAPACITY_PURGE_ACTOR)


@pytest.mark.asyncio
async def test_purge_workspace_data_never_loads_memories_as_orm(
    db_session, free_ws, purge_side_effects
) -> None:
    """``purge_workspace_data`` goes through the chunked UPDATE, not the ORM loop
    ``delete_context`` keeps (100k-memory former paid workspaces)."""
    ws, ctx, owner = free_ws
    for _ in range(5):
        db_session.add(_memory(ws, ctx, owner.user_id))
    await db_session.flush()
    with (
        patch("services.context_service.PURGE_MEMORY_CHUNK", 2),
        patch.object(
            ContextService,
            "_tombstone_memories_orm",
            new=AsyncMock(side_effect=AssertionError("ORM path used")),
        ),
    ):
        report = await ContextService(db_session).purge_workspace_data(
            ws.id, actor=CAPACITY_PURGE_ACTOR
        )
    assert report.memories_deleted == 5
    assert await _live_memories(db_session, ws) == []


@pytest.mark.asyncio
async def test_purge_workspace_data_stops_when_told_to(
    db_session, free_ws, purge_side_effects
) -> None:
    """``should_continue`` is asked before each context and before the
    workspace-scoped files; False stops the purge with a partial report."""
    ws, ctx, owner = free_ws
    second = make_context(workspace_id=ws.id, created_by=owner.user_id)
    db_session.add(second)
    await db_session.flush()
    db_session.add(_file(ws, None, owner.user_id, size=60))
    await db_session.flush()
    answers = iter([True, False])
    asked = 0

    async def should_continue() -> bool:
        nonlocal asked
        asked += 1
        return next(answers)

    report = await ContextService(db_session).purge_workspace_data(
        ws.id, actor=CAPACITY_PURGE_ACTOR, should_continue=should_continue
    )

    assert asked == 2
    assert report.aborted is True
    assert report.contexts_deleted == 1
    live = await _live_contexts(db_session, ws)
    assert len(live) == 1 and live[0].id in {ctx.id, second.id}
    assert len(await _live_files(db_session, ws)) == 1  # workspace-scoped file untouched
