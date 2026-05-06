"""Orphan file sweeper (Issue #485 R3).

Reservation rows in ``file_objects`` that pass their ``expires_at + 1h``
grace without a matching ``upload-complete`` are stale: the client
either died mid-upload, the presigned PUT URL expired, or some other
transport failure left the row dangling. The sweeper:

1. Marks the row ``status='failed'`` so the partial-unique index
   (``WHERE deleted_at IS NULL AND status <> 'failed'``) frees up
   ``(workspace_id, sha256)`` for a redo.
2. Releases the Redis storage reservation so the workspace doesn't
   lose capacity.
3. Best-effort deletes any orphan R2 object the client may have
   PUT-ed inside the window. Swallows R2 errors — a failed delete
   leaves the binary as garbage in R2 and is reconciled by the
   bucket lifecycle rule (Phase 1.5 inventory job).

Cadence: 15 minutes. Architect A's design rationale — Free-tier 100MB
is the smallest cap and a single reserved orphan can otherwise block
new uploads up to ~25h on a nightly sweep. 15min beat keeps reserved-
orphan UX impact under one quarter-hour worst case.
"""

from __future__ import annotations

from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from db.base import get_db
from models.file_objects import FileObject
from services import storage_quota_service
from storage.factory import get_blob_storage
from utils.datetime import utcnow
from utils.exceptions import ExternalServiceError
from utils.logger import get_logger

logger = get_logger(__name__)


# Grace window past expires_at before a reserved row is considered orphan.
# Architect A's design specifies 1h to absorb cross-region clock skew and
# transient client retry windows.
_ORPHAN_GRACE_SECONDS = 3600


# Retention window for soft-deleted file_objects rows before the nightly
# GC removes the R2 binary and hard-deletes the row. Issue #552: 7d gives
# users a "trash undo" window before the bytes are unrecoverable.
_GC_RETENTION_SECONDS = 7 * 86400


async def sweep_orphan_files() -> dict[str, int]:
    """Reap reserved-but-not-uploaded ``file_objects`` rows past grace.

    Returns:
        ``{"swept": N, "released_bytes": N, "r2_deleted": N, "r2_failed": N}``
        — useful for the scheduler log and future ops dashboards.
    """
    counts = {"swept": 0, "released_bytes": 0, "r2_deleted": 0, "r2_failed": 0}

    # Filter at SQL so the partial index ``idx_file_objects_reserved_expires``
    # is used; otherwise every sweeper tick loads all in-flight rows.
    threshold = utcnow() - timedelta(seconds=_ORPHAN_GRACE_SECONDS)

    async for db in get_db():
        result = await db.execute(
            select(FileObject).where(
                FileObject.status == "reserved",
                FileObject.expires_at.isnot(None),
                FileObject.expires_at <= threshold,
                # Defensive: ``delete_file`` on a reserved row already
                # transitions ``status='failed'`` (loop 3 fix on PR #551),
                # so under normal flow no soft-deleted reserved row
                # reaches this query. Adding ``deleted_at IS NULL``
                # as a SQL invariant pins down the contract — protects
                # against future regressions where a path soft-deletes
                # a reserved row without changing status, which would
                # otherwise cause the sweeper to double-release Redis
                # quota (Copilot loop 4 finding).
                FileObject.deleted_at.is_(None),
            )
        )
        candidates = list(result.scalars().all())

        # Try to load the storage backend once; if R2 is not configured
        # (dev / test) we skip the binary cleanup but still mark rows
        # failed and release Redis quota.
        try:
            storage = get_blob_storage()
        except ExternalServiceError:
            # R2 not configured in dev/test — sweep the DB rows but skip
            # the binary cleanup. Same outcome as the legacy
            # ``RuntimeError`` branch this used to catch (the factory
            # now raises ``ExternalServiceError`` per the Copilot loop 3
            # fix on PR #551 so REST/MCP handlers map cleanly to 502).
            storage = None

        # Two-phase: mark all rows ``failed`` first, then commit, then
        # do the side-effects (Redis release + R2 delete). If the
        # commit fails, no Redis release happens and the next sweeper
        # tick re-evaluates the same rows fresh — no double-release.
        pending_releases: list[tuple] = []  # list[(workspace_id, size_bytes, storage_key|None)]
        for file in candidates:
            # SQL-side filter already ensured ``expires_at <= threshold``,
            # but a defensive Python re-check guards against unexpected
            # mutations within the same transaction.
            if file.expires_at is None or file.expires_at > threshold:
                continue

            file.status = "failed"
            counts["swept"] += 1
            counts["released_bytes"] += file.size_bytes
            pending_releases.append(
                (file.workspace_id, file.size_bytes, file.storage_key, str(file.id))
            )

        await db.commit()

        # Side effects (post-commit). A Redis or R2 failure at this
        # point leaves the row durably ``failed`` so the partial-unique
        # index frees the (workspace, sha256) slot — consistent with
        # the operator's mental model of "sweeper marked these dead".
        for workspace_id, size_bytes, storage_key, file_id in pending_releases:
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id,
                size_bytes=size_bytes,
            )
            if storage is not None and storage_key:
                try:
                    await storage.delete_object(storage_key)
                    counts["r2_deleted"] += 1
                except Exception as exc:  # noqa: BLE001 — best-effort
                    counts["r2_failed"] += 1
                    logger.warning(
                        "orphan_file_r2_delete_failed",
                        file_id=file_id,
                        storage_key=storage_key,
                        error=str(exc),
                    )

    if counts["swept"] > 0:
        logger.info("orphan_files_swept", **counts)
    return counts


async def sweep_soft_deleted_files() -> dict[str, int]:
    """Hard-delete ``file_objects`` rows soft-deleted past retention + their R2 binaries.

    Quota was already released at soft-delete time (R5: immediate
    release in ``FileStorageService.delete_file``). This sweep does NOT
    call ``release_storage_bytes`` again — that would double-decrement
    the workspace counter.

    Order: R2 delete first, then DB hard-delete, with a per-row commit.
    If R2 fails the row is left for the next sweep (R2 ``DELETE`` is
    S3-spec idempotent so retries are safe). If we hard-deleted the row
    before R2 succeeded, a transient R2 failure would orphan the binary
    forever — the inverse of the orphan sweeper's "DB-row-as-tracking"
    semantics, because here the binary leakage cost outweighs row
    longevity.

    Skips entirely when R2 is unconfigured: we cannot clean up the
    binary, so we must not hard-delete the row that points at it.

    Returns:
        ``{"swept": N, "r2_deleted": N, "r2_failed": N, "hard_deleted_no_r2": N}``
    """
    counts = {"swept": 0, "r2_deleted": 0, "r2_failed": 0, "hard_deleted_no_r2": 0}
    threshold = utcnow() - timedelta(seconds=_GC_RETENTION_SECONDS)

    try:
        storage = get_blob_storage()
    except ExternalServiceError:
        # R2 unconfigured (dev/test). Don't hard-delete rows whose
        # binaries we can't clean up first; they stay for the next
        # sweep when R2 is reachable.
        logger.info("soft_delete_gc_skipped_r2_unconfigured")
        return counts

    async for db in get_db():
        # Cap a single sweep at 500 rows so a bulk soft-delete (e.g. a
        # workspace purging thousands of files) doesn't load all ORM
        # objects into memory at once. Tail rolls to the next nightly
        # tick — the partial index ``idx_file_objects_soft_deleted_gc``
        # makes the next ``deleted_at <=`` filter cheap.
        result = await db.execute(
            select(FileObject)
            .where(
                FileObject.status == "uploaded",
                FileObject.deleted_at.isnot(None),
                FileObject.deleted_at <= threshold,
            )
            .limit(500)
        )
        candidates = list(result.scalars().all())

        for f in candidates:
            file_id = str(f.id)
            storage_key = f.storage_key

            if storage_key:
                try:
                    await storage.delete_object(storage_key)
                    counts["r2_deleted"] += 1
                except Exception as exc:  # noqa: BLE001 — best-effort
                    counts["r2_failed"] += 1
                    logger.warning(
                        "soft_delete_gc_r2_delete_failed",
                        file_id=file_id,
                        storage_key=storage_key,
                        error=str(exc),
                    )
                    continue
            else:
                # ``storage_key IS NULL`` on an ``uploaded`` row is an
                # invariant violation — the orphan sweeper should have
                # transitioned it to ``failed`` already. Hard-deletable
                # since there's no binary to leak.
                counts["hard_deleted_no_r2"] += 1

            await db.delete(f)
            await db.commit()
            counts["swept"] += 1

    if counts["swept"] > 0 or counts["r2_failed"] > 0:
        logger.info("soft_delete_gc_swept", **counts)
    return counts


def schedule_file_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the orphan file sweeper and the nightly soft-delete GC."""
    scheduler.add_job(
        sweep_orphan_files,
        trigger=IntervalTrigger(minutes=15),
        id="orphan_file_sweeper",
        name="Orphan File Sweeper (Issue #485)",
        replace_existing=True,
    )
    # 03:15 UTC offsets the GC from common 03:00 maintenance windows
    # (and from the orphan sweeper's 15-min cadence) so a slow GC pass
    # doesn't pile up against unrelated batch jobs.
    scheduler.add_job(
        sweep_soft_deleted_files,
        trigger=CronTrigger(hour=3, minute=15),
        id="soft_delete_file_gc",
        name="Soft-Delete File GC (Issue #552)",
        replace_existing=True,
    )
    logger.info(
        "scheduled_file_tasks",
        orphan_interval_minutes=15,
        gc_cron_utc="03:15",
    )
