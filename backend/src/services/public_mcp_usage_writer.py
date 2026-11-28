"""Bounded per-process writer of public MCP usage events (#2678).

The public ``tools/call`` path, and the REST public search for a key bound to
the context (#3035), only call ``offer``: it builds the row (a few small
HMACs) and ``put_nowait``s it on a bounded queue. It never awaits and never
raises, so a slow or absent database cannot slow a search.

A drainer task batches up to ``PUBLIC_TELEMETRY_BATCH_MAX`` rows or one
flush interval and writes them in one transaction (events, then the batch's
doc keys under a savepoint) bounded by the write timeout. A failed batch is
retried once after a short backoff and then dropped. Every dropped event —
queue full, write failed, shutdown — is counted and becomes a ``gap`` row
written at the head of the next successful batch, so the stream says where
it is incomplete instead of skipping silently. A process crash loses at most
the queue.

Daily visitors (#3035) are written apart from the events: a visitor a
served bound-key search reported joins a bounded per-process pending set,
grouped by endpoint and UTC day, once its event was queued. On every drainer
pass, in a transaction of its own that writes no event row, the writer stores
every group that holds at least ``VISITOR_FLUSH_MIN_KEYS`` distinct keys and
every group whose day ended at least ``VISITOR_DAY_GRACE`` ago; ``stop``
stores every group. A search that started before midnight and answers after
it joins its day's last group; a visitor of a day whose last group was already
due is dropped and counted, never written alone. So a visitor row is never
written in the transaction of a call, and is written among at least that many
visitors' calls except for a day's (or a process's) last few values. Rows are
inserted sorted, ``ON CONFLICT DO NOTHING``; a failed write is retried once,
then its keys are dropped (an undercount) and counted.

One writer per process (``start_usage_writer`` in the API lifespan); its
health numbers are process-local.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, String, Uuid, bindparam, cast, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from config.settings import get_settings
from models.public_mcp_usage import PublicMcpDailyVisitor, PublicMcpDocKey, PublicMcpUsageEvent
from models.resource import Resource
from services.public_mcp_usage_events import (
    BuiltUsageEvent,
    UsageEventInput,
    VisitorRow,
    build_gap_row,
    build_usage_event_row,
)
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

PUBLIC_TELEMETRY_BATCH_MAX = 200
# #3035: a group of one endpoint and UTC day is written once it holds this many
# distinct visitor keys (or its day ended VISITOR_DAY_GRACE ago, or the writer
# stops). Ten is the usual minimum cell size of published counts. A constant,
# never a setting.
VISITOR_FLUSH_MIN_KEYS = 10
# A finished day's group waits this long after midnight UTC, far longer than a
# search runs, so searches that started before midnight and answer after it
# join that day's last group. A visitor of a day past this point is dropped
# (and counted) rather than written in a group of its own.
VISITOR_DAY_GRACE = timedelta(minutes=10)
# Visitor keys held per process before a new one is dropped (and counted).
VISITOR_PENDING_MAX = 50_000
_RETRY_BACKOFF_CAP_SECONDS = 30.0
_LOG_EVERY_SECONDS = 60.0


@dataclass
class _Gap:
    count: int
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True)
class WriterHealth:
    """This process's writer state (process-local)."""

    enabled: bool
    queue_depth: int
    queue_capacity: int
    dropped_since_start: int
    last_flush_at: datetime | None
    last_flush_ok: bool | None
    pending_gap: int


class PublicMcpUsageWriter:
    """Queue + drainer for ``public_mcp_usage_events``."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        queue_size: int,
        flush_interval: float,
        write_timeout: float,
        key: str,
        session_details_enabled: bool,
        retry_backoff: float = 1.0,
        batch_max: int = PUBLIC_TELEMETRY_BATCH_MAX,
        visitor_flush_min_keys: int = VISITOR_FLUSH_MIN_KEYS,
    ) -> None:
        self._session_factory = session_factory
        self._capacity = queue_size
        # ``None`` is the stop sentinel; it never counts as an event.
        self._queue: asyncio.Queue[BuiltUsageEvent | None] = asyncio.Queue(maxsize=queue_size)
        self._flush_interval = flush_interval
        self._write_timeout = write_timeout
        self._key = key
        self._session_details_enabled = session_details_enabled
        self._retry_backoff = retry_backoff
        self._batch_max = batch_max
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._gaps: dict[str, _Gap] = {}
        self._inflight_events = 0
        self._inflight_gaps: dict[str, _Gap] = {}
        self._dropped_since_start = 0
        self._last_flush_at: datetime | None = None
        self._last_flush_ok: bool | None = None
        self._last_drop_log: float | None = None
        self._last_failure_log: float | None = None
        # #3035: pending visitor keys per (endpoint, UTC day).
        self._visitor_flush_min_keys = max(1, visitor_flush_min_keys)
        self._visitors: dict[tuple[UUID, date], dict[str, VisitorRow]] = {}
        self._visitors_pending = 0
        self._inflight_visitors = 0
        self._visitors_dropped = 0
        self._last_visitor_drop_log: float | None = None
        self._last_visitor_failure_log: float | None = None

    # ------------------------------------------------------------------
    # Request path
    # ------------------------------------------------------------------

    def offer(self, event: UsageEventInput) -> None:
        """Queue one event; drop and count it when that is not possible. Never raises."""
        try:
            try:
                built = build_usage_event_row(
                    event, key=self._key, session_details_enabled=self._session_details_enabled
                )
            except Exception as exc:  # noqa: BLE001 - the request path must not fail
                now = utcnow()
                self._record_drop("write_failed", 1, now, now)
                self._log_drop("public_mcp_telemetry_failed", error_type=type(exc).__name__)
                return
            try:
                self._queue.put_nowait(built)
            except asyncio.QueueFull:
                now = utcnow()
                self._record_drop("queue_full", 1, now, now)
                self._log_drop("public_mcp_telemetry_dropped", reason="queue_full")
                return
            # #3035: only an event that was queued adds its visitor, so the
            # visitors and the answered searches share one base.
            if built.visitor is not None:
                self._add_visitor(built.visitor)
        except Exception:  # noqa: BLE001, S110 - counting itself failed; nothing to add
            pass

    def _add_visitor(self, visitor: VisitorRow) -> None:
        # The day's last group is due (or written): one decision, the same
        # one ``_flush_visitors`` makes, so no group of a finished day can
        # start after that day's last group was released.
        if visitor.day < _last_due_day():
            self._drop_visitor("late")
            return
        group_key = (visitor.endpoint_id, visitor.day)
        group = self._visitors.get(group_key)
        if group is not None and visitor.visitor_key in group:
            return
        if self._visitors_pending >= VISITOR_PENDING_MAX:
            self._drop_visitor("pending_full")
            return
        if group is None:
            group = self._visitors[group_key] = {}
        group[visitor.visitor_key] = visitor
        self._visitors_pending += 1

    def _drop_visitor(self, reason: str) -> None:
        self._visitors_dropped += 1
        now = time.monotonic()
        last = self._last_visitor_drop_log
        if last is None or now - last >= _LOG_EVERY_SECONDS:
            self._last_visitor_drop_log = now
            logger.warning(
                "public_mcp_visitors_dropped",
                reason=reason,
                count=self._visitors_dropped,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the drainer task on the running loop."""
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="public_mcp_usage_writer")

    async def stop(self, timeout: float = 5.0) -> None:
        """Flush what the budget allows; log what is lost (count only)."""
        task, self._task = self._task, None
        self._stopping = True
        if task is not None:
            try:
                self._queue.put_nowait(None)  # wake a drainer waiting on an empty queue
            except asyncio.QueueFull:
                pass  # a full queue keeps the drainer busy; it checks the flag
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception as exc:  # noqa: BLE001 - shutdown must finish
                logger.warning("public_mcp_telemetry_writer_failed", error_type=type(exc).__name__)
        lost = self._inflight_events + self._drain_queue()
        lost += sum(g.count for g in self._inflight_gaps.values())
        lost += sum(g.count for g in self._gaps.values())
        lost_visitors = self._visitors_pending + self._inflight_visitors
        if lost or lost_visitors:
            fields: dict[str, int] = {"count": lost}
            if lost_visitors:
                fields["visitors"] = lost_visitors
            logger.warning("public_mcp_telemetry_lost_at_shutdown", **fields)

    def health(self) -> WriterHealth:
        """Queue depth, drops and the last flush, for the owner health route."""
        pending = sum(g.count for g in self._gaps.values())
        pending += sum(g.count for g in self._inflight_gaps.values())
        return WriterHealth(
            enabled=True,
            queue_depth=self._queue.qsize(),
            queue_capacity=self._capacity,
            dropped_since_start=self._dropped_since_start,
            last_flush_at=self._last_flush_at,
            last_flush_ok=self._last_flush_ok,
            pending_gap=pending,
        )

    # ------------------------------------------------------------------
    # Drainer
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            if self._stopping and self._queue.empty():
                if self._gaps:
                    await self._flush([])
                await self._flush_visitors(final=True)
                return
            batch = await self._collect()
            if batch or self._gaps:
                ok = await self._flush(batch)
                if not ok and self._stopping:
                    # The database is not answering: what is left becomes one
                    # shutdown gap, written if the database answers once more.
                    now = utcnow()
                    left = self._drain_queue()
                    if left:
                        self._record_drop("shutdown", left, now, now)
                    await self._flush([])
                    await self._flush_visitors(final=True)
                    return
            # #3035: every pass, whether or not it wrote events.
            await self._flush_visitors(final=False)

    async def _collect(self) -> list[BuiltUsageEvent]:
        """Up to ``batch_max`` events, waiting at most two flush intervals."""
        batch: list[BuiltUsageEvent] = []
        if self._stopping and self._queue.empty():
            return batch
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
        except TimeoutError:
            return batch
        if first is not None:
            batch.append(first)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._flush_interval
        while len(batch) < self._batch_max:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                remaining = deadline - loop.time()
                if self._stopping or remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                except TimeoutError:
                    break
            if item is not None:
                batch.append(item)
        return batch

    async def _flush(self, batch: list[BuiltUsageEvent]) -> bool:
        """Write pending gaps + ``batch`` in one transaction; retry once, then drop."""
        gaps, self._gaps = self._gaps, {}
        self._inflight_gaps = gaps
        self._inflight_events = len(batch)
        rows = [
            build_gap_row(
                dropped_count=g.count, started_at=g.started_at, ended_at=g.ended_at, reason=reason
            )
            for reason, g in gaps.items()
        ]
        rows.extend(b.row for b in batch)
        doc_rows = _doc_key_rows(batch)
        error: Exception | None = None
        for attempt in range(2):
            try:
                await asyncio.wait_for(self._write(rows, doc_rows), timeout=self._write_timeout)
            except Exception as exc:  # noqa: BLE001 - includes the write timeout
                error = exc
                if attempt == 0:
                    await asyncio.sleep(min(self._retry_backoff, _RETRY_BACKOFF_CAP_SECONDS))
                continue
            self._inflight_gaps = {}
            self._inflight_events = 0
            self._last_flush_at = utcnow()
            self._last_flush_ok = True
            return True

        # Dropped: keep the gaps for the next flush and add this batch to them.
        self._inflight_gaps = {}
        self._inflight_events = 0
        for reason, g in gaps.items():
            self._merge_gap(reason, g)
        if batch:
            times = [b.row["occurred_at"] for b in batch]
            reason = "shutdown" if self._stopping else "write_failed"
            self._record_drop(reason, len(batch), min(times), max(times))
        self._last_flush_ok = False
        now = time.monotonic()
        last = self._last_failure_log
        if last is None or now - last >= _LOG_EVERY_SECONDS or self._stopping:
            self._last_failure_log = now
            logger.warning(
                "public_mcp_telemetry_write_failed",
                error_type=type(error).__name__,
                dropped=len(batch),
            )
        return False

    async def _write(self, rows: list[dict[str, Any]], doc_rows: list[dict[str, Any]]) -> None:
        async with self._session_factory() as session:
            if rows:
                # A retry after a commit that landed but timed out re-sends
                # the same event_ids: those rows are already stored, not lost.
                await session.execute(_EVENT_INSERT, rows)
            if doc_rows:
                try:
                    # Keys of a resource deleted since the call are skipped;
                    # one deleted while this runs fails the keys, not the events.
                    async with session.begin_nested():
                        await session.execute(_DOC_KEY_INSERT, doc_rows)
                except IntegrityError:
                    logger.info("public_mcp_doc_keys_skipped", count=len(doc_rows))
            await session.commit()

    # ------------------------------------------------------------------
    # Daily visitors (#3035)
    # ------------------------------------------------------------------

    async def _flush_visitors(self, *, final: bool) -> None:
        """Write the groups the crowd rule releases, in a transaction of their own.

        Released: every group with at least ``visitor_flush_min_keys``
        distinct keys, every group whose UTC day ended at least
        ``VISITOR_DAY_GRACE`` ago, and, when ``final``, every group. The rest
        waits.
        """
        if not self._visitors:
            return
        due_before = _last_due_day()
        ready = [
            group_key
            for group_key, group in self._visitors.items()
            if final or len(group) >= self._visitor_flush_min_keys or group_key[1] < due_before
        ]
        if not ready:
            return
        visitors: list[VisitorRow] = []
        for group_key in ready:
            visitors.extend(self._visitors.pop(group_key).values())
        # Moved from pending to in flight with no await between: a write that
        # ``stop`` cancels leaves them in flight, so the shutdown line counts
        # them. Only the two normal exits below clear the in-flight count.
        self._visitors_pending -= len(visitors)
        self._inflight_visitors = len(visitors)
        # One order for every process: no deadlock on the primary key.
        visitors.sort(key=lambda v: (v.endpoint_id, v.day, v.visitor_key))
        rows = [
            {
                "endpoint_id": v.endpoint_id,
                "day": v.day,
                "visitor_key": v.visitor_key,
                "workspace_id": v.workspace_id,
            }
            for v in visitors
        ]
        error: Exception | None = None
        for attempt in range(2):
            try:
                await asyncio.wait_for(self._write_visitors(rows), timeout=self._write_timeout)
            except Exception as exc:  # noqa: BLE001 - includes the write timeout
                error = exc
                if attempt == 0:
                    await asyncio.sleep(min(self._retry_backoff, _RETRY_BACKOFF_CAP_SECONDS))
                continue
            self._inflight_visitors = 0
            return
        self._inflight_visitors = 0
        self._visitors_dropped += len(rows)
        now = time.monotonic()
        last = self._last_visitor_failure_log
        if last is None or now - last >= _LOG_EVERY_SECONDS or self._stopping:
            self._last_visitor_failure_log = now
            logger.warning(
                "public_mcp_visitors_write_failed",
                error_type=type(error).__name__,
                dropped=len(rows),
            )

    async def _write_visitors(self, rows: list[dict[str, Any]]) -> None:
        async with self._session_factory() as session:
            await session.execute(_VISITOR_INSERT, rows)
            await session.commit()

    # ------------------------------------------------------------------
    # Gap bookkeeping
    # ------------------------------------------------------------------

    def _record_drop(self, reason: str, count: int, started: datetime, ended: datetime) -> None:
        self._dropped_since_start += count
        self._merge_gap(reason, _Gap(count, started, ended))

    def _merge_gap(self, reason: str, gap: _Gap) -> None:
        current = self._gaps.get(reason)
        if current is None:
            self._gaps[reason] = _Gap(gap.count, gap.started_at, gap.ended_at)
            return
        current.count += gap.count
        current.started_at = min(current.started_at, gap.started_at)
        current.ended_at = max(current.ended_at, gap.ended_at)

    def _drain_queue(self) -> int:
        """Empty the queue; the number of events it held."""
        count = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return count
            if item is not None:
                count += 1

    def _log_drop(self, event: str, **fields: Any) -> None:
        now = time.monotonic()
        last = self._last_drop_log
        if last is not None and now - last < _LOG_EVERY_SECONDS:
            return
        self._last_drop_log = now
        logger.warning(event, dropped_since_start=self._dropped_since_start, **fields)


def _last_due_day() -> date:
    """Groups of a day before this one are due: that day ended ``VISITOR_DAY_GRACE`` ago."""
    return (utcnow() - VISITOR_DAY_GRACE).date()


_EVENT_INSERT = pg_insert(PublicMcpUsageEvent).on_conflict_do_nothing(index_elements=["event_id"])
_VISITOR_INSERT = pg_insert(PublicMcpDailyVisitor).on_conflict_do_nothing(
    index_elements=["endpoint_id", "day", "visitor_key"]
)

_DOC_KEY_PK = cast(bindparam("resource_pk"), Uuid)
_DOC_KEY_INSERT = (
    # Core statement on the table: ORM bulk INSERT cannot take a SELECT.
    pg_insert(PublicMcpDocKey.__table__)
    .from_select(
        ["resource_pk", "doc_key", "resource_doc_id"],
        select(
            _DOC_KEY_PK,
            cast(bindparam("doc_key"), CHAR(32)),
            cast(bindparam("resource_doc_id"), String(255)),
        ).where(exists().where(Resource.__table__.c.id == _DOC_KEY_PK)),
    )
    .on_conflict_do_nothing()
)


def _doc_key_rows(batch: list[BuiltUsageEvent]) -> list[dict[str, Any]]:
    """The batch's (resource_pk, doc_key) → doc id pairs, once each, sorted.

    Sorted so that two processes inserting the same keys take the unique
    index entries in the same order and cannot deadlock (#3035).
    """
    seen: dict[tuple[Any, str], str] = {}
    for built in batch:
        if built.resource_pk is None:
            continue
        for key, doc_id in built.doc_keys:
            seen.setdefault((built.resource_pk, key), doc_id)
    return [
        {"resource_pk": pk, "doc_key": key, "resource_doc_id": seen[(pk, key)]}
        for pk, key in sorted(seen)
    ]


# ============================================================================
# The process's writer
# ============================================================================

_writer: PublicMcpUsageWriter | None = None


def get_usage_writer() -> PublicMcpUsageWriter | None:
    """This process's writer, or ``None`` when telemetry is off or not started."""
    return _writer


def start_usage_writer(
    *, session_factory: Callable[[], Any] | None = None
) -> PublicMcpUsageWriter | None:
    """Start the process's writer when ``MCP_PUBLIC_TELEMETRY_ENABLED`` is on.

    Args:
        session_factory: Session factory (the application's by default).

    Returns:
        The running writer, or ``None`` when telemetry is off.
    """
    global _writer
    settings = get_settings()
    if not settings.mcp_public_telemetry_enabled:
        return None
    if _writer is not None:
        return _writer
    if session_factory is None:
        from db.base import _get_session_factory

        session_factory = _get_session_factory()
    _writer = PublicMcpUsageWriter(
        session_factory=session_factory,
        queue_size=settings.mcp_public_telemetry_queue_size,
        flush_interval=settings.mcp_public_telemetry_flush_interval_seconds,
        write_timeout=settings.mcp_public_telemetry_write_timeout_seconds,
        key=settings.audit_hmac_key,
        session_details_enabled=settings.mcp_public_telemetry_session_details_enabled,
    )
    _writer.start()
    return _writer


async def stop_usage_writer(timeout: float = 5.0) -> None:
    """Stop the process's writer (API shutdown)."""
    global _writer
    writer, _writer = _writer, None
    if writer is not None:
        await writer.stop(timeout=timeout)
