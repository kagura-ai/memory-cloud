"""The #1966 backfill against Postgres: files left live in soft-deleted contexts.

Seeds a workspace with a live context, a context deleted before #1960 (its
files still live) and a workspace-scoped file, then pins: the plan reads only;
the apply soft-deletes the deleted context's rows with the context's own
``deleted_at`` (so ``restore_context`` treats them as a #1960 deletion),
decrements ``workspace_storage_usage`` by the committed bytes, releases the
uploaded + reserved bytes from Redis after the commit, leaves the live context
and the workspace-scoped file alone, and finds nothing on a second run.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, WorkspaceMember, WorkspaceRole
from models.file_objects import FileObject, WorkspaceStorageUsage
from services import storage_quota_service
from services.storage_backfill import backfill_deleted_context_files
from utils.datetime import utcnow

from ._admin_helpers import make_context, make_user, make_workspace


def _file(ws_id: UUID, ctx_id: UUID | None, owner: str, size: int, *, status: str = "uploaded"):
    sha = uuid.uuid4().hex + uuid.uuid4().hex
    return FileObject(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        context_id=ctx_id,
        sha256=sha,
        size_bytes=size,
        content_type="application/octet-stream",
        filename="f.bin",
        storage_backend="r2",
        storage_key=f"{ws_id}/{sha[:2]}/{sha}",
        status=status,
        created_by=owner,
    )


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> dict:
    owner = make_user()
    db_session.add(owner)
    await db_session.flush()
    ws = make_workspace(owner_user_id=owner.user_id, plan_name="free")
    db_session.add(ws)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=owner.user_id, role=WorkspaceRole.OWNER)
    )
    live = make_context(workspace_id=ws.id, created_by=owner.user_id, is_private=True)
    gone = make_context(workspace_id=ws.id, created_by=owner.user_id, is_private=True)
    old = make_context(workspace_id=ws.id, created_by=owner.user_id, is_private=True)
    db_session.add_all([live, gone, old])
    await db_session.flush()
    # Deleted before #1960: the contexts are soft-deleted, their files are not.
    # ``gone`` is inside the file restore window, ``old`` (the production
    # shape: #1960 shipped weeks ago) is past it.
    gone.deleted_at = utcnow() - timedelta(days=3)
    old.deleted_at = utcnow() - timedelta(days=30)
    db_session.add_all(
        [
            _file(ws.id, live.id, owner.user_id, 100),
            _file(ws.id, None, owner.user_id, 30),
            _file(ws.id, gone.id, owner.user_id, 150),
            _file(ws.id, gone.id, owner.user_id, 70),
            _file(ws.id, gone.id, owner.user_id, 40, status="reserved"),
            _file(ws.id, gone.id, owner.user_id, 999, status="failed"),
            _file(ws.id, old.id, owner.user_id, 10),
        ]
    )
    # The counter still holds the pre-#1960 leftovers: 100 + 30 + 150 + 70 + 10.
    db_session.add(WorkspaceStorageUsage(workspace_id=ws.id, used_bytes=360, file_count=5))
    await db_session.commit()
    return {
        "ws": ws.id,
        "live": live.id,
        "gone": gone.id,
        "old": old.id,
        "deleted_at": gone.deleted_at,
        "old_deleted_at": old.deleted_at,
    }


async def _usage(db_session: AsyncSession, ws_id: UUID) -> WorkspaceStorageUsage:
    db_session.expire_all()
    return (
        await db_session.execute(
            select(WorkspaceStorageUsage).where(WorkspaceStorageUsage.workspace_id == ws_id)
        )
    ).scalar_one()


async def _rows(db_session: AsyncSession, ctx_id: UUID | None, ws_id: UUID) -> list[FileObject]:
    db_session.expire_all()
    stmt = select(FileObject).where(FileObject.workspace_id == ws_id)
    stmt = (
        stmt.where(FileObject.context_id == ctx_id)
        if ctx_id
        else stmt.where(FileObject.context_id.is_(None))
    )
    return list((await db_session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_plan_lists_the_deleted_contexts_and_changes_nothing(db_session, seeded):
    with patch.object(storage_quota_service, "invalidate_storage_counter", AsyncMock()) as drop:
        result = await backfill_deleted_context_files(
            db_session, dry_run=True, workspace_id=seeded["ws"]
        )
    # The plan ended its transaction (the operator reads it at a prompt).
    assert not db_session.in_transaction()

    # Oldest deletion first, scoped to the workspace.
    assert [c.context_id for c in result.contexts] == [seeded["old"], seeded["gone"]]
    old, gone = result.contexts
    assert gone.files == 4  # uploaded x2, reserved, failed
    assert gone.uploaded_bytes == 220
    assert gone.deleted_at == seeded["deleted_at"]
    assert gone.past_restore_window is False
    assert old.files == 1 and old.uploaded_bytes == 10
    assert old.past_restore_window is True  # the next file GC removes its objects
    assert result.past_restore_window == 1
    assert result.files_soft_deleted == 0 and result.released_bytes == {}
    drop.assert_not_awaited()
    assert all(f.deleted_at is None for f in await _rows(db_session, seeded["gone"], seeded["ws"]))
    assert (await _usage(db_session, seeded["ws"])).used_bytes == 360


@pytest.mark.asyncio
async def test_apply_skips_past_window_contexts_by_default(db_session, seeded):
    """The permanent deletions are a separate, explicit decision."""
    with patch.object(storage_quota_service, "invalidate_storage_counter", AsyncMock()) as drop:
        result = await backfill_deleted_context_files(
            db_session, dry_run=False, workspace_id=seeded["ws"]
        )
    assert result.skipped_past_window == [seeded["old"]]
    assert all(f.deleted_at is None for f in await _rows(db_session, seeded["old"], seeded["ws"]))
    assert {f.deleted_at for f in await _rows(db_session, seeded["gone"], seeded["ws"])} == {
        seeded["deleted_at"]
    }
    assert result.files_soft_deleted == 4
    assert drop.await_count == 1
    assert (await _usage(db_session, seeded["ws"])).used_bytes == 140  # 360 - 150 - 70


@pytest.mark.asyncio
async def test_apply_gives_the_files_their_context_deletion(db_session, seeded):
    with patch.object(storage_quota_service, "invalidate_storage_counter", AsyncMock()) as drop:
        result = await backfill_deleted_context_files(
            db_session, dry_run=False, workspace_id=seeded["ws"], include_past_window=True
        )

    gone_rows = await _rows(db_session, seeded["gone"], seeded["ws"])
    assert len(gone_rows) == 4
    # Stamped with the context's own deleted_at: restore_context treats them
    # like a #1960 deletion.
    assert {f.deleted_at for f in gone_rows} == {seeded["deleted_at"]}
    by_size = {f.size_bytes: f for f in gone_rows}
    assert by_size[40].status == "failed"  # the reservation was cancelled
    assert by_size[999].status == "failed"
    old_rows = await _rows(db_session, seeded["old"], seeded["ws"])
    assert {f.deleted_at for f in old_rows} == {seeded["old_deleted_at"]}  # past the window

    # The live context's file and the workspace-scoped file are untouched.
    assert all(f.deleted_at is None for f in await _rows(db_session, seeded["live"], seeded["ws"]))
    assert all(f.deleted_at is None for f in await _rows(db_session, None, seeded["ws"]))

    usage = await _usage(db_session, seeded["ws"])
    assert usage.used_bytes == 130  # 360 - 150 - 70 - 10
    assert usage.file_count == 2

    # The counter is dropped once per context commit, never decremented:
    # uploaded (220 + 10) + the cancelled reservation (40) no longer count.
    assert result.released_bytes.get(seeded["ws"]) == 270
    assert drop.await_count == 2
    drop.assert_any_await(seeded["ws"])
    assert result.files_soft_deleted == 5
    assert result.skipped == [] and result.skipped_past_window == []

    # Idempotent: the second run finds nothing.
    with patch.object(storage_quota_service, "invalidate_storage_counter", AsyncMock()):
        again = await backfill_deleted_context_files(
            db_session, dry_run=False, workspace_id=seeded["ws"], include_past_window=True
        )
    assert again.contexts == []


@pytest.mark.asyncio
async def test_a_context_restored_since_the_plan_is_skipped(db_session, seeded, monkeypatch):
    """The apply re-checks each context under lock: one restored between the
    plan and its turn keeps its files (they would otherwise vanish with a
    stale deleted_at stamp)."""
    from services import storage_backfill

    stale = await storage_backfill.find_deleted_context_files(db_session, workspace_id=seeded["ws"])
    await db_session.rollback()
    gone = (
        await db_session.execute(select(Context).where(Context.id == seeded["gone"]))
    ).scalar_one()
    gone.deleted_at = None  # restored meanwhile
    await db_session.commit()

    async def _stale(*_args, **_kwargs):
        return stale

    monkeypatch.setattr(storage_backfill, "find_deleted_context_files", _stale)
    with patch.object(storage_quota_service, "invalidate_storage_counter", AsyncMock()):
        result = await backfill_deleted_context_files(
            db_session, dry_run=False, workspace_id=seeded["ws"], include_past_window=True
        )

    assert result.skipped == [seeded["gone"]]
    assert all(f.deleted_at is None for f in await _rows(db_session, seeded["gone"], seeded["ws"]))
    assert {f.deleted_at for f in await _rows(db_session, seeded["old"], seeded["ws"])} == {
        seeded["old_deleted_at"]
    }
    assert result.files_soft_deleted == 1


@pytest.mark.asyncio
async def test_quota_reseed_no_longer_counts_the_leftovers(db_session, seeded):
    """Before the backfill the reseed source already excludes them (#1966 step 2)."""
    assert await storage_quota_service._aggregate_from_db(seeded["ws"], db_session) == 130
