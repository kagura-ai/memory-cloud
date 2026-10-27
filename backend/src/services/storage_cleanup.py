"""Durable, shared-key-safe object deletion (#2395)."""

from __future__ import annotations

import re
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.auth import Workspace
from models.file_objects import FileObject, StorageObjectDeletion
from storage.protocol import BlobInventoryProtocol, BlobStorageProtocol
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


async def lock_storage_key(db: AsyncSession, key: str) -> None:
    """Transaction-scoped, including under PgBouncer transaction pooling.

    Matches the database insert/update triggers. Call before writing bytes when
    the blob is written before its registry row (legacy attachment migration).
    Multi-key callers must acquire keys in sorted order.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 2395))"), {"key": key}
    )


async def key_is_registered(db: AsyncSession, key: str) -> bool:
    """Even soft-deleted/reserved rows own their object until the row is reaped."""
    return bool(await db.scalar(select(exists().where(FileObject.storage_key == key))))


async def purge_deleted_workspace_files(
    db: AsyncSession, *, workspace_limit: int = 10, file_limit: int = 500
) -> dict[str, int]:
    """Bounded repair for workspace soft deletions before #2395.

    Workspace lock precedes ordered file locks, like delete_workspace. Recheck
    the deletion under that lock: a revived workspace must keep its files.
    """
    from services import storage_quota_service
    from services.file_storage_service import FileStorageService

    counts = {"backfilled_files": 0, "quota_release_failed": 0}
    ids = list(
        (
            await db.scalars(
                select(Workspace.id)
                .where(
                    Workspace.deleted_at.isnot(None),
                    exists().where(
                        FileObject.workspace_id == Workspace.id, FileObject.deleted_at.is_(None)
                    ),
                )
                .order_by(Workspace.id)
                .limit(workspace_limit)
            )
        ).all()
    )
    for workspace_id in ids:
        workspace = await db.scalar(
            select(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.deleted_at.isnot(None),
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if workspace is None:
            await db.rollback()
            continue
        purged = await FileStorageService(db).purge_workspace_files(
            workspace_id, deleted_at=workspace.deleted_at, limit=file_limit
        )
        await db.commit()
        counts["backfilled_files"] += purged.rows
        for released_workspace, size in purged.released.items():
            try:
                await storage_quota_service.release_storage_bytes(
                    workspace_id=released_workspace, size_bytes=size
                )
            except Exception as exc:  # noqa: BLE001 - DB committed; quota reseeds separately
                counts["quota_release_failed"] += 1
                logger.warning(
                    "deleted_workspace_file_quota_release_failed", error_class=type(exc).__name__
                )
    return counts


async def drain_storage_deletions(
    db: AsyncSession, storage: BlobStorageProtocol, *, limit: int = 500
) -> dict[str, int]:
    """R2 first; retain a failed marker for the next tick. Never log a key."""
    counts = {"deleted": 0, "failed": 0, "registered": 0}
    keys = list(
        (
            await db.scalars(
                select(StorageObjectDeletion.storage_key)
                .where(StorageObjectDeletion.available_after <= func.timezone("UTC", func.now()))
                .order_by(StorageObjectDeletion.storage_key)
                .limit(limit)
            )
        ).all()
    )
    for key in keys:
        await lock_storage_key(db, key)
        job = await db.scalar(
            select(StorageObjectDeletion)
            .where(
                StorageObjectDeletion.storage_key == key,
                StorageObjectDeletion.available_after <= func.timezone("UTC", func.now()),
            )
            .with_for_update()
        )
        if job is None:
            await db.rollback()
            continue
        if await key_is_registered(db, key):
            counts["registered"] += 1
        else:
            try:
                await storage.delete_object(key)
            except Exception as exc:  # noqa: BLE001 - durable retry, no key/exception text
                counts["failed"] += 1
                logger.warning("storage_cleanup_failed", error_class=type(exc).__name__)
                await db.rollback()
                continue
            counts["deleted"] += 1
        await db.execute(
            delete(StorageObjectDeletion).where(StorageObjectDeletion.storage_key == key)
        )
        await db.commit()
    return counts


def _managed_object_key(key: str) -> bool:
    """Recognize this application's current and legacy key shapes only."""
    prefix, separator, suffix = key.partition("/")
    try:
        UUID(prefix)
    except ValueError:
        return False
    if not separator:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{2}/[0-9a-fA-F]{64}", suffix):
        return suffix[:2].lower() == suffix[3:5].lower()
    legacy, separator, filename = suffix.removeprefix("legacy/").partition("/")
    if not suffix.startswith("legacy/") or not separator or not filename:
        return False
    try:
        UUID(legacy)
    except ValueError:
        return False
    return True


async def sweep_unregistered_objects(
    db: AsyncSession,
    storage: BlobInventoryProtocol,
    *,
    dry_run: bool = True,
    minimum_age: timedelta = timedelta(days=2),
) -> dict[str, int]:
    """Inventory old managed objects; recheck under the writer lock in apply."""
    if minimum_age < timedelta(days=2):
        raise ValueError("minimum_age must be at least two days")
    counts = {"scanned": 0, "candidates": 0, "deleted": 0, "failed": 0, "kept": 0}
    # A configured long-lived PUT URL must have expired before inventory can
    # call an unregistered object an orphan.
    minimum_age = max(minimum_age, timedelta(seconds=get_settings().presign_put_ttl_seconds + 3600))
    threshold = utcnow() - minimum_age
    async for item in storage.iter_objects():
        counts["scanned"] += 1
        if not _managed_object_key(item["key"]):
            counts["kept"] += 1
            continue
        if item["last_modified"] > threshold:
            counts["kept"] += 1
            continue
        key = item["key"]
        await lock_storage_key(db, key)
        future_job = await db.scalar(
            select(
                exists().where(
                    StorageObjectDeletion.storage_key == key,
                    StorageObjectDeletion.available_after > func.timezone("UTC", func.now()),
                )
            )
        )
        if await key_is_registered(db, key) or future_job:
            counts["kept"] += 1
        elif dry_run:
            counts["candidates"] += 1
        else:
            meta = await storage.head_object(key)
            if meta is None or meta.get("last_modified", utcnow()) > threshold:
                counts["kept"] += 1
            else:
                counts["candidates"] += 1
                try:
                    await storage.delete_object(key)
                    counts["deleted"] += 1
                except Exception as exc:  # noqa: BLE001 - report counts only
                    counts["failed"] += 1
                    logger.warning("orphan_storage_cleanup_failed", error_class=type(exc).__name__)
        await db.rollback()
    return counts
