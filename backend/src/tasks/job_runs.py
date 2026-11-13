"""Record every scheduled job's runs in ``scheduled_job_status`` (Issue #2768).

One listener on the leader's scheduler (attached by
:func:`tasks.register_scheduled_jobs` after every ``schedule_*`` call) covers
every registered job without a change to its task body:

- ``EVENT_SCHEDULER_STARTED``: the registration snapshot (ids, names, trigger
  kinds, expected intervals) is written in one statement.
- ``EVENT_JOB_SUBMITTED``: the run's start (``last_started_at``).
- ``EVENT_JOB_EXECUTED``: ``ok`` with the counts the job returned, or
  ``failed`` when it returned :func:`job_failed` (a job that handles its own
  failure and returns normally).
- ``EVENT_JOB_ERROR``: ``failed`` with the exception CLASS name. That includes
  a run cancelled when the scheduler shuts down (``CancelledError``): the run
  did not complete.
- ``EVENT_JOB_MISSED``: ``missed`` (outcome only; success fields unchanged).

``EVENT_JOB_MAX_INSTANCES`` is not an outcome: a slow run of a 30 s job would
flip the row on every tick. A hung run shows as overdue instead.

What reaches the table (it is published to operators by the status route):
only job ids of the registration snapshot that match
:data:`services.operations.job_status.JOB_ID_RE`; exception class names
through :func:`sanitize_error_class` (never the message or the traceback);
and a job's returned mapping through :func:`sanitize_counts`. **A job's
returned mapping is published to operators: its keys are code literals,
never data, and its values are integer counts.**

The listener is synchronous and never raises (APScheduler also swallows
listener errors). It only queues work: ONE consumer task, started lazily on
the loop, drains a bounded queue that coalesces per (kind, job), holding at
most one database connection at a time. A write failure is logged with its
class name (rate-limited) and never reaches the job. The registration write
is retried before every later write until it succeeded once.

:func:`drain_job_run_recorders` is awaited by ``SchedulerLeader.stop`` right
after the scheduler shutdown: it lets the cancelled runs report, then flushes
the queue within a bound. After that the recorder drops every event and
creates no task.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    EVENT_SCHEDULER_STARTED,
)

from models.operations_status import ERROR_CLASS_MAX_LENGTH, JOB_COUNTS_MAX_BYTES
from services.operations import job_status
from services.operations.job_status import JobRegistration, JobRunUpdate
from utils.logger import get_logger

logger = get_logger(__name__)

EVENT_MASK = (
    EVENT_SCHEDULER_STARTED
    | EVENT_JOB_SUBMITTED
    | EVENT_JOB_EXECUTED
    | EVENT_JOB_ERROR
    | EVENT_JOB_MISSED
)

QUEUE_MAX = 200
WRITE_TIMEOUT_SECONDS = 10.0
DRAIN_TIMEOUT_SECONDS = 5.0
_LOG_EVERY_SECONDS = 60.0
_IN_FLIGHT_POLL_SECONDS = 0.01

COUNTS_MAX_ENTRIES = 32
COUNT_VALUE_MAX = 2**53 - 1
_COUNT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# A key carrying an un-hyphenated id (uuid hex, a hash) is data, not a label.
_HEX_RUN_RE = re.compile(r"[0-9a-f]{32,}")
_ERROR_CLASS_RE = re.compile(rf"^[A-Za-z_][A-Za-z0-9_]{{0,{ERROR_CLASS_MAX_LENGTH - 1}}}$")
_FALLBACK_ERROR_CLASS = "Exception"

# Kinds of queued writes; the key is (kind, job_id).
_REGISTRATIONS = "registrations"
_STARTED = "started"
_RUN = "run"


def sanitize_error_class(name: Any) -> str:
    """An exception class name safe to publish, else ``"Exception"``."""
    if isinstance(name, str) and _ERROR_CLASS_RE.match(name):
        return name
    return _FALLBACK_ERROR_CLASS


def sanitize_counts(value: Any) -> dict[str, int] | None:
    """The integer counts of a job's returned mapping, safe to publish.

    Keeps snake_case keys (no run of 32+ hex characters) with ``int`` values
    (``bool`` → 0/1; anything else, nested mappings included, is dropped),
    clipped to ``[0, 2**53 - 1]``, at most 32 entries in key order, trimmed
    until the JSON fits the column's size cap. ``None`` when nothing is left.
    """
    if not isinstance(value, Mapping):
        return None
    kept: dict[str, int] = {}
    for key in sorted(k for k in value if isinstance(k, str)):
        if not _COUNT_KEY_RE.match(key) or _HEX_RUN_RE.search(key):
            continue
        raw = value[key]
        if not isinstance(raw, int):
            continue
        kept[key] = min(max(int(raw), 0), COUNT_VALUE_MAX)
        if len(kept) >= COUNTS_MAX_ENTRIES:
            break
    while kept and len(json.dumps(kept).encode()) > JOB_COUNTS_MAX_BYTES:
        kept.pop(next(reversed(kept)))
    return kept or None


class JobFailed:
    """What a job returns when it handled its own failure (logged, rolled back).

    The run is then recorded as ``failed`` with the exception's class name.
    Built only from an exception type (use :func:`job_failed`): no caller can
    put a free-form string, such as an exception message, in it.
    """

    __slots__ = ("_error_class",)

    def __init__(self, exc_type: type[BaseException]) -> None:
        if not (isinstance(exc_type, type) and issubclass(exc_type, BaseException)):
            raise TypeError("JobFailed takes an exception type; use job_failed(exc)")
        self._error_class = sanitize_error_class(exc_type.__name__)

    @property
    def error_class(self) -> str:
        """The sanitised exception class name."""
        return self._error_class

    def __eq__(self, other: object) -> bool:
        return isinstance(other, JobFailed) and other._error_class == self._error_class

    def __hash__(self) -> int:
        return hash(("JobFailed", self._error_class))

    def __repr__(self) -> str:
        return f"JobFailed({self._error_class})"


def job_failed(exc: BaseException) -> JobFailed:
    """The return value of a job whose top-level ``except`` caught ``exc``."""
    return JobFailed(type(exc))


# What a scheduled job returns: integer counts, a handled failure, or nothing.
JobResult = Mapping[str, int] | JobFailed | None


_last_warned: dict[tuple[str, str], float] = {}


def _warn(event: str, error_class: str, **fields: Any) -> None:
    """``logger.warning`` at most once a minute per (event, error class)."""
    key = (event, error_class)
    now = time.monotonic()
    last = _last_warned.get(key)
    if last is not None and now - last < _LOG_EVERY_SECONDS:
        return
    _last_warned[key] = now
    logger.warning(event, error_class=error_class, **fields)


def _now() -> datetime:
    return datetime.now(UTC)


def _skipped_only(counts: Mapping[str, int] | None) -> bool:
    """A run that only reports ``skipped`` (a single-flight skip) did no work."""
    return counts is not None and set(counts) == {"skipped"} and counts["skipped"] > 0


class JobRunRecorder:
    """The scheduler listener plus its write queue (one per scheduler).

    Args:
        scheduler: The scheduler whose jobs are recorded.
        writer: The write functions; defaults to
            :mod:`services.operations.job_status` (tests pass a fake).
    """

    def __init__(self, scheduler: Any, *, writer: Any = None) -> None:
        self._scheduler = scheduler
        self._writer = writer if writer is not None else job_status
        self._known: dict[str, JobRegistration] = {}
        self._registrations: list[JobRegistration] | None = None
        self.registrations_persisted = False
        self._started: dict[str, tuple[datetime, float]] = {}
        self._in_flight: Counter[str] = Counter()
        self._pending: dict[tuple[str, str], Any] = {}
        self._queue: asyncio.Queue[tuple[str, str]] | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def closed(self) -> bool:
        """Drained: every later event is dropped."""
        return self._closing

    # ------------------------------------------------------------------
    # Listener (synchronous, on the loop thread; never raises)
    # ------------------------------------------------------------------

    def __call__(self, event: Any) -> None:
        try:
            self._handle(event)
        except Exception as exc:  # noqa: BLE001 - a listener must never fail
            _warn("job_run_recorder_failed", type(exc).__name__)

    def _handle(self, event: Any) -> None:
        if self._closing:
            return
        code = event.code
        now = _now()
        if code == EVENT_SCHEDULER_STARTED:
            regs = job_status.job_registrations(self._scheduler, now)
            self._known = {r.job_id: r for r in regs}
            self._registrations = regs
            self.registrations_persisted = False
            self._enqueue((_REGISTRATIONS, ""), regs)
            return
        job_id = getattr(event, "job_id", None)
        if not isinstance(job_id, str):
            return
        if code == EVENT_JOB_SUBMITTED:
            self._in_flight[job_id] += 1
            self._started[job_id] = (now, time.monotonic())
            if self._is_known(job_id):
                self._enqueue((_STARTED, job_id), now)
            return
        if code not in (EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED):
            return
        if self._in_flight[job_id] > 0:
            self._in_flight[job_id] -= 1
        if self._in_flight[job_id] <= 0:
            del self._in_flight[job_id]
        started = self._started.pop(job_id, None)
        if not self._is_known(job_id):
            return
        self._enqueue((_RUN, job_id), self._run_update(event, job_id, now, started))

    def _is_known(self, job_id: str) -> bool:
        if job_id in self._known:
            return True
        _warn("job_run_record_dropped", "UnregisteredJob")
        return False

    @staticmethod
    def _run_update(
        event: Any, job_id: str, now: datetime, started: tuple[datetime, float] | None
    ) -> JobRunUpdate:
        if event.code == EVENT_JOB_MISSED:
            return JobRunUpdate(job_id=job_id, outcome="missed", outcome_at=now)
        started_at = started[0] if started else None
        duration_ms = (
            max(0, int((time.monotonic() - started[1]) * 1000)) if started is not None else None
        )
        base = {
            "job_id": job_id,
            "outcome_at": now,
            "started_at": started_at,
            "finished_at": now,
            "duration_ms": duration_ms,
        }
        if event.code == EVENT_JOB_ERROR:
            error_class = sanitize_error_class(type(event.exception).__name__)
            return JobRunUpdate(outcome="failed", error_class=error_class, failures_added=1, **base)
        retval = event.retval
        if isinstance(retval, JobFailed):
            return JobRunUpdate(
                outcome="failed", error_class=retval.error_class, failures_added=1, **base
            )
        counts = sanitize_counts(retval)
        if _skipped_only(counts):
            return JobRunUpdate(outcome="ok", counts=counts, **base)
        return JobRunUpdate(
            outcome="ok", counts=counts, success_at=now, reset_failures=True, **base
        )

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def _enqueue(self, key: tuple[str, str], item: Any) -> None:
        if self._closing:
            return
        queue = self._ensure_consumer()
        if key in self._pending:
            current = self._pending[key]
            self._pending[key] = current.then(item) if key[0] == _RUN else item
            return
        try:
            queue.put_nowait(key)
        except asyncio.QueueFull:
            _warn("job_run_record_dropped", "QueueFull")
            return
        self._pending[key] = item

    def _ensure_consumer(self) -> asyncio.Queue[tuple[str, str]]:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if self._consumer is None or self._consumer.done():
            loop = asyncio.get_running_loop()
            self._consumer = loop.create_task(self._consume(), name="job_run_recorder")
        return self._queue

    async def _consume(self) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            key = await queue.get()
            try:
                item = self._pending.pop(key, None)
                if item is not None:
                    await self._write(key, item)
            finally:
                queue.task_done()

    async def _write(self, key: tuple[str, str], item: Any) -> None:
        kind, job_id = key
        if kind == _REGISTRATIONS:
            await self._write_registrations(item)
            return
        if not self.registrations_persisted and self._registrations is not None:
            await self._write_registrations(self._registrations)
        try:
            if kind == _STARTED:
                await asyncio.wait_for(
                    self._writer.record_job_started(job_id, item), WRITE_TIMEOUT_SECONDS
                )
            else:
                await asyncio.wait_for(self._writer.record_job_run(item), WRITE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a lost record never fails a job
            _warn("job_status_record_failed", type(exc).__name__, kind=kind)

    async def _write_registrations(self, regs: list[JobRegistration]) -> None:
        try:
            await asyncio.wait_for(self._writer.record_registrations(regs), WRITE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retried before the next write
            _warn("job_status_record_failed", type(exc).__name__, kind=_REGISTRATIONS)
            return
        if regs is self._registrations:
            self.registrations_persisted = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def drain(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> None:
        """Flush within ``timeout`` and close: later events are dropped.

        Called right after the scheduler shutdown. The runs that shutdown
        cancelled report their ``CancelledError`` a few loop iterations later,
        so it first waits (within the budget) until no submitted run is left
        without an outcome, then for the queue to empty.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.0)
        while self._in_flight and loop.time() < deadline:
            await asyncio.sleep(_IN_FLIGHT_POLL_SECONDS)
        self._closing = True
        _forget(self)
        consumer, self._consumer = self._consumer, None
        if consumer is None or consumer.done():
            return
        queue = self._queue
        if queue is not None:
            try:
                await asyncio.wait_for(queue.join(), max(deadline - loop.time(), 0.0))
            except TimeoutError:
                logger.warning("job_run_recorder_drain_timeout", pending=len(self._pending))
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


# One recorder per scheduler object: APScheduler does not de-duplicate
# listeners, and a scheduler whose start failed is reused by the next leader
# attempt (``shutdown_scheduler`` only forgets a running one).
_attached: weakref.WeakKeyDictionary[Any, JobRunRecorder] = weakref.WeakKeyDictionary()
# Recorders not yet drained (strong references: the scheduler global is
# dropped at shutdown, before the drain).
_live: set[JobRunRecorder] = set()


def _forget(recorder: JobRunRecorder) -> None:
    _live.discard(recorder)
    scheduler = recorder._scheduler
    try:
        if _attached.get(scheduler) is recorder:
            del _attached[scheduler]
    except TypeError:  # not weak-referenceable; never attached
        pass


def attach_job_run_recorder(scheduler: Any) -> JobRunRecorder:
    """Attach the run recorder to ``scheduler`` once (a second call is a no-op).

    Args:
        scheduler: The leader's (not yet started) scheduler.

    Returns:
        The scheduler's recorder.
    """
    existing = _attached.get(scheduler)
    if existing is not None and not existing.closed:
        return existing
    recorder = JobRunRecorder(scheduler)
    scheduler.add_listener(recorder, EVENT_MASK)
    _attached[scheduler] = recorder
    _live.add(recorder)
    return recorder


async def drain_job_run_recorders(timeout: float = DRAIN_TIMEOUT_SECONDS) -> None:
    """Drain and close every live recorder (bounded by ``timeout``). Never raises."""
    recorders = list(_live)
    if not recorders:
        return
    results = await asyncio.gather(*(r.drain(timeout) for r in recorders), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            _warn("job_run_recorder_drain_failed", type(result).__name__)


__all__ = [
    "EVENT_MASK",
    "JobFailed",
    "JobResult",
    "JobRunRecorder",
    "attach_job_run_recorder",
    "drain_job_run_recorders",
    "job_failed",
    "sanitize_counts",
    "sanitize_error_class",
]
