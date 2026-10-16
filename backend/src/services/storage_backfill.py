"""Backfill: files left live in soft-deleted contexts (#1966).

Before #1960 a context deletion left its files live: invisible to
``list_files`` and the storage page, but still counted by the upload quota
(``workspace_storage_usage`` and the Redis counter). The capacity lock and
the downgrade eligibility (#1961) count only files the owner can still
delete, so such a workspace could pass eligibility, downgrade to Free, and
then have every upload refused by the counter with no lock to explain it.

This one-shot backfill soft-deletes those files the way ``delete_context``
does since #1960 — stamped with the context's own ``deleted_at``, so
``restore_context`` treats them like any #1960 deletion — releasing their
bytes from ``workspace_storage_usage`` in the same transaction and from the
Redis counter after each commit (R5: Redis follows the database). It runs
one context per transaction so a workspace with many files never holds
every row locked at once, and it is idempotent: the target set shrinks to
empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context
from models.file_objects import FileObject
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class DeletedContextFiles:
    """One soft-deleted context that still has live file rows."""

    context_id: UUID
    workspace_id: UUID
    deleted_at: datetime
    files: int
    """Live rows of every status (uploaded, reserved, failed)."""
    uploaded_bytes: int
    """Committed bytes still counted by the upload quota."""


@dataclass
class DeletedContextFilesBackfill:
    """What the backfill did, or would do on a dry run."""

    dry_run: bool
    contexts: list[DeletedContextFiles] = field(default_factory=list)
    # Filled on apply: Redis bytes released per workspace (uploaded + reserved).
    released_bytes: dict[UUID, int] = field(default_factory=dict)
    files_soft_deleted: int = 0

    @property
    def workspaces(self) -> set[UUID]:
        return {c.workspace_id for c in self.contexts}

    @property
    def uploaded_bytes(self) -> int:
        return sum(c.uploaded_bytes for c in self.contexts)

    @property
    def files(self) -> int:
        return sum(c.files for c in self.contexts)


async def find_deleted_context_files(db: AsyncSession) -> list[DeletedContextFiles]:
    """Soft-deleted contexts that still own live ``file_objects`` rows, oldest deletion first."""
    uploaded_bytes = func.coalesce(
        func.sum(FileObject.size_bytes).filter(FileObject.status == "uploaded"), 0
    )
    stmt = (
        select(
            Context.id,
            Context.workspace_id,
            Context.deleted_at,
            func.count(FileObject.id),
            uploaded_bytes,
        )
        .join(FileObject, FileObject.context_id == Context.id)
        .where(Context.deleted_at.is_not(None), FileObject.deleted_at.is_(None))
        .group_by(Context.id, Context.workspace_id, Context.deleted_at)
        .order_by(Context.deleted_at, Context.id)
    )
    rows = (await db.execute(stmt)).all()
    return [
        DeletedContextFiles(
            context_id=row[0],
            workspace_id=row[1],
            deleted_at=row[2],
            files=int(row[3]),
            uploaded_bytes=int(row[4]),
        )
        for row in rows
    ]


async def backfill_deleted_context_files(
    db: AsyncSession, *, dry_run: bool = True
) -> DeletedContextFilesBackfill:
    """Soft-delete the files left live in soft-deleted contexts.

    Args:
        db: Async session. A dry run only reads. An apply commits once per
            context and releases that context's bytes from the Redis counter
            right after its commit, so an interrupted run leaves every
            earlier context fully done and the rest untouched.
        dry_run: Count only.

    Returns:
        The contexts found (and, on apply, the bytes released per workspace).
    """
    result = DeletedContextFilesBackfill(dry_run=dry_run)
    result.contexts = await find_deleted_context_files(db)
    if dry_run or not result.contexts:
        return result

    from services import storage_quota_service
    from services.file_storage_service import FileStorageService

    files = FileStorageService(db)
    for context in result.contexts:
        # The same rows and accounting as a #1960 context deletion, stamped
        # with the context's own deleted_at so restore_context brings them
        # back with the context (inside the file restore window).
        released = await files.purge_files_for_contexts(
            [context.context_id], deleted_at=context.deleted_at
        )
        await db.commit()
        result.files_soft_deleted += context.files
        for ws_id, size in released.items():
            result.released_bytes[ws_id] = result.released_bytes.get(ws_id, 0) + size
            await storage_quota_service.release_storage_bytes(ws_id, size)
        logger.info(
            "deleted_context_files_backfilled",
            context_id=str(context.context_id),
            workspace_id=str(context.workspace_id),
            files=context.files,
            uploaded_bytes=context.uploaded_bytes,
            released_bytes=sum(released.values()),
        )
    return result
