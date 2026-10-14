"""#1969: the daily sweep stamps / clears ``workspaces.capacity_locked_since``
and purges a workspace over the Free capacity for 30 days — real queries.

The test database is shared and not truncated between tests, so the sweep also
visits Free, billing-managed workspaces other tests left behind. Assertions
therefore pin THIS test's workspace and treat the run counts as lower bounds.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import (
    ENTITLEMENT_SOURCE_ADMIN_GRANT,
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    AuditLog,
    Context,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from models.file_objects import FileObject
from models.memory import Memory
from services import capacity_purge
from services.capacity_lock import capacity_lock_state
from services.capacity_purge import purge_expired_workspaces, stamp_capacity_locks
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
    return await _make_free_ws(db_session)


async def _make_free_ws(db_session: AsyncSession):
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
async def test_stamp_clear_also_clears_the_final_notice(db_session, free_ws) -> None:
    """A cleanup restarts the clock: the next lock needs its own day-29 mail."""
    ws, ctx, owner = free_ws
    ws.capacity_locked_since = utcnow() - timedelta(days=30)
    ws.capacity_purge_final_notice_at = utcnow() - timedelta(days=1)
    ws.capacity_purge_first_notice_at = utcnow() - timedelta(days=30)
    await db_session.flush()
    await stamp_capacity_locks(db_session, now=utcnow())
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is None
    assert ws.capacity_purge_final_notice_at is None
    assert ws.capacity_purge_first_notice_at is None


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


@pytest.mark.parametrize("failure", ["raise", "sql_error"])
@pytest.mark.asyncio
async def test_stamp_continues_after_one_workspace_fails(db_session, free_ws, failure) -> None:
    """Measuring one workspace raises — a plain exception, or a database error
    that aborts the transaction: it is logged and skipped, and the others are
    still stamped (and an earlier stamp in the same batch is not lost)."""
    import structlog
    from sqlalchemy import text

    first, first_ctx, first_owner = free_ws
    bad, bad_ctx, bad_owner = await _make_free_ws(db_session)
    good, good_ctx, good_owner = await _make_free_ws(db_session)
    for ws, ctx, owner in (
        (first, first_ctx, first_owner),
        (bad, bad_ctx, bad_owner),
        (good, good_ctx, good_owner),
    ):
        await _over_cap(db_session, ws, ctx, owner)
    await db_session.commit()
    real_state = capacity_purge.capacity_lock_state
    bad_id = bad.id

    async def state_or_fail(db, ws):
        if ws.id == bad_id:
            if failure == "raise":
                raise RuntimeError("boom")
            await db.execute(text("SELECT 1/0"))
        return await real_state(db, ws)

    t0 = utcnow().replace(microsecond=0)
    with (
        patch("services.capacity_purge.STAMP_COMMIT_EVERY", 10_000),
        patch.object(capacity_purge, "capacity_lock_state", state_or_fail),
        structlog.testing.capture_logs() as logs,
    ):
        counts = await stamp_capacity_locks(db_session, now=t0)

    assert counts.failed >= 1
    failed = [e for e in logs if e["event"] == "capacity_lock_stamp_failed"]
    assert str(bad_id) in {e["workspace_id"] for e in failed}
    for ws in (first, good):
        await db_session.refresh(ws)
        assert ws.capacity_locked_since == t0
    await db_session.refresh(bad)
    assert bad.capacity_locked_since is None


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
        total, aborted = await ContextService(db_session)._tombstone_memories_chunked(
            ctx, actor=CAPACITY_PURGE_ACTOR, deleted_at=t, chunk=2
        )

    assert (total, aborted) == (5, False)
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


@pytest.mark.asyncio
async def test_purge_stops_between_memory_chunks(db_session, free_ws, purge_side_effects) -> None:
    """A re-subscribe during one huge context stops the purge within one chunk:
    ``should_continue`` is asked after every committed chunk, and the context
    itself is neither detached nor soft-deleted."""
    ws, ctx, owner = free_ws
    for _ in range(5):
        db_session.add(_memory(ws, ctx, owner.user_id))
    db_session.add(_file(ws, ctx, owner.user_id, size=50))
    await db_session.flush()
    answers = iter([True, False])  # before the context; after the first chunk

    async def should_continue() -> bool:
        return next(answers)

    with patch("services.context_service.PURGE_MEMORY_CHUNK", 2):
        report = await ContextService(db_session).purge_workspace_data(
            ws.id, actor=CAPACITY_PURGE_ACTOR, should_continue=should_continue
        )

    assert report.aborted is True
    assert report.memories_deleted == 2
    assert (report.contexts_deleted, report.contexts_emptied, report.files_deleted) == (0, 0, 0)
    await db_session.refresh(ctx)
    assert ctx.deleted_at is None
    assert len(await _live_memories(db_session, ws)) == 3
    assert len(await _live_files(db_session, ws)) == 1


@pytest.mark.asyncio
async def test_purge_takes_memories_outside_live_contexts(
    db_session, free_ws, purge_side_effects
) -> None:
    """The lock counts every live memory of the workspace, wherever it sits:
    one with no context and one still live under an already-deleted context
    are tombstoned too, so the workspace ends within capacity."""
    ws, ctx, owner = free_ws
    dead = make_context(workspace_id=ws.id, created_by=owner.user_id)
    dead.deleted_at = utcnow() - timedelta(days=3)
    db_session.add(dead)
    await db_session.flush()
    no_context = _memory(ws, ctx, owner.user_id)
    no_context.context_id = None
    under_dead = _memory(ws, dead, owner.user_id)
    db_session.add_all([no_context, under_dead])
    await _over_cap(db_session, ws, ctx, owner)
    assert await capacity_lock_state(db_session, ws) is not None

    with patch("services.context_service.PURGE_MEMORY_CHUNK", 1):
        report = await ContextService(db_session).purge_workspace_data(
            ws.id, actor=CAPACITY_PURGE_ACTOR
        )

    assert report.aborted is False
    assert report.memories_deleted == MEMORY_LIMIT + 1 + 2
    assert await _live_memories(db_session, ws) == []
    for m in (no_context, under_dead):
        await db_session.refresh(m)
        assert m.deleted_at is not None and m.deleted_by == CAPACITY_PURGE_ACTOR
    assert await capacity_lock_state(db_session, ws) is None


@pytest.mark.asyncio
async def test_purge_takes_the_default_context_last(
    db_session, free_ws, purge_side_effects
) -> None:
    """Non-default contexts go first, the default one last — even when it is
    the oldest — so an abort leaves the default context's memories intact."""
    ws, ctx, owner = free_ws
    default = Context(
        workspace_id=ws.id,
        name="default",
        created_by=owner.user_id,
        created_at=utcnow() - timedelta(days=365),
    )
    db_session.add(default)
    await db_session.flush()
    db_session.add(_memory(ws, default, owner.user_id))
    db_session.add(_memory(ws, ctx, owner.user_id))
    await db_session.flush()

    async def should_continue() -> bool:
        # Re-subscribed as soon as the non-default context is gone.
        return ctx.deleted_at is None

    report = await ContextService(db_session).purge_workspace_data(
        ws.id, actor=CAPACITY_PURGE_ACTOR, should_continue=should_continue
    )

    assert report.aborted is True
    assert (report.contexts_deleted, report.contexts_emptied) == (1, 0)
    await db_session.refresh(ctx)
    assert ctx.deleted_at is not None
    live = await _live_memories(db_session, ws)
    assert [m.context_id for m in live] == [default.id]


# ---------------------------------------------------------------------------
# purge_expired_workspaces (Task 7)
# ---------------------------------------------------------------------------


@pytest.fixture
def only_workspaces():
    """Restrict the purge to this test's workspaces.

    The shared database keeps stamped workspaces from other tests; the purge's
    per-run cap (oldest stamp first) would otherwise let them crowd this
    test's workspace out — and this test must never purge theirs.
    """
    real = capacity_purge._stamped_workspaces
    keep: set = set()

    async def mine(db):
        return [ws for ws in await real(db) if ws.id in keep]

    with patch.object(capacity_purge, "_stamped_workspaces", mine):
        yield keep


def _mail():
    return MagicMock(send_capacity_purge_complete=AsyncMock(return_value=True))


async def _audit_rows(db_session: AsyncSession, ws) -> list[AuditLog]:
    rows = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "capacity_purge", AuditLog.resource == f"workspace:{ws.id}"
        )
    )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_purge_after_thirty_days(
    db_session, free_ws, purge_side_effects, only_workspaces
) -> None:
    ws, ctx, owner = free_ws
    only_workspaces.add(ws.id)
    await _over_cap(db_session, ws, ctx, owner)
    now = utcnow().replace(microsecond=0)
    # Calendar day 30, a few seconds later in the day than this run.
    since = now - timedelta(days=30) + timedelta(seconds=5)
    if since.date() != (now - timedelta(days=30)).date():  # run near midnight
        since = now - timedelta(days=30)
    ws.capacity_locked_since = since
    ws.capacity_purge_final_notice_at = now - timedelta(days=1)  # the day-29 run
    ws.capacity_purge_first_notice_at = now - timedelta(days=30)  # the day-0 run
    await db_session.flush()
    email = _mail()

    counts = await purge_expired_workspaces(db_session, now=now, email=email)

    assert (counts.due, counts.purged, counts.aborted) == (1, 1, 0)
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is None
    assert ws.capacity_purge_final_notice_at is None
    await db_session.refresh(ctx)
    assert ctx.deleted_at is not None and ctx.deleted_by == CAPACITY_PURGE_ACTOR
    assert await _live_memories(db_session, ws) == []
    kwargs = email.send_capacity_purge_complete.call_args.kwargs
    assert kwargs["memories_deleted"] == MEMORY_LIMIT + 1 and kwargs["contexts_deleted"] == 1
    assert kwargs["to_email"] == owner.email
    audit = await _audit_rows(db_session, ws)
    assert len(audit) == 1
    assert audit[0].user_id == CAPACITY_PURGE_ACTOR
    assert audit[0].user_metadata["memories_deleted"] == MEMORY_LIMIT + 1
    assert audit[0].user_metadata["locked_since"] == since.isoformat()
    assert audit[0].user_metadata["aborted"] is False


@pytest.mark.asyncio
async def test_purge_not_before_thirty_days(db_session, free_ws, only_workspaces) -> None:
    """Calendar day 29 is not due, however late in the day."""
    ws, ctx, owner = free_ws
    only_workspaces.add(ws.id)
    await _over_cap(db_session, ws, ctx, owner)
    now = utcnow().replace(microsecond=0)
    ws.capacity_locked_since = now - timedelta(days=29)
    await db_session.flush()
    email = _mail()
    counts = await purge_expired_workspaces(db_session, now=now, email=email)
    assert counts.due == 0
    await db_session.refresh(ctx)
    assert ctx.deleted_at is None
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is not None
    email.send_capacity_purge_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_skips_workspace_now_within_capacity(
    db_session, free_ws, only_workspaces
) -> None:
    """Stale stamp, cleaned up since: nothing deleted, stamp cleared."""
    ws, ctx, owner = free_ws
    only_workspaces.add(ws.id)
    db_session.add(_memory(ws, ctx, owner.user_id))  # within the limit of 2
    now = utcnow()
    ws.capacity_locked_since = now - timedelta(days=31)
    ws.capacity_purge_final_notice_at = now - timedelta(days=2)
    ws.capacity_purge_first_notice_at = now - timedelta(days=30)  # the day-0 run
    await db_session.flush()
    email = _mail()
    counts = await purge_expired_workspaces(db_session, now=now, email=email)
    assert (counts.due, counts.skipped_within, counts.purged) == (1, 1, 0)
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is None
    await db_session.refresh(ctx)
    assert ctx.deleted_at is None
    assert len(await _live_memories(db_session, ws)) == 1
    email.send_capacity_purge_complete.assert_not_awaited()
    assert await _audit_rows(db_session, ws) == []


@pytest.mark.asyncio
async def test_purge_aborts_when_resubscribed_mid_run(
    db_session, free_ws, purge_side_effects, only_workspaces
) -> None:
    """Re-subscribed while the purge runs: the context not yet reached stays,
    the stamp is cleared, the audit row says aborted, no completion mail."""
    ws, ctx, owner = free_ws
    only_workspaces.add(ws.id)
    second = make_context(workspace_id=ws.id, created_by=owner.user_id)
    db_session.add(second)
    await db_session.flush()
    await _over_cap(db_session, ws, ctx, owner)
    db_session.add(_memory(ws, second, owner.user_id))
    now = utcnow()
    ws.capacity_locked_since = now - timedelta(days=30)
    ws.capacity_purge_final_notice_at = now - timedelta(days=1)
    ws.capacity_purge_first_notice_at = now - timedelta(days=30)  # the day-0 run
    await db_session.flush()
    await db_session.commit()

    # The first context is purged; the owner re-subscribes before the second.
    real_tombstone = ContextService._tombstone_memories_chunked
    first_done: list = []

    async def tombstone_then_resubscribe(self, context, **kwargs):
        total = await real_tombstone(self, context, **kwargs)
        if not first_done:
            first_done.append(context.id)
            await self.db.execute(
                Workspace.__table__.update().where(Workspace.id == ws.id).values(plan_name="pro")
            )
        return total

    email = _mail()
    with patch.object(ContextService, "_tombstone_memories_chunked", tombstone_then_resubscribe):
        counts = await purge_expired_workspaces(db_session, now=now, email=email)

    assert (counts.aborted, counts.purged) == (1, 0)
    live = await _live_contexts(db_session, ws)
    assert len(live) == 1 and live[0].id != first_done[0]
    assert len(await _live_memories(db_session, ws)) >= 1
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is None
    assert ws.plan_name == "pro"
    email.send_capacity_purge_complete.assert_not_awaited()
    audit = await _audit_rows(db_session, ws)
    assert len(audit) == 1 and audit[0].user_metadata["aborted"] is True


@pytest.mark.asyncio
async def test_purge_continues_after_one_workspace_fails(
    db_session, free_ws, purge_side_effects, only_workspaces
) -> None:
    """The oldest due workspace's purge raises: the rollback expires every
    loaded row, yet the next workspace is still purged (no lazy load of an
    expired attribute outside the try)."""
    bad, bad_ctx, bad_owner = free_ws
    good, good_ctx, good_owner = await _make_free_ws(db_session)
    only_workspaces.update({bad.id, good.id})
    await _over_cap(db_session, bad, bad_ctx, bad_owner)
    await _over_cap(db_session, good, good_ctx, good_owner)
    now = utcnow().replace(microsecond=0)
    bad.capacity_locked_since = now - timedelta(days=32)  # oldest: goes first
    good.capacity_locked_since = now - timedelta(days=31)
    bad.capacity_purge_final_notice_at = good.capacity_purge_final_notice_at = now - timedelta(
        days=1
    )
    bad.capacity_purge_first_notice_at = good.capacity_purge_first_notice_at = now - timedelta(
        days=30
    )
    await db_session.flush()
    await db_session.commit()

    real_purge = ContextService.purge_workspace_data
    bad_id = bad.id  # the rollback expires ``bad``

    async def purge_or_raise(self, workspace_id, **kwargs):
        if workspace_id == bad_id:
            raise RuntimeError("boom")
        return await real_purge(self, workspace_id, **kwargs)

    email = _mail()
    with patch.object(ContextService, "purge_workspace_data", purge_or_raise):
        counts = await purge_expired_workspaces(db_session, now=now, email=email)

    assert (counts.due, counts.failed, counts.purged) == (2, 1, 1)
    await db_session.refresh(bad)
    assert bad.capacity_locked_since is not None  # retried next run
    await db_session.refresh(bad_ctx)
    assert bad_ctx.deleted_at is None
    await db_session.refresh(good)
    assert good.capacity_locked_since is None
    assert await _live_memories(db_session, good) == []
    assert email.send_capacity_purge_complete.await_count == 1


@pytest.mark.parametrize(
    "final_notice_days_ago",
    [None, 0],  # never warned / warned by this very run
)
@pytest.mark.asyncio
async def test_purge_waits_for_the_final_notice(
    db_session, free_ws, purge_side_effects, only_workspaces, final_notice_days_ago
) -> None:
    """Day 30 and over, but no day-29 mail on an earlier calendar day: nothing goes."""
    ws, ctx, owner = free_ws
    only_workspaces.add(ws.id)
    await _over_cap(db_session, ws, ctx, owner)
    now = utcnow().replace(microsecond=0)
    ws.capacity_locked_since = now - timedelta(days=30)
    if final_notice_days_ago is not None:
        ws.capacity_purge_final_notice_at = now - timedelta(days=final_notice_days_ago)
    ws.capacity_purge_first_notice_at = now - timedelta(days=30)
    await db_session.flush()
    email = _mail()

    counts = await purge_expired_workspaces(db_session, now=now, email=email)

    assert (counts.due, counts.awaiting_notice, counts.purged) == (1, 1, 0)
    await db_session.refresh(ws)
    assert ws.capacity_locked_since is not None
    await db_session.refresh(ctx)
    assert ctx.deleted_at is None
    assert len(await _live_memories(db_session, ws)) == MEMORY_LIMIT + 1
    email.send_capacity_purge_complete.assert_not_awaited()
    assert await _audit_rows(db_session, ws) == []


class _Redis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def delete(self, key):
        self.keys.discard(key)


@pytest.mark.parametrize(("day", "final"), [(27, False), (29, True)])
@pytest.mark.asyncio
async def test_final_notice_is_persisted_by_the_day_29_mail(
    db_session, free_ws, only_workspaces, day, final
) -> None:
    ws, ctx, owner = free_ws
    only_workspaces.add(ws.id)
    await _over_cap(db_session, ws, ctx, owner)
    now = utcnow().replace(microsecond=0)
    ws.capacity_locked_since = now - timedelta(days=day)
    await db_session.flush()
    email = MagicMock(send_capacity_purge_notice=AsyncMock(return_value=True))

    counts = await capacity_purge.send_due_purge_notices(
        db_session, now=now, redis=_Redis(), email=email
    )

    assert counts.sent == 1
    await db_session.refresh(ws)
    assert ws.capacity_purge_final_notice_at == (now if final else None)
    assert ws.capacity_purge_first_notice_at == now  # the first mail of this lock


@pytest.mark.asyncio
async def test_notice_persist_failure_skips_the_mail_and_moves_on(
    db_session, free_ws, only_workspaces
) -> None:
    """The record's commit raises for the first workspace: no mail for it,
    its claim released, nothing recorded — and after the rollback (which
    expires every loaded row) the second workspace still gets its mail."""
    bad, bad_ctx, bad_owner = free_ws
    good, good_ctx, good_owner = await _make_free_ws(db_session)
    only_workspaces.update({bad.id, good.id})
    await _over_cap(db_session, bad, bad_ctx, bad_owner)
    await _over_cap(db_session, good, good_ctx, good_owner)
    now = utcnow().replace(microsecond=0)
    bad.capacity_locked_since = good.capacity_locked_since = now - timedelta(days=29)
    await db_session.flush()
    await db_session.commit()
    bad_id, good_id, since = bad.id, good.id, bad.capacity_locked_since

    # _stamped_workspaces has no ORDER BY: fail whichever commit comes first.
    real_commit = db_session.commit
    calls: list[int] = []

    async def commit_once_failing() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db gone")
        await real_commit()

    recipients: list[str] = []

    async def send(**kwargs) -> bool:
        recipients.append(kwargs["to_email"])
        return True

    redis = _Redis()
    with patch.object(db_session, "commit", commit_once_failing):
        counts = await capacity_purge.send_due_purge_notices(
            db_session, now=now, redis=redis, email=MagicMock(send_capacity_purge_notice=send)
        )

    assert (counts.sent, counts.failed) == (1, 1)
    rows = {
        w.id: w
        for w in (
            await db_session.execute(select(Workspace).where(Workspace.id.in_([bad_id, good_id])))
        )
        .scalars()
        .all()
    }
    for w in rows.values():
        await db_session.refresh(w)
    recorded = [w for w in rows.values() if w.capacity_purge_final_notice_at == now]
    unrecorded = [w for w in rows.values() if w.capacity_purge_final_notice_at is None]
    assert len(recorded) == 1 and len(unrecorded) == 1
    assert unrecorded[0].capacity_purge_first_notice_at is None
    assert capacity_purge.notice_key(unrecorded[0].id, since, 29) not in redis.keys
    assert capacity_purge.notice_key(recorded[0].id, since, 29) in redis.keys
    assert len(recipients) == 1


@pytest.mark.parametrize(("first_notice_days_ago", "purged"), [(3, False), (7, True)])
@pytest.mark.asyncio
async def test_purge_needs_a_first_notice_a_week_before(
    db_session, free_ws, purge_side_effects, only_workspaces, first_notice_days_ago, purged
) -> None:
    """Day 30, final notice the previous day, but the first notice only
    ``first_notice_days_ago`` days ago: deleted only once it is a week old."""
    ws, ctx, owner = free_ws
    only_workspaces.add(ws.id)
    await _over_cap(db_session, ws, ctx, owner)
    now = utcnow().replace(microsecond=0)
    ws.capacity_locked_since = now - timedelta(days=30)
    ws.capacity_purge_final_notice_at = now - timedelta(days=1)
    ws.capacity_purge_first_notice_at = now - timedelta(days=first_notice_days_ago)
    await db_session.flush()

    counts = await purge_expired_workspaces(db_session, now=now, email=_mail())

    assert (counts.purged, counts.awaiting_notice) == (int(purged), int(not purged))
    await db_session.refresh(ctx)
    assert (ctx.deleted_at is not None) is purged
    assert (len(await _live_memories(db_session, ws)) == 0) is purged


class _RecordingMail:
    """Every countdown / completion mail, with the day of the run that sent it."""

    def __init__(self) -> None:
        self.day = -1
        self.notices: list[tuple[int, int]] = []  # (day, days_left)
        self.completions: list[int] = []

    async def send_capacity_purge_notice(self, **kwargs) -> bool:
        self.notices.append((self.day, kwargs["days_left"]))
        return True

    async def send_capacity_purge_complete(self, **kwargs) -> bool:
        self.completions.append(self.day)
        return True


@pytest.mark.asyncio
async def test_capacity_purge_timeline_over_31_days(
    db_session, free_ws, purge_side_effects, only_workspaces
) -> None:
    """The whole policy, one daily run at a time: an over-cap workspace gets
    the countdown on calendar days 0/7/14/23/27/29, is purged on day 30 and not
    before, gets one completion mail, and nothing happens after."""
    from datetime import datetime, time

    from config.settings import get_settings

    ws, ctx, owner = free_ws
    only_workspaces.add(ws.id)
    await _over_cap(db_session, ws, ctx, owner)
    await db_session.commit()
    day0 = datetime.combine(utcnow().date() - timedelta(days=31), time(4, 0, 5))
    mail, redis, settings = _RecordingMail(), _Redis(), get_settings()

    with (
        patch.object(settings, "capacity_purge_notice_enabled", True),
        patch.object(settings, "capacity_purge_enabled", True),
        patch.object(capacity_purge, "mail_provider_configured", return_value=True),
        patch("db.redis.get_redis_client", return_value=redis),
        patch("services.email_service.get_email_service", return_value=mail),
    ):
        for day in range(32):
            mail.day = day
            await capacity_purge.run_capacity_purge_sweep(
                db_session, now=day0 + timedelta(days=day)
            )
            live = len(await _live_memories(db_session, ws))
            assert live == (MEMORY_LIMIT + 1 if day < 30 else 0), f"day {day}"
            await db_session.refresh(ws)
            if day == 0:
                assert ws.capacity_purge_first_notice_at == day0
            if day == 29:
                assert ws.capacity_purge_final_notice_at == day0 + timedelta(days=29)
            if day < 30:
                assert ws.capacity_locked_since == day0, f"day {day}"

    assert mail.notices == [(0, 30), (7, 23), (14, 16), (23, 7), (27, 3), (29, 1)]
    assert mail.completions == [30]
    assert ws.capacity_locked_since is None
    assert ws.capacity_purge_first_notice_at is None
    assert ws.capacity_purge_final_notice_at is None
    assert len(await _audit_rows(db_session, ws)) == 1
