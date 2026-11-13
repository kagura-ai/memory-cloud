"""Upload malware scan: claim, scan, record, gate, backlog (Issue #565).

Off by default (``FILE_SCAN_ENABLED``). When on, a confirmed upload is
``pending`` until clamd has scanned the stored bytes, and its download URL is
refused until then (design A: hold until scanned, fail-closed).

State lives in ``file_objects.scan_*`` (``status`` and everything keyed on it
are untouched): ``unscanned`` → (backfill) → ``clean`` / ``infected``;
``pending`` → ``clean`` / ``infected`` / ``error``.

How a verdict is bound to the bytes that are served:

- checksum binding on: the storage only accepts the declared bytes, so a
  file is eligible at once and the confirm kicks an in-process scan;
- binding off: the presigned PUT URL stays valid after the confirm, so a file
  becomes eligible only after that URL expired (+ a margin); the sweeper scans
  it. The ETag, LastModified and size of the scanned version come from the
  same GetObject that fed clamd, and every download URL of a ``clean`` file
  HEADs the object and compares them (:func:`object_matches_scan`); a change
  puts the file back to ``pending`` (:func:`requeue_changed_object`).

Concurrency: a DB lease claims a row (``FOR UPDATE SKIP LOCKED``; the claim
moves ``scan_next_attempt_at`` to the lease end), the result UPDATE is fenced
on the claim (status, attempts, lease, live, digest). No session is held while
bytes stream. A per-process semaphore bounds the scans; it is taken before a
claim. The kick queue is bounded; the 30 s sweeper is the safety net.

Failure policy: clamd unreachable (nothing streamed) or storage unreachable
before streaming does not count against the file; a failure after streaming
started counts, and after ``FILE_SCAN_MAX_ATTEMPTS`` a new upload is
``error`` / ``scan_failed``. ``error`` files are blocked but never deleted
automatically; ``infected`` files are soft-deleted after
``FILE_QUARANTINE_RETENTION_DAYS``. Files uploaded before scanning was on
(``unscanned``) stay downloadable and are backfilled at low priority; they are
blocked only when found infected.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar, cast
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings, get_settings
from db.base import get_db
from models.file_objects import SCAN_LEGACY_REQUEUED, FileObject
from services import storage_quota_service
from services.clamd_client import (
    CHUNK_BYTES,
    ClamdScanFailed,
    ClamdSizeLimit,
    ClamdUnavailable,
    clamd_instream,
    clamd_ping,
)
from storage.factory import get_blob_storage
from storage.protocol import BlobReaderProtocol, ObjectMetadata
from utils.datetime import to_naive_utc, utcnow
from utils.exceptions import ExternalServiceError, FileBlockedError, FileScanPendingError
from utils.logger import get_logger

if TYPE_CHECKING:
    from tasks.job_runs import JobFailed

logger = get_logger(__name__)

T = TypeVar("T")

#: Scheduler job ids (``tasks/file_tasks.py::schedule_file_tasks``).
FILE_SCAN_SWEEPER_JOB_ID: Final = "file_scan_sweeper"
FILE_QUARANTINE_EXPIRY_JOB_ID: Final = "file_quarantine_expiry"

#: Kicks alive at once (waiting for a slot or scanning); more are dropped and
#: left to the sweeper, so a flood of small uploads cannot pile up tasks.
KICK_QUEUE_LIMIT: Final = 16
#: Infected rows soft-deleted per transaction by the quarantine expiry.
EXPIRY_BATCH: Final = 50
_EXPIRY_MAX_BATCHES: Final = 200
#: Lease beyond the scan timeout before another claim may take the row.
_LEASE_SLACK: Final = timedelta(seconds=60)
_BACKOFF_BASE: Final = timedelta(seconds=30)
_BACKOFF_CAP: Final = timedelta(minutes=5)
#: How long one sweeper worker keeps claiming the next file with its slot.
_WORKER_BUDGET_SECONDS: Final = 240.0
#: Creators of rows the attachment migration wrote (no uploader account).
_MIGRATED_ATTACHMENT_PREFIX: Final = "migration:attachment:"

ScanKind = Literal[
    "clean",
    "infected",
    "unscannable",
    "object_missing",
    "digest_mismatch",
    "size_limit",
    "retry",
    "retry_uncounted",
]


@dataclass(frozen=True)
class ClaimedScan:
    """One row claimed for a scan, with the values the result is fenced on."""

    file_id: UUID
    workspace_id: UUID
    storage_key: str
    sha256: str
    size_bytes: int
    created_by: str
    #: ``pending`` (a new upload) or ``unscanned`` (the backfill).
    claimed_status: str
    attempts: int
    lease_until: datetime
    #: A legacy row a context restore sent back to the scan (``pending``).
    legacy_requeued: bool = False

    @property
    def backfill(self) -> bool:
        return self.claimed_status == "unscanned"

    @property
    def digest_unverified(self) -> bool:
        """A legacy row: its stored bytes were never checked against its sha256."""
        return self.backfill or self.legacy_requeued


@dataclass(frozen=True)
class ScanResult:
    """What one scan found; :func:`scan_outcome` turns it into column values."""

    kind: ScanKind
    signature: str | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    #: Short reason for logs (an exception class name), never clamd text.
    reason: str | None = None


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def scan_eligible_at(
    *,
    now: datetime,
    reservation_expires_at: datetime | None,
    server_written: bool,
    settings: Settings,
) -> datetime:
    """When a file may be scanned (R1 a/b).

    Checksum binding on, or bytes the server wrote itself: now. Binding off:
    after the reservation's presigned PUT URL expired plus
    ``FILE_SCAN_PUT_EXPIRY_MARGIN_SECONDS``, so the scanned bytes are the final
    ones. Pass ``reservation_expires_at=None`` when the last PUT URL for the
    key is unknown (a restore, a changed object): a full PUT TTL is counted
    from ``now``. Naive UTC in, naive UTC out.
    """
    if server_written or settings.storage_checksum_binding_enabled:
        return now
    margin = timedelta(seconds=settings.file_scan_put_expiry_margin_seconds)
    if reservation_expires_at is None:
        return now + timedelta(seconds=settings.presign_put_ttl_seconds) + margin
    return max(now, reservation_expires_at + margin)


def scan_backoff(attempts: int) -> timedelta:
    """Delay before the next try after ``attempts`` counted failures: 30 s doubling, 5 min cap."""
    if attempts <= 1:
        return _BACKOFF_BASE
    return min(_BACKOFF_BASE * (2 ** min(attempts - 1, 16)), _BACKOFF_CAP)


def ensure_downloadable(file: FileObject, settings: Settings) -> None:
    """The download gate (R7): an allow-list, raised after the access checks.

    Scanning on: only ``clean`` and ``unscanned`` pass; ``pending`` raises
    :class:`FileScanPendingError` (409 ``SCAN-001``); anything else, a future
    or unknown value included, raises :class:`FileBlockedError` (409
    ``SCAN-002``). Scanning off: everything passes except ``infected`` (a
    finding stays refused after the switch is turned off).
    """
    status = file.scan_status
    if settings.file_scan_enabled:
        if status in ("clean", "unscanned"):
            return
        if status == "pending":
            raise FileScanPendingError(str(file.id))
        raise FileBlockedError(str(file.id), str(status))
    if status == "infected":
        raise FileBlockedError(str(file.id), status)


def object_matches_scan(file: Any, meta: ObjectMetadata) -> bool:
    """Whether the stored object is still the version that was scanned (R1 c).

    Compares ETag, size and LastModified of a fresh ``head_object`` with what
    the scan's own GetObject reported. Any difference (also a same-ETag
    rewrite, which only LastModified shows) means the object changed.
    """
    if not file.scan_etag:
        return False
    return (
        meta.get("etag") == file.scan_etag
        and meta.get("size_bytes") == file.size_bytes
        and meta.get("last_modified") == file.scan_last_modified
    )


def _give_up_values(claim: ClaimedScan, cause: str, settings: Settings, now: datetime):
    """A permanent cause: a new upload is blocked; the backfill leaves the file as it was."""
    if claim.backfill:
        return "unscanned", {
            "scan_status": "unscanned",
            "scan_error": cause,
            "scan_attempts": max(claim.attempts, settings.file_scan_max_attempts),
            "scan_lease_until": None,
            "scan_next_attempt_at": None,
        }
    return "error", {
        "scan_status": "error",
        "scan_error": cause,
        "scanned_at": now,
        "scan_lease_until": None,
        "scan_next_attempt_at": None,
    }


def scan_outcome(
    claim: ClaimedScan, result: ScanResult, *, now: datetime, settings: Settings
) -> tuple[str, dict[str, Any]]:
    """The row's new ``scan_status`` and the column values for ``result``."""
    if result.kind in ("clean", "infected"):
        return result.kind, {
            "scan_status": result.kind,
            "scanned_at": now,
            "scan_signature": result.signature if result.kind == "infected" else None,
            "scan_etag": result.etag,
            "scan_last_modified": result.last_modified,
            "scan_error": None,
            "scan_lease_until": None,
            "scan_next_attempt_at": None,
        }
    if result.kind in ("unscannable", "object_missing", "digest_mismatch", "size_limit"):
        state, values = _give_up_values(claim, result.kind, settings, now)
        if result.kind == "unscannable":
            values["scan_signature"] = result.signature
        return state, values
    if result.kind == "retry_uncounted":
        return claim.claimed_status, {
            "scan_attempts": max(claim.attempts - 1, 0),
            "scan_lease_until": None,
            "scan_next_attempt_at": now + _BACKOFF_BASE,
        }
    # Counted failure.
    if claim.attempts >= settings.file_scan_max_attempts:
        return _give_up_values(claim, "scan_failed", settings, now)
    return claim.claimed_status, {
        "scan_lease_until": None,
        "scan_next_attempt_at": now + scan_backoff(claim.attempts),
    }


# ---------------------------------------------------------------------------
# Claim / scan / record
# ---------------------------------------------------------------------------


async def claim_scan(
    db: AsyncSession,
    *,
    now: datetime,
    settings: Settings,
    file_id: UUID | None = None,
    backfill: bool = False,
) -> ClaimedScan | None:
    """Claim one due row (or ``file_id``) and count the attempt; the caller commits.

    ``FOR UPDATE SKIP LOCKED`` so a kick and the leader's sweep never wait on
    each other or take the same row; the lease end becomes the next attempt
    time, which takes the row out of the due set until the lease runs out.
    """
    if backfill:
        conditions = [
            FileObject.scan_status == "unscanned",
            FileObject.scan_attempts < settings.file_scan_max_attempts,
            or_(
                FileObject.scan_next_attempt_at.is_(None),
                FileObject.scan_next_attempt_at <= now,
            ),
        ]
        order = FileObject.id
    else:
        conditions = [
            FileObject.scan_status == "pending",
            FileObject.scan_next_attempt_at <= now,
        ]
        order = FileObject.scan_next_attempt_at
    conditions += [FileObject.status == "uploaded", FileObject.deleted_at.is_(None)]
    if file_id is not None:
        conditions.append(FileObject.id == file_id)
    target = (
        select(FileObject.id)
        .where(*conditions)
        .order_by(order)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    lease = now + timedelta(seconds=settings.clamd_scan_timeout_seconds) + _LEASE_SLACK
    row = (
        await db.execute(
            update(FileObject)
            .where(FileObject.id == target)
            .values(
                scan_attempts=FileObject.scan_attempts + 1,
                scan_lease_until=lease,
                scan_next_attempt_at=lease,
            )
            .returning(
                FileObject.id,
                FileObject.workspace_id,
                FileObject.storage_key,
                FileObject.sha256,
                FileObject.size_bytes,
                FileObject.created_by,
                FileObject.scan_status,
                FileObject.scan_attempts,
                FileObject.scan_lease_until,
                FileObject.scan_error,
            )
            .execution_options(synchronize_session=False)
        )
    ).first()
    if row is None:
        return None
    return ClaimedScan(
        file_id=row.id,
        workspace_id=row.workspace_id,
        storage_key=row.storage_key or "",
        sha256=row.sha256.lower(),
        size_bytes=row.size_bytes,
        created_by=row.created_by,
        claimed_status=row.scan_status,
        attempts=row.scan_attempts,
        lease_until=row.scan_lease_until,
        legacy_requeued=row.scan_error == SCAN_LEGACY_REQUEUED,
    )


async def scan_claimed(
    claim: ClaimedScan, *, storage: BlobReaderProtocol, settings: Settings
) -> ScanResult:
    """Stream the claimed object to clamd and classify (no DB session involved).

    Precedence (R6): a finding beats a digest mismatch, which beats ``OK``.
    A new upload whose stored bytes do not match its sha256 is
    ``digest_mismatch`` (blocked); for a legacy row (the backfill, or one a
    context restore requeued) the verdict stands and the mismatch is only
    logged (its digest was never verified).
    """
    digest = hashlib.sha256()
    opened_at_all = False
    try:
        async with storage.open_object(claim.storage_key) as opened:
            opened_at_all = True
            if opened is None:
                return ScanResult("object_missing")
            if opened.size_bytes > settings.clamd_stream_max_bytes:
                return ScanResult("size_limit")

            async def chunks() -> AsyncIterator[bytes]:
                async for chunk in opened.iter_chunks(CHUNK_BYTES):
                    digest.update(chunk)
                    yield chunk

            verdict = await clamd_instream(
                settings.clamd_host,
                settings.clamd_port,
                chunks(),
                expected_bytes=opened.size_bytes,
                connect_timeout=settings.clamd_connect_timeout_seconds,
                scan_timeout=settings.clamd_scan_timeout_seconds,
            )
            etag, last_modified = opened.etag, opened.last_modified
    except ClamdUnavailable:
        return ScanResult("retry_uncounted", reason="clamd_unavailable")
    except ClamdSizeLimit:
        return ScanResult("size_limit")
    except ClamdScanFailed as exc:
        return ScanResult("retry", reason=type(exc).__name__)
    except ExternalServiceError as exc:
        if not opened_at_all:
            return ScanResult("retry_uncounted", reason="storage_unavailable")
        return ScanResult("retry", reason=type(exc).__name__)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad file must never stop the sweep
        if not opened_at_all:
            return ScanResult("retry_uncounted", reason=type(exc).__name__)
        return ScanResult("retry", reason=type(exc).__name__)

    if verdict.outcome == "infected":
        return ScanResult("infected", verdict.signature, etag, last_modified)
    if verdict.outcome == "unscannable":
        return ScanResult("unscannable", verdict.signature)
    if digest.hexdigest() != claim.sha256.lower():
        logger.warning(
            "file_scan_digest_mismatch",
            file_id=str(claim.file_id),
            workspace_id=str(claim.workspace_id),
            backfill=claim.backfill,
        )
        if not claim.digest_unverified:
            return ScanResult("digest_mismatch")
    return ScanResult("clean", None, etag, last_modified)


def _fence(claim: ClaimedScan) -> list[Any]:
    return [
        FileObject.id == claim.file_id,
        FileObject.scan_status == claim.claimed_status,
        FileObject.scan_attempts == claim.attempts,
        FileObject.scan_lease_until == claim.lease_until,
        FileObject.deleted_at.is_(None),
        func.lower(FileObject.sha256) == claim.sha256.lower(),
    ]


async def record_scan_result(
    db: AsyncSession,
    claim: ClaimedScan,
    result: ScanResult,
    *,
    now: datetime,
    settings: Settings,
) -> str | None:
    """Write ``result`` if the claim still holds the row; the caller commits.

    Returns the row's new ``scan_status``, or ``None`` when the fence discarded
    the result (the file was deleted, requeued or claimed again meanwhile).
    """
    state, values = scan_outcome(claim, result, now=now, settings=settings)
    written = cast(
        CursorResult[Any],
        await db.execute(
            update(FileObject)
            .where(*_fence(claim))
            .values(**values)
            .execution_options(synchronize_session=False)
        ),
    )
    return state if written.rowcount else None


async def release_claim(db: AsyncSession, claim: ClaimedScan, *, now: datetime) -> bool:
    """Give an interrupted claim back (shutdown): due now, the attempt not counted."""
    written = cast(
        CursorResult[Any],
        await db.execute(
            update(FileObject)
            .where(*_fence(claim))
            .values(
                scan_attempts=max(claim.attempts - 1, 0),
                scan_lease_until=None,
                scan_next_attempt_at=now,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    return bool(written.rowcount)


async def requeue_changed_object(
    db: AsyncSession, file: FileObject, *, now: datetime, settings: Settings
) -> bool:
    """A ``clean`` file whose object changed goes back to ``pending`` (R1 c); the caller commits.

    Fenced on the recorded ETag so two downloads racing on the same change
    requeue it once. The next attempt follows the PUT-window rule counted from
    ``now`` (a PUT URL minted up to now may still be live).
    """
    written = cast(
        CursorResult[Any],
        await db.execute(
            update(FileObject)
            .where(
                FileObject.id == file.id,
                FileObject.scan_status == "clean",
                FileObject.scan_etag.is_not_distinct_from(file.scan_etag),
                FileObject.deleted_at.is_(None),
            )
            .values(
                scan_status="pending",
                scan_attempts=0,
                scan_lease_until=None,
                scanned_at=None,
                scan_next_attempt_at=scan_eligible_at(
                    now=now, reservation_expires_at=None, server_written=False, settings=settings
                ),
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if written.rowcount:
        logger.warning("file_scan_object_changed", file_id=str(file.id))
    return bool(written.rowcount)


# ---------------------------------------------------------------------------
# Runtime: kick, sweep, shutdown
# ---------------------------------------------------------------------------

# Strong references to scan tasks: the event loop keeps tasks only weakly.
_running: set[asyncio.Task[None]] = set()
_kicks: set[asyncio.Task[None]] = set()
_slots_by_loop: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _reset_runtime_for_tests() -> None:
    _running.clear()
    _kicks.clear()
    _slots_by_loop.clear()


def _slots() -> asyncio.Semaphore:
    """The process's scan slots (``FILE_SCAN_CONCURRENCY``), one set per event loop."""
    loop = asyncio.get_running_loop()
    slots = _slots_by_loop.get(loop)
    if slots is None:
        for stale in [lp for lp in _slots_by_loop if lp.is_closed()]:
            del _slots_by_loop[stale]
        slots = asyncio.Semaphore(get_settings().file_scan_concurrency)
        _slots_by_loop[loop] = slots
    return slots


def _spawn(coro: Awaitable[None], *, kick: bool = False) -> asyncio.Task[None]:
    task = asyncio.get_running_loop().create_task(cast(Any, coro))
    _running.add(task)
    task.add_done_callback(_running.discard)
    if kick:
        _kicks.add(task)
        task.add_done_callback(_kicks.discard)
    return task


def _reader() -> BlobReaderProtocol | None:
    """The blob storage when it is configured and can stream objects."""
    try:
        storage = get_blob_storage()
    except ExternalServiceError:
        return None
    if not isinstance(storage, BlobReaderProtocol):
        logger.warning("file_scan_storage_cannot_read")
        return None
    return storage


async def _in_session(work: Callable[[AsyncSession], Awaitable[T]]) -> T | None:
    """Run ``work`` in a short session of its own and commit it."""
    result: T | None = None
    async for db in get_db():
        result = await work(db)
        await db.commit()
    return result


def _notify_uploader(claim: ClaimedScan) -> None:
    """Tell the uploader that a file of theirs was blocked (owner decision: email + UI).

    Rows the attachment migration wrote have no uploader account. The email
    has its own generic text (no filename, finding name or link), goes to the
    account's verified address only, and is coalesced per account by the
    notification service. Called only after an ``infected`` result committed;
    never raises.
    """
    if claim.created_by.startswith(_MIGRATED_ATTACHMENT_PREFIX):
        return
    from services.security_notification_service import (
        SecurityEvent,
        spawn_security_notification,
    )

    spawn_security_notification(user_id=claim.created_by, event=SecurityEvent.FILE_BLOCKED)


async def _scan_and_record(claim: ClaimedScan, storage: BlobReaderProtocol) -> ScanResult:
    """Scan one claimed row (no session open) and record the result in a short session."""
    settings = get_settings()
    try:
        result = await scan_claimed(claim, storage=storage, settings=settings)
    except asyncio.CancelledError:
        await asyncio.shield(_give_back(claim))
        raise
    state = await _in_session(
        lambda db: record_scan_result(db, claim, result, now=utcnow(), settings=settings)
    )
    log = {
        "file_id": str(claim.file_id),
        "workspace_id": str(claim.workspace_id),
        "backfill": claim.backfill,
        "attempts": claim.attempts,
    }
    if state is None:
        logger.info("file_scan_result_discarded", **log)
    elif state == "infected":
        logger.warning("file_scan_infected", signature=result.signature, **log)
        _notify_uploader(claim)
    elif state == "error":
        logger.warning("file_scan_gave_up", cause=result.kind, reason=result.reason, **log)
    elif result.kind in ("retry", "retry_uncounted"):
        logger.info("file_scan_retry", cause=result.kind, reason=result.reason, **log)
    else:
        logger.info("file_scan_recorded", scan_status=state, **log)
    return result


async def _give_back(claim: ClaimedScan) -> None:
    try:
        await _in_session(lambda db: release_claim(db, claim, now=utcnow()))
    except Exception as exc:  # noqa: BLE001 - shutdown path; the lease expires anyway
        logger.warning("file_scan_release_failed", error_class=type(exc).__name__)


async def _kick(file_id: UUID) -> None:
    try:
        async with _slots():
            storage = _reader()
            if storage is None:
                return
            settings = get_settings()
            claim = await _in_session(
                lambda db: claim_scan(db, now=utcnow(), settings=settings, file_id=file_id)
            )
            if claim is not None:
                await _scan_and_record(claim, storage)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - best effort; the sweeper is the safety net
        logger.warning("file_scan_kick_failed", error_class=type(exc).__name__)


def kick_file_scan(file_id: UUID) -> None:
    """Start scanning ``file_id`` in this process now, if it may be (never raises).

    A no-op when scanning is off, when checksum binding is off (the file is
    not eligible before its PUT URL expired; the sweeper takes it), outside an
    event loop, or when ``KICK_QUEUE_LIMIT`` kicks are already alive. Call it
    after the commit that made the row ``pending``; it opens its own sessions.
    """
    try:
        settings = get_settings()
        if not settings.file_scan_enabled or not settings.storage_checksum_binding_enabled:
            return
        asyncio.get_running_loop()  # outside a loop: RuntimeError, nothing created
        if len(_kicks) >= KICK_QUEUE_LIMIT:
            logger.info("file_scan_kick_dropped", file_id=str(file_id))
            return
        _spawn(_kick(file_id), kick=True)
    except Exception as exc:  # noqa: BLE001 - never fail the confirm
        logger.info("file_scan_kick_not_started", error_class=type(exc).__name__)


@dataclass
class _TickBudget:
    backfill: int


async def _claim_next(settings: Settings, budget: _TickBudget) -> ClaimedScan | None:
    """The next due new upload; else, while the tick's budget lasts, a legacy file."""
    claim = await _in_session(lambda db: claim_scan(db, now=utcnow(), settings=settings))
    if claim is not None or budget.backfill <= 0:
        return claim
    budget.backfill -= 1
    claim = await _in_session(
        lambda db: claim_scan(db, now=utcnow(), settings=settings, backfill=True)
    )
    if claim is None:
        budget.backfill = 0
    return claim


async def _drain(
    claim: ClaimedScan,
    storage: BlobReaderProtocol,
    settings: Settings,
    budget: _TickBudget,
    slots: asyncio.Semaphore,
) -> None:
    """Scan ``claim`` with the slot already held, then keep claiming until the queue is empty."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WORKER_BUDGET_SECONDS
    try:
        current: ClaimedScan | None = claim
        while current is not None:
            result = await _scan_and_record(current, storage)
            if result.kind == "retry_uncounted" or loop.time() > deadline:
                break
            current = await _claim_next(settings, budget)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the next tick starts a new worker
        logger.warning("file_scan_worker_failed", error_class=type(exc).__name__)
    finally:
        slots.release()


async def run_scan_sweep() -> dict[str, int] | JobFailed:
    """Scheduler entry (every 30 s): hand due scans to workers and return at once.

    Quiet no-op when scanning is off or storage is unconfigured. When clamd
    does not answer PING it logs one warning, touches no row and returns a
    failed run (``ClamdUnavailable``, so the operations status shows the job
    failing for the length of the outage). Otherwise it
    claims at most the free slots (new uploads first, then up to
    ``FILE_SCAN_BACKFILL_BATCH`` legacy files per tick); each claim runs in a
    worker task that keeps its slot for the next due file.
    """
    counts = {"dispatched": 0, "backfill_dispatched": 0}
    settings = get_settings()
    if not settings.file_scan_enabled:
        return counts
    storage = _reader()
    if storage is None:
        return counts
    if not await clamd_ping(
        settings.clamd_host, settings.clamd_port, timeout=settings.clamd_connect_timeout_seconds
    ):
        logger.warning("file_scan_clamd_unavailable")
        # Imported here: ``tasks`` imports this module through ``tasks.file_tasks``.
        from tasks.job_runs import job_failed

        return job_failed(ClamdUnavailable())
    budget = _TickBudget(backfill=settings.file_scan_backfill_batch)
    slots = _slots()
    while not slots.locked():
        await slots.acquire()
        try:
            claim = await _claim_next(settings, budget)
        except BaseException:
            slots.release()
            raise
        if claim is None:
            slots.release()
            break
        counts["backfill_dispatched" if claim.backfill else "dispatched"] += 1
        _spawn(_drain(claim, storage, settings, budget, slots))
    if counts["dispatched"] or counts["backfill_dispatched"]:
        logger.info("file_scan_sweep", **counts)
    return counts


async def stop_file_scans(timeout: float = 5.0) -> None:
    """Cancel this process's scans and wait for them (API shutdown, before the DB closes).

    A cancelled scan gives its claim back (shielded), so the file is due again
    at once instead of after the lease.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    tasks = [t for t in list(_running) if not t.done() and t.get_loop() is loop]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)


# ---------------------------------------------------------------------------
# Quarantine expiry
# ---------------------------------------------------------------------------


async def expire_quarantined_files() -> dict[str, int]:
    """Scheduler entry (daily): soft-delete ``infected`` files past the retention.

    Runs whether or not scanning is on (findings stay after a switch-off).
    ``error`` files are never touched (R3). Batches of :data:`EXPIRY_BATCH`
    rows, each committed before its Redis bytes are released (Redis follows
    the DB). The nightly file GC removes the objects after the usual
    soft-delete retention.
    """
    from services.file_storage_service import FileStorageService

    settings = get_settings()
    cutoff = utcnow() - timedelta(days=settings.file_quarantine_retention_days)
    counts = {"expired": 0, "released_bytes": 0}
    for _ in range(_EXPIRY_MAX_BATCHES):
        purged = await _in_session(
            lambda db: FileStorageService(db).purge_quarantined_files(
                cutoff=cutoff, limit=EXPIRY_BATCH
            )
        )
        if purged is None:
            break
        for workspace_id, size_bytes in purged.released.items():
            await storage_quota_service.release_storage_bytes(
                workspace_id=workspace_id, size_bytes=size_bytes
            )
        counts["expired"] += purged.rows
        counts["released_bytes"] += sum(purged.released.values())
        if purged.rows < EXPIRY_BATCH:
            break
    if counts["expired"]:
        logger.info("file_quarantine_expired", **counts)
    return counts


# ---------------------------------------------------------------------------
# Backlogs (aggregates only: never ids, names or signatures)
# ---------------------------------------------------------------------------


async def _count_and_oldest(
    db: AsyncSession, column: Any, *conditions: Any
) -> tuple[int, datetime | None]:
    row = (await db.execute(select(func.count(), func.min(column)).where(*conditions))).one()
    return int(row[0] or 0), row[1]


async def files_awaiting_scan_backlog(
    db: AsyncSession, now: datetime
) -> tuple[int, datetime | None]:
    """Due pending scans: count and the oldest due time (naive UTC).

    ``now`` may be aware (the operations status passes one): the column is naive UTC.
    """
    return await _count_and_oldest(
        db,
        FileObject.scan_next_attempt_at,
        FileObject.scan_status == "pending",
        FileObject.status == "uploaded",
        FileObject.deleted_at.is_(None),
        FileObject.scan_next_attempt_at <= to_naive_utc(now),
    )


async def files_scan_failed_backlog(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    """Files the scanner could not check (blocked until rescanned or deleted)."""
    return await _count_and_oldest(
        db,
        FileObject.scanned_at,
        FileObject.scan_status == "error",
        FileObject.deleted_at.is_(None),
    )


async def files_quarantined_backlog(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    """Infected files still listed (until the quarantine expiry)."""
    return await _count_and_oldest(
        db,
        FileObject.scanned_at,
        FileObject.scan_status == "infected",
        FileObject.deleted_at.is_(None),
    )


async def files_unscanned_backlog(db: AsyncSession, now: datetime) -> tuple[int, datetime | None]:
    """Uploaded files never scanned (legacy, or confirmed while scanning was off)."""
    return await _count_and_oldest(
        db,
        FileObject.uploaded_at,
        FileObject.scan_status == "unscanned",
        FileObject.status == "uploaded",
        FileObject.deleted_at.is_(None),
    )


@dataclass(frozen=True)
class FileScanBacklog:
    """The scan queue at a glance (CLI ``status``)."""

    enabled: bool
    due_pending: int
    oldest_due_pending_at: datetime | None
    waiting_pending: int
    errored: int
    quarantined: int
    unscanned: int


async def file_scan_backlog(db: AsyncSession, now: datetime) -> FileScanBacklog:
    due, oldest = await files_awaiting_scan_backlog(db, now)
    waiting = await db.scalar(
        select(func.count()).where(
            FileObject.scan_status == "pending",
            FileObject.status == "uploaded",
            FileObject.deleted_at.is_(None),
            or_(
                FileObject.scan_next_attempt_at.is_(None),
                FileObject.scan_next_attempt_at > to_naive_utc(now),
            ),
        )
    )
    errored, _ = await files_scan_failed_backlog(db, now)
    quarantined, _ = await files_quarantined_backlog(db, now)
    unscanned, _ = await files_unscanned_backlog(db, now)
    return FileScanBacklog(
        enabled=get_settings().file_scan_enabled,
        due_pending=due,
        oldest_due_pending_at=oldest,
        waiting_pending=int(waiting or 0),
        errored=errored,
        quarantined=quarantined,
        unscanned=unscanned,
    )


# ---------------------------------------------------------------------------
# Operator rescan (CLI) and the startup warning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RescanSelection:
    """Rows a rescan would put back to ``pending`` (ids and their current state)."""

    rows: tuple[tuple[UUID, str, str | None], ...] = ()

    @property
    def count(self) -> int:
        return len(self.rows)


async def requeue_for_rescan(
    db: AsyncSession,
    *,
    now: datetime,
    settings: Settings,
    dry_run: bool,
    file_id: UUID | None = None,
    errors_only: bool = False,
    errors_since: datetime | None = None,
) -> RescanSelection:
    """Put files back to ``pending`` so they are scanned again (operator CLI).

    ``file_id`` alone: that file, whatever its verdict (e.g. a false positive
    after a signature update). ``errors_only``: only ``error`` files, narrowed
    by ``file_id`` and/or ``errors_since`` (a verdict that recent or newer) —
    one of the two is required, so a rescan of every failure is never one
    flag away. Only live ``uploaded`` rows are touched. Without checksum
    binding a row still inside its PUT window waits for it. Does not commit.
    """
    if file_id is None and not (errors_only and errors_since is not None):
        raise ValueError("rescan needs --file, or --errors with --since")
    conditions: list[Any] = [FileObject.status == "uploaded", FileObject.deleted_at.is_(None)]
    if file_id is not None:
        conditions.append(FileObject.id == file_id)
    if errors_only:
        conditions.append(FileObject.scan_status == "error")
    if errors_since is not None:
        conditions.append(FileObject.scanned_at >= errors_since)
    rows = (
        await db.execute(
            select(FileObject.id, FileObject.scan_status, FileObject.scan_error)
            .where(*conditions)
            .order_by(FileObject.id)
            .with_for_update()
        )
    ).all()
    selection = RescanSelection(rows=tuple((r[0], r[1], r[2]) for r in rows))
    if dry_run or not rows:
        return selection
    if settings.storage_checksum_binding_enabled:
        next_attempt: Any = now
    else:
        window = timedelta(
            seconds=settings.presign_put_ttl_seconds + settings.file_scan_put_expiry_margin_seconds
        )
        next_attempt = func.greatest(now, FileObject.created_at + window)
    await db.execute(
        update(FileObject)
        .where(FileObject.id.in_([r[0] for r in rows]))
        .values(
            scan_status="pending",
            scan_attempts=0,
            scan_lease_until=None,
            scan_next_attempt_at=next_attempt,
            scan_error=None,
            scan_signature=None,
            scanned_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    logger.info("file_scan_requeued", count=len(rows))
    return selection


def log_scan_configuration(settings: Settings) -> None:
    """Startup warning (R1 d): scanning without checksum binding holds new uploads longer.

    Without binding a new upload is held until its PUT URL expired (about
    ``PRESIGN_PUT_TTL_SECONDS`` + margin from the reservation), and every
    download URL of a clean file costs one storage HEAD. Binding on is the
    hard guarantee that the scanned bytes are the served ones.
    """
    if settings.file_scan_enabled and not settings.storage_checksum_binding_enabled:
        logger.warning(
            "file_scan_without_checksum_binding",
            hold_seconds=settings.presign_put_ttl_seconds
            + settings.file_scan_put_expiry_margin_seconds,
        )
