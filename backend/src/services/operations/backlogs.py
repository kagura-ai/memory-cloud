"""Work queue backlogs for the operations status route (Issue #2768).

One registry, :data:`BUILTIN_BACKLOGS`: each entry names a queue, the job
ids that drain it (``()`` = people drain it), a reader and an ``enabled``
predicate (a disabled entry is left out of the response). A reader returns
``(count, timestamp of the oldest item)``; naive timestamps are UTC (most
of these columns are naive) and :func:`read_backlogs` normalises them.

Readers return aggregates only: a count and one timestamp, never an id, a
name, an address or any content. Each is a ``count`` / ``min`` over the
predicate the draining job itself uses, on an index where one exists.

A module that owns a queue adds one ``BacklogSpec(...)`` line to the tuple;
there is no runtime registration, so nothing depends on import order.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, union_all

from services.file_scan_service import (
    FILE_QUARANTINE_EXPIRY_JOB_ID,
    FILE_SCAN_SWEEPER_JOB_ID,
    files_awaiting_scan_backlog,
    files_quarantined_backlog,
    files_scan_failed_backlog,
    files_unscanned_backlog,
)
from tasks.job_runs import sanitize_error_class

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from config.settings import Settings

BacklogReader = Callable[["AsyncSession", datetime], Awaitable[tuple[int, datetime | None]]]

BACKLOG_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# The error class of an entry the request's time budget did not reach.
DEADLINE_EXCEEDED = "DeadlineExceeded"


def _always(settings: Settings) -> bool:
    return True


@dataclass(frozen=True)
class BacklogSpec:
    """One work queue.

    Attributes:
        name: Stable public id (``^[a-z][a-z0-9_]{0,63}$``).
        drained_by: Job ids that drain it; ``()`` = drained by people.
        read: ``async (db, now) -> (count, oldest timestamp | None)``.
        enabled: Whether the deployment has the queue; disabled entries are
            omitted from the response.
    """

    name: str
    drained_by: tuple[str, ...]
    read: BacklogReader
    enabled: Callable[[Settings], bool] = _always


@dataclass(frozen=True)
class BacklogReading:
    """One entry of the response's ``backlogs``.

    ``count`` / ``oldest_at`` are ``None`` and ``error_class`` names the
    exception class when the reader failed (or ``DeadlineExceeded`` when the
    request's budget ran out before it).
    """

    name: str
    drained_by: tuple[str, ...]
    count: int | None
    oldest_at: datetime | None
    error_class: str | None = None


def _naive(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _count_min(db: AsyncSession, model: Any, oldest: Any, *where: Any) -> tuple[int, Any]:
    count, first = (
        await db.execute(select(func.count(), func.min(oldest)).select_from(model).where(*where))
    ).one()
    return int(count or 0), first


# --- embeddings (drained by the embedding sweep) -----------------------------


def _embedding_reader(status: str, *, stalled: bool | None = None) -> BacklogReader:
    async def read(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
        from config.constants import MAX_EMBEDDING_RETRIES
        from models.memory import Memory

        where = [
            # The sweep's partial index (embedding_status <> 'success').
            Memory.embedding_status != "success",
            Memory.embedding_status == status,
            Memory.deleted_at.is_(None),
            Memory.embedding_suppressed_at.is_(None),
        ]
        if stalled is True:
            where.append(Memory.embedding_retry_count >= MAX_EMBEDDING_RETRIES)
        elif stalled is False:
            where.append(Memory.embedding_retry_count < MAX_EMBEDDING_RETRIES)
        return await _count_min(db, Memory, Memory.created_at, *where)

    return read


# --- deferred point deletes -------------------------------------------------


async def _deferred_point_deletes(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    from services.deferred_point_deletes import deferred_backlog

    count, age_seconds = await deferred_backlog(db)
    oldest = now - timedelta(seconds=age_seconds) if age_seconds is not None else None
    return count, oldest


# --- erasure ------------------------------------------------------------------


def _erasure_reader(status: str) -> BacklogReader:
    async def read(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
        from models.erasure import ErasureRequest

        return await _count_min(
            db, ErasureRequest, ErasureRequest.requested_at, ErasureRequest.status == status
        )

    return read


async def _erasure_cooling_off_due(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    from models.erasure import STATUS_COOLING_OFF, ErasureRequest

    return await _count_min(
        db,
        ErasureRequest,
        ErasureRequest.scheduled_for,
        ErasureRequest.status == STATUS_COOLING_OFF,
        ErasureRequest.scheduled_for <= now,
    )


async def _vector_erasure_jobs(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    from models.vector_erasure import VectorErasureJob

    return await _count_min(
        db, VectorErasureJob, VectorErasureJob.created_at, VectorErasureJob.completed_at.is_(None)
    )


# --- ownership force transfers (a second admin decides) ---------------------


async def _force_transfer_requests(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    from models.auth import WorkspaceOwnershipForceTransferRequest as Request

    return await _count_min(db, Request, Request.created_at, Request.status == "pending")


# --- files ----------------------------------------------------------------------


async def _storage_object_deletions_due(
    db: AsyncSession, now: datetime
) -> tuple[int, datetime | None]:
    from models.file_objects import StorageObjectDeletion

    return await _count_min(
        db,
        StorageObjectDeletion,
        StorageObjectDeletion.available_after,
        StorageObjectDeletion.available_after <= func.timezone("UTC", func.now()),
    )


async def _file_reservations_expired(
    db: AsyncSession, now: datetime
) -> tuple[int, datetime | None]:
    from models.file_objects import FileObject
    from tasks.file_tasks import _ORPHAN_GRACE_SECONDS

    threshold = _naive(now) - timedelta(seconds=_ORPHAN_GRACE_SECONDS)
    return await _count_min(
        db,
        FileObject,
        FileObject.expires_at,
        FileObject.status == "reserved",
        FileObject.expires_at.isnot(None),
        FileObject.expires_at <= threshold,
        FileObject.deleted_at.is_(None),
    )


async def _file_soft_deleted_due(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    from models.file_objects import FileObject
    from tasks.file_tasks import _GC_RETENTION_SECONDS

    threshold = _naive(now) - timedelta(seconds=_GC_RETENTION_SECONDS)
    return await _count_min(
        db,
        FileObject,
        FileObject.deleted_at,
        FileObject.status.in_(("uploaded", "failed")),
        FileObject.deleted_at.isnot(None),
        FileObject.deleted_at <= threshold,
    )


# --- resource indexers ----------------------------------------------------------


async def _resource_indexer_due(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    from models.resource import IndexerState

    return await _count_min(
        db,
        IndexerState,
        IndexerState.next_run_at,
        IndexerState.job_status == "queued",
        IndexerState.next_run_at <= _naive(now),
    )


# --- retention cleanups (the job's own DELETE predicate, as a SELECT) ----------


async def _email_action_tokens_spent(
    db: AsyncSession, now: datetime
) -> tuple[int, datetime | None]:
    from config.settings import get_settings
    from models.auth import EmailActionToken
    from tasks.email_action_token_tasks import spent_email_action_tokens_delete

    cutoff = _naive(now) - timedelta(seconds=get_settings().email_action_token_retention_seconds)
    where = spent_email_action_tokens_delete(cutoff).whereclause
    return await _count_min(
        db,
        EmailActionToken,
        func.least(EmailActionToken.used_at, EmailActionToken.expires_at),
        where,
    )


async def _email_signup_requests_expired(
    db: AsyncSession, now: datetime
) -> tuple[int, datetime | None]:
    from models.auth import EmailSignupRequest
    from tasks.email_action_token_tasks import expired_email_signup_requests_delete

    where = expired_email_signup_requests_delete(_naive(now)).whereclause
    return await _count_min(db, EmailSignupRequest, EmailSignupRequest.expires_at, where)


async def _device_codes_expired(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    from config.settings import get_settings
    from models.auth import OAuth2DeviceCode
    from tasks.device_code_tasks import expired_device_codes_delete

    cutoff = _naive(now) - timedelta(seconds=get_settings().oauth_device_code_retention_seconds)
    where = expired_device_codes_delete(cutoff).whereclause
    return await _count_min(db, OAuth2DeviceCode, OAuth2DeviceCode.expires_at, where)


async def _known_devices_stale(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    from config.settings import get_settings
    from models.auth import UserKnownDevice
    from services.known_device_service import stale_devices_delete

    cutoff = _naive(now) - timedelta(days=get_settings().known_device_retention_days)
    where = stale_devices_delete(cutoff).whereclause
    return await _count_min(db, UserKnownDevice, UserKnownDevice.last_seen, where)


# --- referral grants ------------------------------------------------------------


async def _referral_grants_pending(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    """Pending grant sides the referral sweep would re-home (its own predicate)."""
    from models.auth import Workspace
    from models.referral import ReferralGrant
    from services.referral_service import _GRANT_SIDES, _uncredited

    sides = [
        select(ReferralGrant.granted_at.label("granted_at")).where(
            ReferralGrant.revoked_at.is_(None),
            _uncredited(workspace_col),
            select(Workspace.id)
            .where(Workspace.owner_user_id == user_col, Workspace.deleted_at.is_(None))
            .exists(),
        )
        for user_col, workspace_col in _GRANT_SIDES
    ]
    pending = union_all(*sides).subquery()
    count, oldest = (await db.execute(select(func.count(), func.min(pending.c.granted_at)))).one()
    return int(count or 0), oldest


def _referral_sweep_enabled(settings: Settings) -> bool:
    """The referral sweep is registered (its interval env is not 0)."""
    from config.constants import (
        REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_DEFAULT,
        REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_ENV,
    )
    from tasks.env import env_interval_seconds

    interval = env_interval_seconds(
        REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_ENV, REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_DEFAULT
    )
    return interval > 0


def _scan_enabled(settings: Settings) -> bool:
    """The upload malware scan is on (``FILE_SCAN_ENABLED``, #565)."""
    return settings.file_scan_enabled


_EMBEDDING_SWEEP = ("sweep_pending_embeddings",)
_ERASURE_SWEEP = ("sweep_pending_erasures",)

# The single registry, sorted by name.
BUILTIN_BACKLOGS: tuple[BacklogSpec, ...] = (
    BacklogSpec(
        "deferred_point_deletes", ("drain_deferred_point_deletes",), _deferred_point_deletes
    ),
    BacklogSpec("device_codes_expired", ("cleanup_expired_device_codes",), _device_codes_expired),
    BacklogSpec(
        "email_action_tokens_spent", ("cleanup_email_action_tokens",), _email_action_tokens_spent
    ),
    BacklogSpec(
        "email_signup_requests_expired",
        ("cleanup_email_action_tokens",),
        _email_signup_requests_expired,
    ),
    BacklogSpec(
        "embedding_failed_retryable",
        _EMBEDDING_SWEEP,
        _embedding_reader("failed", stalled=False),
    ),
    # Retries spent: the admin retry (POST /api/v1/admin/embedding/retry-failed) drains it.
    BacklogSpec("embedding_failed_stalled", (), _embedding_reader("failed", stalled=True)),
    BacklogSpec("embedding_pending", _EMBEDDING_SWEEP, _embedding_reader("pending")),
    BacklogSpec("embedding_processing", _EMBEDDING_SWEEP, _embedding_reader("processing")),
    # Awaiting the user's confirmation, and failed runs: people act on them.
    BacklogSpec("erasure_cooling_off", _ERASURE_SWEEP, _erasure_reader("cooling_off")),
    BacklogSpec("erasure_cooling_off_due", _ERASURE_SWEEP, _erasure_cooling_off_due),
    BacklogSpec("erasure_failed", (), _erasure_reader("failed")),
    BacklogSpec("erasure_in_progress", _ERASURE_SWEEP, _erasure_reader("in_progress")),
    BacklogSpec("erasure_pending", (), _erasure_reader("pending")),
    BacklogSpec("file_reservations_expired", ("orphan_file_sweeper",), _file_reservations_expired),
    BacklogSpec("file_soft_deleted_due", ("soft_delete_file_gc",), _file_soft_deleted_due),
    # Upload malware scan (#565). Infected files stay refused after a switch-off,
    # so the quarantine is always shown; files the scanner could not check
    # wait for people (rescan or delete).
    BacklogSpec(
        "files_awaiting_scan",
        (FILE_SCAN_SWEEPER_JOB_ID,),
        files_awaiting_scan_backlog,
        enabled=_scan_enabled,
    ),
    BacklogSpec("files_quarantined", (FILE_QUARANTINE_EXPIRY_JOB_ID,), files_quarantined_backlog),
    BacklogSpec("files_scan_failed", (), files_scan_failed_backlog, enabled=_scan_enabled),
    BacklogSpec(
        "files_unscanned",
        (FILE_SCAN_SWEEPER_JOB_ID,),
        files_unscanned_backlog,
        enabled=_scan_enabled,
    ),
    # A second system admin approves or cancels a force transfer.
    BacklogSpec("force_transfer_requests_pending", (), _force_transfer_requests),
    BacklogSpec("known_devices_stale", ("cleanup_stale_known_devices",), _known_devices_stale),
    BacklogSpec(
        "referral_grants_pending",
        ("apply_pending_referral_grants",),
        _referral_grants_pending,
        enabled=_referral_sweep_enabled,
    ),
    BacklogSpec("resource_indexer_due", ("resource_indexer",), _resource_indexer_due),
    BacklogSpec(
        "storage_object_deletions_due",
        ("storage_object_deletion_sweeper",),
        _storage_object_deletions_due,
    ),
    BacklogSpec("vector_erasure_jobs", _ERASURE_SWEEP, _vector_erasure_jobs),
)

_STATEMENT_TIMEOUT_SQL = text("SELECT set_config('statement_timeout', :ms, true)")
STATEMENT_TIMEOUT_MS = 5000


async def read_backlogs(
    db: AsyncSession,
    now: datetime,
    settings: Settings,
    *,
    deadline: Callable[[], float] | None = None,
) -> list[BacklogReading]:
    """Read every enabled backlog, sequentially, each in its own savepoint.

    A reader that fails leaves its class name on its entry; the others are
    still read. With ``deadline`` (seconds left in the request's budget),
    each statement's timeout is capped at what is left, and once nothing is
    left the remaining entries carry ``DeadlineExceeded``.

    Args:
        db: The request's session.
        now: Timezone-aware current time.
        settings: Settings (only the ``enabled`` predicates read them).
        deadline: Seconds left in the request's budget, or ``None``.
    """
    readings: list[BacklogReading] = []
    for spec in BUILTIN_BACKLOGS:
        if not spec.enabled(settings):
            continue
        left = deadline() if deadline is not None else None
        if left is not None and left <= 0:
            readings.append(
                BacklogReading(spec.name, spec.drained_by, None, None, DEADLINE_EXCEEDED)
            )
            continue
        ms = (
            STATEMENT_TIMEOUT_MS
            if left is None
            else max(1, min(STATEMENT_TIMEOUT_MS, int(left * 1000)))
        )
        try:
            async with db.begin_nested():
                await db.execute(_STATEMENT_TIMEOUT_SQL, {"ms": str(ms)})
                count, oldest = await spec.read(db, now)
        except Exception as exc:  # noqa: BLE001 - one queue's failure never hides the rest
            readings.append(
                BacklogReading(
                    spec.name,
                    spec.drained_by,
                    None,
                    None,
                    sanitize_error_class(type(exc).__name__),
                )
            )
            continue
        readings.append(BacklogReading(spec.name, spec.drained_by, int(count), _aware(oldest)))
    return readings


__all__ = [
    "BACKLOG_NAME_RE",
    "BUILTIN_BACKLOGS",
    "DEADLINE_EXCEEDED",
    "BacklogReader",
    "BacklogReading",
    "BacklogSpec",
    "read_backlogs",
]
