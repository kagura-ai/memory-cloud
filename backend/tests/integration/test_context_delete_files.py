"""#1960: deleting a context takes its files with it, and a restore brings them back.

Real PostgreSQL. The capacity lock (#1941) counts file bytes, so until this
change an over-storage owner could only lift the lock file by file; the
context's files survived ``delete_context`` (restorable, #1804) and sat in the
storage counter. Now ``delete_context`` soft-deletes them with the context's
own timestamp and frees their bytes, and ``restore_deleted_context`` revives
the ones still inside the 7-day GC window whose content was not uploaded
again in the meantime.

The fixture hands back ids, not ORM instances: the services commit, which
expires the session's objects, and touching an expired attribute from a test
is a sync lazy load the async session refuses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import (
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Context,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from models.file_objects import FileObject, WorkspaceStorageUsage
from services.capacity_lock import capacity_lock_state
from services.context_restore import restore_deleted_context
from services.context_service import ContextService
from services.file_storage_service import FILE_SOFT_DELETE_RETENTION, FileStorageService

from ._admin_helpers import make_context, make_user, make_workspace

STORAGE_LIMIT = 200


@pytest.fixture(autouse=True)
def _small_limits():
    with (
        patch.object(Workspace, "effective_memory_limit", property(lambda self: 10**6)),
        patch.object(
            Workspace, "effective_storage_limit_bytes", property(lambda self: STORAGE_LIMIT)
        ),
    ):
        yield


@pytest.fixture
def side_effects():
    """Qdrant and Redis are side effects after the commit; this file pins the rows."""
    with (
        patch("db.qdrant.delete_context_points_everywhere", AsyncMock(return_value={})),
        patch("services.storage_quota_service.release_storage_bytes", AsyncMock()) as release,
        patch("services.storage_quota_service.bump_committed_storage_bytes", AsyncMock()) as bump,
    ):
        yield release, bump


@dataclass(frozen=True)
class Seeded:
    workspace_id: UUID
    keep_id: UUID
    doomed_id: UUID
    owner: str
    doomed_sha: str  # content hash of the doomed context's 150-byte upload


def _file(
    ws_id: UUID,
    ctx_id: UUID | None,
    owner: str,
    size: int,
    *,
    status: str = "uploaded",
    sha: str | None = None,
):
    sha = sha or uuid.uuid4().hex + uuid.uuid4().hex
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
async def seeded(db_session: AsyncSession) -> Seeded:
    """A Free, billing-managed workspace over its storage limit through one context."""
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
    keep = make_context(workspace_id=ws.id, created_by=owner.user_id, is_private=True)
    doomed = make_context(workspace_id=ws.id, created_by=owner.user_id, is_private=True)
    db_session.add_all([keep, doomed])
    await db_session.flush()
    doomed_upload = _file(ws.id, doomed.id, owner.user_id, 150)
    db_session.add_all(
        [
            _file(ws.id, keep.id, owner.user_id, 100),
            doomed_upload,
            _file(ws.id, doomed.id, owner.user_id, 40, status="reserved"),
        ]
    )
    db_session.add(WorkspaceStorageUsage(workspace_id=ws.id, used_bytes=250, file_count=2))
    await db_session.commit()
    return Seeded(ws.id, keep.id, doomed.id, owner.user_id, doomed_upload.sha256)


async def _workspace(db_session: AsyncSession, ws_id: UUID) -> Workspace:
    return (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()


async def _context(db_session: AsyncSession, ctx_id: UUID) -> Context:
    return (await db_session.execute(select(Context).where(Context.id == ctx_id))).scalar_one()


async def _usage(db_session: AsyncSession, ws_id: UUID) -> WorkspaceStorageUsage:
    return (
        await db_session.execute(
            select(WorkspaceStorageUsage).where(WorkspaceStorageUsage.workspace_id == ws_id)
        )
    ).scalar_one()


async def _files(db_session: AsyncSession, ctx_id: UUID, *, live_only: bool = False):
    stmt = select(FileObject).where(FileObject.context_id == ctx_id)
    if live_only:
        stmt = stmt.where(FileObject.deleted_at.is_(None))
    return (await db_session.execute(stmt)).scalars().all()


async def _restore(db_session: AsyncSession, ctx_id: UUID, *, dry_run: bool = False):
    return await restore_deleted_context(db_session, ctx_id, dry_run=dry_run, actor_id="t")


@pytest.mark.asyncio
async def test_deleting_a_context_frees_its_files_and_lifts_the_lock(
    db_session, seeded: Seeded, side_effects
) -> None:
    release, _bump = side_effects
    ws = await _workspace(db_session, seeded.workspace_id)
    lock = await capacity_lock_state(db_session, ws)
    assert lock is not None and lock.over_bytes == 50

    await ContextService(db_session).delete_context(seeded.owner, seeded.doomed_id)

    doomed = await _context(db_session, seeded.doomed_id)
    rows = await _files(db_session, seeded.doomed_id)
    assert len(rows) == 2 and all(f.deleted_at == doomed.deleted_at for f in rows)
    assert {f.status for f in rows} == {"uploaded", "failed"}  # the reservation was cancelled
    usage = await _usage(db_session, seeded.workspace_id)
    assert (usage.used_bytes, usage.file_count) == (100, 1)
    ws = await _workspace(db_session, seeded.workspace_id)
    assert await capacity_lock_state(db_session, ws) is None
    # The Redis counter follows after the commit: uploaded bytes + the cancelled reservation.
    release.assert_awaited_once_with(workspace_id=seeded.workspace_id, size_bytes=190)


@pytest.mark.asyncio
async def test_a_rolled_back_delete_releases_nothing_later(
    db_session, seeded: Seeded, side_effects
) -> None:
    """The pending Redis release belongs to the delete that committed, not to an
    earlier one on the same service that was rolled back."""
    release, _bump = side_effects
    service = ContextService(db_session)
    await service.delete_context(seeded.owner, seeded.keep_id, _commit=False)
    await db_session.rollback()
    assert (await _usage(db_session, seeded.workspace_id)).used_bytes == 250

    await service.delete_context(seeded.owner, seeded.doomed_id)

    release.assert_awaited_once_with(workspace_id=seeded.workspace_id, size_bytes=190)
    assert (await _usage(db_session, seeded.workspace_id)).used_bytes == 100


@pytest.mark.asyncio
async def test_merging_away_the_source_frees_its_files(
    db_session, seeded: Seeded, side_effects
) -> None:
    release, _bump = side_effects
    with patch("services.context_service.ContextService._remove_context_points", AsyncMock()):
        await ContextService(db_session).merge_contexts(
            seeded.owner, seeded.doomed_id, seeded.keep_id, delete_source=True
        )

    assert (await _context(db_session, seeded.doomed_id)).deleted_at is not None
    assert await _files(db_session, seeded.doomed_id, live_only=True) == []
    assert (await _usage(db_session, seeded.workspace_id)).used_bytes == 100
    release.assert_awaited_once_with(workspace_id=seeded.workspace_id, size_bytes=190)


@pytest.mark.asyncio
async def test_restore_brings_the_files_back_with_their_bytes(
    db_session, seeded: Seeded, side_effects
) -> None:
    _release, bump = side_effects
    await ContextService(db_session).delete_context(seeded.owner, seeded.doomed_id)

    dry = await _restore(db_session, seeded.doomed_id, dry_run=True)
    assert (dry.files_restored, dry.file_bytes_restored) == (1, 150)
    # 100 live + 150 back = 250 > the 200 limit: the admin is told.
    assert any("capacity-locked" in w for w in dry.warnings)

    result = await _restore(db_session, seeded.doomed_id)
    assert (result.files_restored, result.file_bytes_restored) == (1, 150)
    live = await _files(db_session, seeded.doomed_id, live_only=True)
    assert [f.size_bytes for f in live] == [150]  # the cancelled reservation stays gone
    usage = await _usage(db_session, seeded.workspace_id)
    assert (usage.used_bytes, usage.file_count) == (250, 2)
    bump.assert_awaited_once_with(workspace_id=seeded.workspace_id, size_bytes=150)


@pytest.mark.asyncio
async def test_a_file_uploaded_again_since_the_deletion_blocks_its_restore(
    db_session, seeded: Seeded, side_effects
) -> None:
    """``uq_file_objects_workspace_sha256_active`` allows one live row per content
    hash: the re-upload stays, the old row stays deleted, the restore says so."""
    _release, bump = side_effects
    await ContextService(db_session).delete_context(seeded.owner, seeded.doomed_id)
    db_session.add(
        _file(seeded.workspace_id, seeded.keep_id, seeded.owner, 150, sha=seeded.doomed_sha)
    )
    await db_session.execute(
        update(WorkspaceStorageUsage)
        .where(WorkspaceStorageUsage.workspace_id == seeded.workspace_id)
        .values(used_bytes=250, file_count=2)
    )
    await db_session.commit()

    dry = await _restore(db_session, seeded.doomed_id, dry_run=True)
    assert (dry.files_restored, dry.file_bytes_restored) == (0, 0)
    assert any("uploaded again" in w for w in dry.warnings)

    result = await _restore(db_session, seeded.doomed_id)
    assert (result.files_restored, result.file_bytes_restored) == (0, 0)
    assert (await _context(db_session, seeded.doomed_id)).deleted_at is None  # the context is back
    assert await _files(db_session, seeded.doomed_id, live_only=True) == []
    assert (await _usage(db_session, seeded.workspace_id)).used_bytes == 250
    bump.assert_not_awaited()


@pytest.mark.asyncio
async def test_files_past_the_retention_window_are_not_restored(
    db_session, seeded: Seeded, side_effects
) -> None:
    """The GC removes the R2 object before the row, so a row restored while the
    sweep is on it would come back without its bytes: past the window the restore
    leaves every file deleted."""
    _release, bump = side_effects
    await ContextService(db_session).delete_context(seeded.owner, seeded.doomed_id)
    doomed = await _context(db_session, seeded.doomed_id)
    assert doomed.deleted_at is not None
    long_ago = doomed.deleted_at - FILE_SOFT_DELETE_RETENTION - timedelta(hours=1)
    await db_session.execute(
        update(FileObject)
        .where(FileObject.context_id == seeded.doomed_id)
        .values(deleted_at=long_ago)
    )
    await db_session.execute(
        update(Context).where(Context.id == seeded.doomed_id).values(deleted_at=long_ago)
    )
    await db_session.commit()

    result = await _restore(db_session, seeded.doomed_id)
    assert (result.files_restored, result.file_bytes_restored) == (0, 0)
    assert any("retention window" in w for w in result.warnings)
    assert await _files(db_session, seeded.doomed_id, live_only=True) == []
    assert (await _usage(db_session, seeded.workspace_id)).used_bytes == 100
    bump.assert_not_awaited()


@pytest.mark.asyncio
async def test_purging_a_live_contexts_files_keeps_the_context(
    db_session, seeded: Seeded, side_effects
) -> None:
    """The service path ``DELETE /contexts/{id}/files`` takes (#1960): every live
    file of the context goes, the quota follows, the context stays."""
    files, released = await FileStorageService(db_session).purge_context_files(seeded.doomed_id)
    await db_session.commit()
    # Two live rows went (the upload and the cancelled reservation); both release Redis bytes.
    assert (files, released) == (2, 190)
    usage = await _usage(db_session, seeded.workspace_id)
    assert (usage.used_bytes, usage.file_count) == (100, 1)
    assert (await _context(db_session, seeded.doomed_id)).deleted_at is None
    assert all(f.deleted_at is not None for f in await _files(db_session, seeded.doomed_id))
