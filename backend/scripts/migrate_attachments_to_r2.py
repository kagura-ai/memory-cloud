#!/usr/bin/env python3
"""One-shot migration: copy legacy ``attachments`` BYTEA blobs to R2.

Issue #485 Phase 1 (final commit): unify file storage onto the new
``file_objects`` + R2 surface. The legacy ``Attachment`` table (BYTEA
in PostgreSQL, 5MB cap) stays in place to keep the existing REST
``/api/v1/attachments/*`` route functional during the transition.

This script:

1. Iterates non-orphan ``Attachment`` rows in pages.
2. For each, joins to the parent ``Memory`` to resolve ``workspace_id``.
3. Calls ``FileStorageService.migrate_attachment`` — idempotent,
   matching by storage key shape ``{workspace_id}/legacy/{attachment_id}/{filename}``.
4. Logs a per-row result and accumulates totals.

Skips:
- Attachments whose parent memory has ``workspace_id IS NULL``
  (pre-#247 / pre-multi-tenant rows). Logged for ops follow-up.
- Memory soft-deleted (``deleted_at IS NOT NULL``) — those rows are
  scheduled for hard delete; their attachments will be GC'd alongside.

Usage:
    python backend/scripts/migrate_attachments_to_r2.py
    python backend/scripts/migrate_attachments_to_r2.py --dry-run
    python backend/scripts/migrate_attachments_to_r2.py --batch-size 100
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from models.memory import Attachment, Memory
from services.file_storage_service import FileStorageService
from utils.logger import get_logger

logger = get_logger(__name__)


async def _migrate_one(
    *,
    db: AsyncSession,
    attachment: Attachment,
    memory: Memory,
    dry_run: bool,
) -> str:
    """Migrate a single attachment. Returns ``"migrated" | "skipped" | "dry_run"``."""
    if memory.workspace_id is None:
        logger.warning(
            "attachment_skip_no_workspace",
            attachment_id=str(attachment.id),
            memory_id=str(memory.id),
        )
        return "skipped"
    if dry_run:
        logger.info(
            "attachment_migration_dry_run",
            attachment_id=str(attachment.id),
            memory_id=str(memory.id),
            workspace_id=str(memory.workspace_id),
            size_bytes=attachment.size_bytes,
        )
        return "dry_run"

    service = FileStorageService(db)
    await service.migrate_attachment(
        workspace_id=memory.workspace_id,
        attachment_id=attachment.id,
        filename=attachment.filename,
        content_type=attachment.content_type,
        size_bytes=attachment.size_bytes,
        data=attachment.data,
        created_by=f"migration:attachment:{attachment.id}",
    )
    return "migrated"


async def run(*, dry_run: bool, batch_size: int) -> dict[str, int]:
    """Walk all attachments in pages of ``batch_size``."""
    counts = {"migrated": 0, "skipped": 0, "dry_run": 0, "failed": 0}
    last_id = None

    async for db in get_db():
        while True:
            stmt = (
                select(Attachment, Memory)
                .join(Memory, Memory.id == Attachment.memory_id)
                .where(Memory.deleted_at.is_(None))
                .order_by(Attachment.id)
                .limit(batch_size)
            )
            if last_id is not None:
                stmt = stmt.where(Attachment.id > last_id)

            result = await db.execute(stmt)
            rows = list(result.all())
            if not rows:
                break

            for att, mem in rows:
                try:
                    outcome = await _migrate_one(
                        db=db,
                        attachment=att,
                        memory=mem,
                        dry_run=dry_run,
                    )
                    counts[outcome] += 1
                except Exception as exc:  # noqa: BLE001 — best-effort migration
                    counts["failed"] += 1
                    logger.error(
                        "attachment_migration_failed",
                        attachment_id=str(att.id),
                        error=str(exc),
                    )
                last_id = att.id
        break  # exit the get_db async-iterator loop after one session

    logger.info("attachment_migration_complete", **counts)
    return counts


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Don't upload to R2")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Attachments per page (default: 50)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    counts = asyncio.run(run(dry_run=args.dry_run, batch_size=args.batch_size))
    if counts["failed"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
