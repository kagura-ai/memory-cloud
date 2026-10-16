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
bytes from ``workspace_storage_usage`` in the same transaction and dropping
the workspace's Redis counter after each commit so the next reservation
reseeds it (R5: Redis follows the database). It runs one context per
transaction, re-checking under lock that the context is still deleted, so a
workspace with many files never holds every row locked at once and a context
restored meanwhile is left alone. It is idempotent: the target set shrinks
to empty.

A context deleted more than ``FILE_RESTORE_WINDOW`` ago is past the window:
its files are stamped past the GC retention and the nightly
``sweep_soft_deleted_files`` removes their R2 objects on its next run. The
plan says so per context; that deletion is permanent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context
from models.file_objects import FileObject
from utils.datetime import utcnow
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
    past_restore_window: bool
    """Deleted more than ``FILE_RESTORE_WINDOW`` ago: the files cannot come
    back with ``restore_context`` and the next file GC removes their objects."""


@dataclass
class DeletedContextFilesBackfill:
    """What the backfill did, or would do on a dry run."""

    dry_run: bool
    contexts: list[DeletedContextFiles] = field(default_factory=list)
    # Filled on apply.
    files_soft_deleted: int = 0
    released_bytes: dict[UUID, int] = field(default_factory=dict)
    """Bytes the upload quota no longer holds, per workspace (uploaded + reserved)."""
    skipped: list[UUID] = field(default_factory=list)
    """Contexts restored between the plan and their turn; left alone."""

    @property
    def workspaces(self) -> set[UUID]:
        return {c.workspace_id for c in self.contexts}

    @property
    def uploaded_bytes(self) -> int:
        return sum(c.uploaded_bytes for c in self.contexts)

    @property
    def files(self) -> int:
        return sum(c.files for c in self.contexts)

    @property
    def past_restore_window(self) -> int:
        return sum(1 for c in self.contexts if c.past_restore_window)


async def find_deleted_context_files(
    db: AsyncSession, *, workspace_id: UUID | None = None
) -> list[DeletedContextFiles]:
    """Soft-deleted contexts that still own live ``file_objects`` rows, oldest deletion first.

    Args:
        db: Async session.
        workspace_id: Limit to one workspace (an operator canary, and what
            the tests use against the shared database).
    """
    from services.file_storage_service import FILE_RESTORE_WINDOW

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
    if workspace_id is not None:
        stmt = stmt.where(Context.workspace_id == workspace_id)
    window_start = utcnow() - FILE_RESTORE_WINDOW
    rows = (await db.execute(stmt)).all()
    return [
        DeletedContextFiles(
            context_id=row[0],
            workspace_id=row[1],
            deleted_at=row[2],
            files=int(row[3]),
            uploaded_bytes=int(row[4]),
            past_restore_window=row[2] <= window_start,
        )
        for row in rows
    ]


async def backfill_deleted_context_files(
    db: AsyncSession, *, dry_run: bool = True, workspace_id: UUID | None = None
) -> DeletedContextFilesBackfill:
    """Soft-delete the files left live in soft-deleted contexts.

    Args:
        db: Async session. A dry run only reads and ends its transaction. An
            apply commits once per context and drops that workspace's Redis
            counter right after the commit, so an interrupted run leaves every
            earlier context fully done and the rest untouched.
        dry_run: Count only.
        workspace_id: Limit to one workspace.

    Returns:
        The contexts found and, on apply, what was changed.
    """
    result = DeletedContextFilesBackfill(dry_run=dry_run)
    result.contexts = await find_deleted_context_files(db, workspace_id=workspace_id)
    if dry_run or not result.contexts:
        # Do not sit in a read transaction while the operator reads the plan
        # (idle_in_transaction_session_timeout).
        await db.rollback()
        return result

    from services import storage_quota_service
    from services.file_storage_service import FileStorageService

    files = FileStorageService(db)
    for context in result.contexts:
        # Re-check under lock: a context restored since the plan is live again
        # and its files must stay.
        still_deleted = (
            await db.execute(
                select(Context.id)
                .where(Context.id == context.context_id, Context.deleted_at.is_not(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if still_deleted is None:
            await db.rollback()
            result.skipped.append(context.context_id)
            logger.info(
                "deleted_context_files_backfill_skipped", context_id=str(context.context_id)
            )
            continue
        # The same rows and accounting as a #1960 context deletion, stamped
        # with the context's own deleted_at so restore_context brings them
        # back with the context (inside the file restore window).
        rows, _visible, released = await files._purge_live_files(
            [context.context_id], context.deleted_at
        )
        await db.commit()
        result.files_soft_deleted += len(rows)
        for ws_id, size in released.items():
            result.released_bytes[ws_id] = result.released_bytes.get(ws_id, 0) + size
            # Not a decrement: the key may be missing (no TTL-less negative
            # key) or already reseeded from the new definition (no double
            # release). The next reservation reseeds from _aggregate_from_db.
            await storage_quota_service.invalidate_storage_counter(ws_id)
        logger.info(
            "deleted_context_files_backfilled",
            context_id=str(context.context_id),
            workspace_id=str(context.workspace_id),
            files=len(rows),
            uploaded_bytes=context.uploaded_bytes,
            released_bytes=sum(released.values()),
            past_restore_window=context.past_restore_window,
        )
    return result
