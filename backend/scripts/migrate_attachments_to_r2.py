#!/usr/bin/env python3
"""One-shot migration: copy legacy ``attachments`` BYTEA blobs to R2.

Issue #485 Phase 1 (final commit): unify file storage onto the new
``file_objects`` + R2 surface. The legacy ``Attachment`` table (BYTEA
in PostgreSQL, 5MB cap) stays in place to keep the existing REST
``/api/v1/attachments/*`` route functional during the transition.

This script:

1. Iterates non-orphan ``Attachment`` rows in pages.
2. For each, joins to the parent ``Memory`` to resolve ``workspace_id``
   and ``context_id``, and to that memory's ``Context`` (outer join).
3. Calls ``FileStorageService.migrate_attachment`` — idempotent,
   matching by storage key shape ``{workspace_id}/legacy/{attachment_id}/{filename}``.
4. Logs a per-row result and accumulates totals.

Context binding (#2647): the ``file_objects`` row gets the parent memory's
``context_id``. A NULL ``context_id`` means workspace-shared (every workspace
member can list and download the file), so it is kept only for a memory
without a context; an attachment of a memory in a private or restricted
context stays behind that context's access list.

Skips:
- Attachments whose parent memory has ``workspace_id IS NULL``
  (pre-#247 / pre-multi-tenant rows). Logged for ops follow-up.
- Memory soft-deleted (``deleted_at IS NOT NULL``) — those rows are
  scheduled for hard delete; their attachments will be GC'd alongside.
- Memory whose context is soft-deleted or missing
  (``attachment_skip_deleted_context``): a file created there would be a
  live file in a deleted context, the shape the #1966 backfill cleans up.

Repair (``--repair-context``, #2647): rows migrated before the binding existed
carry ``context_id IS NULL``. With the flag the script runs the repair instead
of the migration: for ``file_objects`` rows with ``created_by LIKE
'migration:attachment:%' AND context_id IS NULL`` it reads the attachment id
from ``created_by`` and sets ``context_id`` from that attachment's memory.
Rows a user uploaded never match ``created_by``. Counts: ``bound`` (set, or
would be set under ``--dry-run``), ``shared`` (the memory has no context: left
workspace-shared), ``orphan`` (the attachment or memory is gone: untouched),
``context_deleted`` (bound to a soft-deleted context: hidden by the access
check, and the #1966 backfill finds it), ``unparsable`` (``created_by`` holds
no attachment id: untouched). Re-running the migration does not re-bind old
rows; only the repair does. Ops recipe: run ``--repair-context --dry-run``
first and read the counts, then ``--repair-context``; a second run binds
nothing. Read-only cross-check, before and after (0 after the repair)::

    SELECT count(*) FROM file_objects f
    JOIN attachments a ON f.created_by = 'migration:attachment:' || a.id
    JOIN memories m ON m.id = a.memory_id
    WHERE f.context_id IS NULL AND m.context_id IS NOT NULL;

Usage:
    python backend/scripts/migrate_attachments_to_r2.py
    python backend/scripts/migrate_attachments_to_r2.py --dry-run
    python backend/scripts/migrate_attachments_to_r2.py --batch-size 100
    python backend/scripts/migrate_attachments_to_r2.py --repair-context --dry-run
    python backend/scripts/migrate_attachments_to_r2.py --repair-context
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import aclosing
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from uuid import UUID

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from models.auth import Context
from models.file_objects import FileObject
from models.memory import Attachment, Memory
from services.file_storage_service import FileStorageService
from utils.logger import get_logger

logger = get_logger(__name__)

CREATED_BY_PREFIX = "migration:attachment:"


def _str(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


class _PendingAttachment(NamedTuple):
    """One page row as plain values, in the column order of ``_page_query``.

    Plain values, not ORM instances: the per-row rollback after a failure
    expires every instance in the session, and reading an expired attribute
    in async code raises instead of lazy-loading.
    """

    attachment_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    data: bytes
    memory_id: UUID
    workspace_id: UUID | None
    context_id: UUID | None
    context_row_id: UUID | None  # None with a context_id: the context row is gone
    context_deleted_at: datetime | None


def _page_query(batch_size: int, last_id: UUID | None) -> Select:
    """Attachments of live memories after ``last_id``, with their context state."""
    stmt = (
        select(
            Attachment.id,
            Attachment.filename,
            Attachment.content_type,
            Attachment.size_bytes,
            Attachment.data,
            Memory.id,
            Memory.workspace_id,
            Memory.context_id,
            Context.id,
            Context.deleted_at,
        )
        .join(Memory, Memory.id == Attachment.memory_id)
        .outerjoin(Context, Context.id == Memory.context_id)
        .where(Memory.deleted_at.is_(None))
        .order_by(Attachment.id)
        .limit(batch_size)
    )
    if last_id is not None:
        stmt = stmt.where(Attachment.id > last_id)
    return stmt


async def _migrate_one(
    *,
    db: AsyncSession,
    row: _PendingAttachment,
    dry_run: bool,
) -> str:
    """Migrate a single attachment. Returns ``"migrated" | "skipped" | "dry_run"``."""
    if row.workspace_id is None:
        logger.warning(
            "attachment_skip_no_workspace",
            attachment_id=str(row.attachment_id),
            memory_id=str(row.memory_id),
        )
        return "skipped"
    if row.context_id is not None and (
        row.context_row_id is None or row.context_deleted_at is not None
    ):
        logger.info(
            "attachment_skip_deleted_context",
            attachment_id=str(row.attachment_id),
            memory_id=str(row.memory_id),
            context_id=str(row.context_id),
        )
        return "skipped"
    if dry_run:
        logger.info(
            "attachment_migration_dry_run",
            attachment_id=str(row.attachment_id),
            memory_id=str(row.memory_id),
            workspace_id=str(row.workspace_id),
            context_id=_str(row.context_id),
            size_bytes=row.size_bytes,
        )
        return "dry_run"

    service = FileStorageService(db)
    await service.migrate_attachment(
        workspace_id=row.workspace_id,
        attachment_id=row.attachment_id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        data=row.data,
        created_by=f"{CREATED_BY_PREFIX}{row.attachment_id}",
        context_id=row.context_id,
    )
    return "migrated"


async def migrate_all(db: AsyncSession, *, dry_run: bool, batch_size: int) -> dict[str, int]:
    """Walk all attachments in pages of ``batch_size`` and migrate each one."""
    counts = {"migrated": 0, "skipped": 0, "dry_run": 0, "failed": 0}
    last_id: UUID | None = None

    while True:
        result = await db.execute(_page_query(batch_size, last_id))
        rows = [_PendingAttachment(*r) for r in result.all()]
        if not rows:
            break

        for row in rows:
            try:
                outcome = await _migrate_one(db=db, row=row, dry_run=dry_run)
                counts[outcome] += 1
            except Exception as exc:  # noqa: BLE001 — best-effort migration
                counts["failed"] += 1
                logger.error(
                    "attachment_migration_failed",
                    attachment_id=str(row.attachment_id),
                    error=str(exc),
                )
                # A failure mid-transaction (e.g. ``flush`` errored before
                # ``commit``) leaves the session in a failed transaction and
                # every later ``db.execute`` would raise, aborting the run.
                # Roll back so the next row starts fresh.
                try:
                    await db.rollback()
                except Exception as rollback_exc:  # noqa: BLE001 — best-effort
                    # Even rollback can fail on a closed session; the next
                    # ``db.execute`` will surface the underlying state.
                    logger.warning("attachment_migration_rollback_failed", error=str(rollback_exc))
            last_id = row.attachment_id

    logger.info("attachment_migration_complete", **counts)
    return counts


async def repair_context_bindings(
    db: AsyncSession, *, dry_run: bool, batch_size: int
) -> dict[str, int]:
    """Bind migrated rows left workspace-shared to their memory's context (#2647).

    Pages ``file_objects`` rows with ``created_by LIKE 'migration:attachment:%'``
    and ``context_id IS NULL`` by id, with no status or ``deleted_at`` filter
    (the binding is access metadata; ``restore_context`` relies on it for
    soft-deleted rows too). Binds a page's rows in file id order, the row-lock
    order of the purges, so it cannot deadlock with them. Commits per page;
    ``dry_run`` writes nothing and returns the same counts (``bound`` = would
    bind).
    """
    counts = {"bound": 0, "shared": 0, "orphan": 0, "context_deleted": 0, "unparsable": 0}
    last_id: UUID | None = None

    while True:
        stmt = (
            select(FileObject.id, FileObject.created_by)
            .where(
                FileObject.created_by.like(f"{CREATED_BY_PREFIX}%"),
                FileObject.context_id.is_(None),
            )
            .order_by(FileObject.id)
            .limit(batch_size)
        )
        if last_id is not None:
            stmt = stmt.where(FileObject.id > last_id)
        page = (await db.execute(stmt)).all()
        if not page:
            break
        last_id = page[-1][0]

        files_by_attachment: dict[UUID, list[UUID]] = {}
        for file_id, created_by in page:
            try:
                attachment_id = UUID((created_by or "").removeprefix(CREATED_BY_PREFIX))
            except ValueError:
                counts["unparsable"] += 1
                logger.warning(
                    "attachment_repair_unparsable", file_id=str(file_id), created_by=created_by
                )
                continue
            files_by_attachment.setdefault(attachment_id, []).append(file_id)

        # One query per page; never select ``Attachment.data`` (BYTEA).
        parents: dict[UUID, tuple[UUID | None, UUID | None, datetime | None]] = {}
        if files_by_attachment:
            result = await db.execute(
                select(Attachment.id, Memory.context_id, Context.id, Context.deleted_at)
                .join(Memory, Memory.id == Attachment.memory_id)
                .outerjoin(Context, Context.id == Memory.context_id)
                .where(Attachment.id.in_(list(files_by_attachment)))
            )
            parents = {r[0]: (r[1], r[2], r[3]) for r in result.all()}

        to_bind: list[tuple[UUID, UUID, UUID, bool]] = []
        for attachment_id, files in files_by_attachment.items():
            parent = parents.get(attachment_id)
            if parent is None or (parent[0] is not None and parent[1] is None):
                # The attachment or its memory is gone (or the memory points at
                # a context row that no longer exists): nothing to bind to.
                counts["orphan"] += len(files)
                logger.info(
                    "attachment_repair_orphan",
                    attachment_id=str(attachment_id),
                    file_ids=[str(f) for f in files],
                )
                continue
            context_id, _, context_deleted_at = parent
            if context_id is None:
                counts["shared"] += len(files)  # a context-less memory: workspace-wide
                continue
            for file_id in files:
                to_bind.append((file_id, attachment_id, context_id, context_deleted_at is not None))

        # File id order: row locks in id order like every multi-row file_objects
        # writer (``_purge_live_rows``); each row's storage-key lock (UPDATE
        # trigger) follows its row lock, as everywhere else.
        for file_id, attachment_id, context_id, context_deleted in sorted(to_bind):
            if not dry_run:
                updated = await db.execute(
                    update(FileObject)
                    .where(FileObject.id == file_id, FileObject.context_id.is_(None))
                    .values(context_id=context_id)
                    .execution_options(synchronize_session=False)
                )
                if not updated.rowcount:
                    continue  # bound by someone else meanwhile
            counts["bound"] += 1
            if context_deleted:
                counts["context_deleted"] += 1
                logger.info(
                    "attachment_repair_context_deleted",
                    file_id=str(file_id),
                    attachment_id=str(attachment_id),
                    context_id=str(context_id),
                )

        if dry_run:
            await db.rollback()
        else:
            await db.commit()

    logger.info("attachment_context_repair_complete", dry_run=dry_run, **counts)
    return counts


async def run(*, dry_run: bool, batch_size: int, repair_context: bool = False) -> dict[str, int]:
    """Open one session and run the migration, or the repair with ``repair_context``."""
    async with aclosing(get_db()) as sessions:
        async for db in sessions:
            if repair_context:
                return await repair_context_bindings(db, dry_run=dry_run, batch_size=batch_size)
            return await migrate_all(db, dry_run=dry_run, batch_size=batch_size)
    raise RuntimeError("get_db() yielded no session")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Write nothing; log and count what would happen"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Rows per page (default: 50)",
    )
    parser.add_argument(
        "--repair-context",
        action="store_true",
        help=(
            "Instead of migrating, bind already-migrated rows with a NULL context to "
            "their memory's context (#2647)"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    counts = asyncio.run(
        run(
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            repair_context=args.repair_context,
        )
    )
    if counts.get("failed", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
