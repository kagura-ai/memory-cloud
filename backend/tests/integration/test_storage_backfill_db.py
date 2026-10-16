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

from models.auth import WorkspaceMember, WorkspaceRole
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
    db_session.add_all([live, gone])
    await db_session.flush()
    # Deleted before #1960: the context is soft-deleted, its files are not.
    gone.deleted_at = utcnow() - timedelta(days=3)
    db_session.add_all(
        [
            _file(ws.id, live.id, owner.user_id, 100),
            _file(ws.id, None, owner.user_id, 30),
            _file(ws.id, gone.id, owner.user_id, 150),
            _file(ws.id, gone.id, owner.user_id, 70),
            _file(ws.id, gone.id, owner.user_id, 40, status="reserved"),
            _file(ws.id, gone.id, owner.user_id, 999, status="failed"),
        ]
    )
    # The counter still holds the pre-#1960 leftovers: 100 + 30 + 150 + 70.
    db_session.add(WorkspaceStorageUsage(workspace_id=ws.id, used_bytes=350, file_count=4))
    await db_session.commit()
    return {"ws": ws.id, "live": live.id, "gone": gone.id, "deleted_at": gone.deleted_at}


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
async def test_plan_lists_the_deleted_context_and_changes_nothing(db_session, seeded):
    with patch.object(storage_quota_service, "release_storage_bytes", AsyncMock()) as release:
        result = await backfill_deleted_context_files(db_session, dry_run=True)

    mine = [c for c in result.contexts if c.workspace_id == seeded["ws"]]
    assert [c.context_id for c in mine] == [seeded["gone"]]
    assert mine[0].files == 4  # uploaded x2, reserved, failed
    assert mine[0].uploaded_bytes == 220
    assert mine[0].deleted_at == seeded["deleted_at"]
    assert result.files_soft_deleted == 0 and result.released_bytes == {}
    release.assert_not_awaited()
    assert all(f.deleted_at is None for f in await _rows(db_session, seeded["gone"], seeded["ws"]))
    assert (await _usage(db_session, seeded["ws"])).used_bytes == 350


@pytest.mark.asyncio
async def test_apply_gives_the_files_their_context_deletion(db_session, seeded):
    with patch.object(storage_quota_service, "release_storage_bytes", AsyncMock()) as release:
        result = await backfill_deleted_context_files(db_session, dry_run=False)

    gone_rows = await _rows(db_session, seeded["gone"], seeded["ws"])
    assert len(gone_rows) == 4
    # Stamped with the context's own deleted_at: restore_context treats them
    # like a #1960 deletion.
    assert {f.deleted_at for f in gone_rows} == {seeded["deleted_at"]}
    by_size = {f.size_bytes: f for f in gone_rows}
    assert by_size[40].status == "failed"  # the reservation was cancelled
    assert by_size[999].status == "failed"

    # The live context's file and the workspace-scoped file are untouched.
    assert all(f.deleted_at is None for f in await _rows(db_session, seeded["live"], seeded["ws"]))
    assert all(f.deleted_at is None for f in await _rows(db_session, None, seeded["ws"]))

    usage = await _usage(db_session, seeded["ws"])
    assert usage.used_bytes == 130  # 350 - 150 - 70
    assert usage.file_count == 2

    # Redis follows the commit: uploaded (220) + the cancelled reservation (40).
    assert result.released_bytes.get(seeded["ws"]) == 260
    release.assert_any_await(seeded["ws"], 260)
    assert result.files_soft_deleted >= 4

    # Idempotent: the second run finds nothing of ours.
    with patch.object(storage_quota_service, "release_storage_bytes", AsyncMock()):
        again = await backfill_deleted_context_files(db_session, dry_run=False)
    assert not [c for c in again.contexts if c.workspace_id == seeded["ws"]]


@pytest.mark.asyncio
async def test_quota_reseed_no_longer_counts_the_leftovers(db_session, seeded):
    """Before the backfill the reseed source already excludes them (#1966 step 2)."""
    assert await storage_quota_service._aggregate_from_db(seeded["ws"], db_session) == 130
