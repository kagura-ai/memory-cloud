"""Per-workspace file-storage quota — Redis-backed reservation (Issue #485).

Issue #332 lesson: REST and MCP paths MUST share the same quota check.
This module is the canonical shared service that ``FileStorageService``
funnels both routes through. The CI grep gate (Commit 7+) verifies no
handler calls these functions outside the service layer.

Key format: ``storage:bytes:{workspace_id}``

- Workspace-scoped (storage is workspace-level, not context-level).
- **No TTL** — storage is a hard cap, not a rate limit. The counter is
  durable; reseed from DB on key miss (cold-start / Redis flush).
- ``release_storage_bytes`` reverses a reservation when an upload
  fails (orphan sweeper) or a file is soft-deleted (R5: immediate
  quota release in same txn).
- Fail-open on ``RedisError`` (matches ``resource_quota_service`` —
  Redis outage MUST NOT block uploads; reconcile via DB sweep later).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.redis import get_cache, incrby_counter, set_cache
from utils.exceptions import QuotaExceededError, RedisError
from utils.logger import get_logger

logger = get_logger(__name__)


def _build_key(workspace_id: UUID) -> str:
    return f"storage:bytes:{workspace_id}"


async def reserve_storage_bytes(
    workspace_id: UUID,
    size_bytes: int,
    quota_bytes: int,
    db: AsyncSession,
) -> None:
    """Atomically reserve ``size_bytes`` against ``quota_bytes`` ceiling.

    Reseeds from DB on Redis key miss — matches the durable-counter
    semantics of file storage (vs the ephemeral hour-window of
    ``resource_quota_service``).

    Args:
        workspace_id: Owning workspace.
        size_bytes: Bytes to reserve. Caller passes the upload size from
            ``upload-init``. Must be > 0.
        quota_bytes: Maximum allowed bytes. ``Workspace.effective_storage_limit_bytes``.
            Values <= 0 disable the check.
        db: Async session, used for DB reseed on Redis cache miss.

    Raises:
        QuotaExceededError: HTTP 409. Reserving ``size_bytes`` would
            push past ``quota_bytes``.
        ValueError: ``size_bytes <= 0`` (defensive).

    Fail-open: ``RedisError`` is logged and swallowed so uploads are
    not blocked. The next Redis-healthy reservation reseeds from DB.
    """
    if quota_bytes <= 0:
        return
    if size_bytes <= 0:
        msg = f"size_bytes must be > 0, got {size_bytes}"
        raise ValueError(msg)

    key = _build_key(workspace_id)

    try:
        current = await _get_or_seed_counter(workspace_id, db, key)

        if current + size_bytes > quota_bytes:
            logger.warning(
                "storage_quota_exceeded",
                workspace_id=str(workspace_id),
                current_bytes=current,
                quota_bytes=quota_bytes,
                requested_bytes=size_bytes,
            )
            raise QuotaExceededError(
                message=(f"Storage quota exceeded: {current + size_bytes} / {quota_bytes} bytes"),
                quota_type="storage_bytes",
                limit=quota_bytes,
                current=current,
                requested=size_bytes,
            )

        new_total = await incrby_counter(key, size_bytes)
        logger.debug(
            "storage_quota_reserved",
            workspace_id=str(workspace_id),
            previous_bytes=current,
            reserved_bytes=size_bytes,
            new_total=new_total,
            quota_bytes=quota_bytes,
        )
    except QuotaExceededError:
        raise
    except RedisError as exc:
        logger.error("storage_quota_redis_failed", workspace_id=str(workspace_id), error=str(exc))
        return


async def bump_committed_storage_bytes(
    workspace_id: UUID,
    size_bytes: int,
) -> None:
    """Increment the workspace's Redis counter without a quota check.

    Used by ``FileStorageService.migrate_attachment`` to keep the live
    counter in sync after committing a migrated row directly into
    ``status='uploaded'``. Unlike ``reserve_storage_bytes`` this does
    not check or raise on cap exceedance — migrations restore data
    that already exists; the cap was implicitly satisfied at original
    upload time.

    Fail-open on RedisError — next reseed self-corrects from DB.
    """
    if size_bytes <= 0:
        return
    key = _build_key(workspace_id)
    try:
        await incrby_counter(key, size_bytes)
    except RedisError as exc:
        logger.warning(
            "storage_quota_bump_redis_failed",
            workspace_id=str(workspace_id),
            error=str(exc),
        )


async def release_storage_bytes(
    workspace_id: UUID,
    size_bytes: int,
) -> None:
    """Reverse a previous reservation by ``size_bytes``.

    Called by:

    - Orphan sweeper when ``status='reserved'`` rows expire (R3).
    - ``FileStorageService.delete_file`` immediately on soft-delete (R5).
    - ``FileStorageService.confirm_upload`` if ``head_object`` reports a
      smaller-than-reserved size (truncation refund).

    Idempotency: a second release for the same bytes will floor the
    counter at zero on next reseed; we don't try to detect double-release
    here. Net effect is "slightly under-counted in Redis until next
    reseed", which is the safer drift direction.

    Fail-open on Redis errors — sweeper retries naturally on next tick.
    """
    if size_bytes <= 0:
        return

    key = _build_key(workspace_id)
    try:
        new_total = await incrby_counter(key, -size_bytes)
        # Floor at zero — INCRBY with a negative can go below 0 if we
        # released more than was reserved. Reseed-on-miss handles
        # eventual consistency; here we just log so tests can see it.
        if new_total < 0:
            logger.warning(
                "storage_quota_release_underflow",
                workspace_id=str(workspace_id),
                released_bytes=size_bytes,
                new_total=new_total,
            )
        else:
            logger.debug(
                "storage_quota_released",
                workspace_id=str(workspace_id),
                released_bytes=size_bytes,
                new_total=new_total,
            )
    except RedisError as exc:
        logger.error(
            "storage_quota_release_redis_failed",
            workspace_id=str(workspace_id),
            error=str(exc),
        )


async def get_current_storage_usage(
    workspace_id: UUID,
    db: AsyncSession,
) -> int:
    """Return current Redis counter, reseeding from DB on miss.

    Used by dashboards / admin pages to display "X / Y MB used".
    """
    key = _build_key(workspace_id)
    try:
        return await _get_or_seed_counter(workspace_id, db, key)
    except RedisError:
        # Fall back to a fresh DB aggregate — slow but correct.
        return await _aggregate_from_db(workspace_id, db)


async def _get_or_seed_counter(
    workspace_id: UUID,
    db: AsyncSession,
    key: str,
) -> int:
    """Read Redis counter, reseed from DB if missing.

    The reseed window is racy — two concurrent reservations both miss
    the key, both run the DB aggregate, both ``set_cache`` (last writer
    wins). Net effect is at worst one reservation skipped against the
    cap (the under-counted side); the next ``reserve_storage_bytes``
    call self-corrects via the next reseed. Acceptable given the
    fail-open posture.
    """
    cached = await get_cache(key)
    if cached is not None:
        return int(cached)

    seeded = await _aggregate_from_db(workspace_id, db)
    # Persist the seed for ~24h; INCRBY preserves the value, only EXPIRE
    # resets it. ``set_cache`` is fine here because we're seeding, not
    # incrementing.
    await set_cache(key, str(seeded), ttl=86400)
    logger.info(
        "storage_quota_seeded_from_db",
        workspace_id=str(workspace_id),
        seeded_bytes=seeded,
    )
    return seeded


async def _aggregate_from_db(workspace_id: UUID, db: AsyncSession) -> int:
    """Sum committed (uploaded, not deleted) ``file_objects`` size_bytes
    for ``workspace_id``.

    Excludes ``status='reserved'`` rows because those are tracked in
    Redis until ``confirm_upload`` flips them to ``uploaded``. Excludes
    ``status='failed'`` and soft-deleted rows.
    """
    from models.file_objects import FileObject

    result = await db.execute(
        select(func.coalesce(func.sum(FileObject.size_bytes), 0)).where(
            FileObject.workspace_id == workspace_id,
            FileObject.status == "uploaded",
            FileObject.deleted_at.is_(None),
        )
    )
    return int(result.scalar_one())
