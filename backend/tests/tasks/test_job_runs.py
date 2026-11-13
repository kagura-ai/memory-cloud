"""The scheduled job run recorder (Issue #2768).

No database: the recorder's writer is replaced by an in-memory fake that keeps
what it was asked to write. The real-scheduler tests start an
``AsyncIOScheduler`` on the test loop.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    EVENT_SCHEDULER_STARTED,
    JobExecutionEvent,
    JobSubmissionEvent,
    SchedulerEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import Settings
from models.operations_status import JOB_COUNTS_MAX_BYTES
from services.operations import job_status
from services.operations.job_status import (
    JOB_ID_RE,
    JobRegistration,
    JobRunUpdate,
    expected_interval_seconds,
    job_registrations,
)
from tasks import job_runs
from tasks.job_runs import (
    EVENT_MASK,
    JobFailed,
    JobRunRecorder,
    attach_job_run_recorder,
    job_failed,
    sanitize_counts,
    sanitize_error_class,
)

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


class FakeWriter:
    """Records every write; can fail the first ``fail_*`` calls of a kind."""

    def __init__(self, *, fail_registrations: int = 0, fail_runs: int = 0) -> None:
        self.registrations: list[list[JobRegistration]] = []
        self.started: list[tuple[str, datetime]] = []
        self.runs: list[JobRunUpdate] = []
        self.calls: list[str] = []
        self._fail_registrations = fail_registrations
        self._fail_runs = fail_runs

    async def record_registrations(self, regs: list[JobRegistration]) -> None:
        self.calls.append("registrations")
        if self._fail_registrations:
            self._fail_registrations -= 1
            raise ConnectionError("database unavailable")
        self.registrations.append(list(regs))

    async def record_job_started(self, job_id: str, at: datetime) -> None:
        self.calls.append(f"started:{job_id}")
        self.started.append((job_id, at))

    async def record_job_run(self, run: JobRunUpdate) -> None:
        self.calls.append(f"run:{run.job_id}")
        if self._fail_runs:
            self._fail_runs -= 1
            raise ConnectionError("database unavailable at 10.0.0.1 for ws-secret")
        self.runs.append(run)


def _scheduler_with(*job_ids: str) -> MagicMock:
    scheduler = MagicMock(name="scheduler")
    jobs = []
    for job_id in job_ids:
        job = MagicMock()
        job.id = job_id
        job.name = job_id.replace("_", " ")
        job.trigger = IntervalTrigger(seconds=60)
        jobs.append(job)
    scheduler.get_jobs.return_value = jobs
    return scheduler


def _started() -> SchedulerEvent:
    return SchedulerEvent(EVENT_SCHEDULER_STARTED)


def _submitted(job_id: str) -> JobSubmissionEvent:
    return JobSubmissionEvent(EVENT_JOB_SUBMITTED, job_id, "default", [NOW])


def _executed(job_id: str, retval: Any = None) -> JobExecutionEvent:
    return JobExecutionEvent(EVENT_JOB_EXECUTED, job_id, "default", NOW, retval=retval)


def _error(job_id: str, exc: BaseException) -> JobExecutionEvent:
    return JobExecutionEvent(
        EVENT_JOB_ERROR, job_id, "default", NOW, exception=exc, traceback="Traceback..."
    )


def _missed(job_id: str) -> JobExecutionEvent:
    return JobExecutionEvent(EVENT_JOB_MISSED, job_id, "default", NOW)


async def _settle(recorder: JobRunRecorder) -> None:
    """Wait until the queue is empty and the consumer is idle."""
    for _ in range(200):
        queue = recorder._queue
        if queue is None or (queue.empty() and queue._unfinished_tasks == 0):  # type: ignore[attr-defined]
            return
        await asyncio.sleep(0.005)
    raise AssertionError("recorder did not settle")


@pytest.fixture
def writer() -> FakeWriter:
    return FakeWriter()


@pytest.fixture
async def recorder(writer: FakeWriter):
    rec = JobRunRecorder(_scheduler_with("alpha_job", "beta_job"), writer=writer)
    yield rec
    await rec.drain(timeout=1)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestExpectedInterval:
    @pytest.mark.parametrize(
        ("trigger", "expected"),
        [
            (IntervalTrigger(seconds=30), 30),
            (IntervalTrigger(hours=1), 3600),
            (CronTrigger(second=30, timezone="UTC"), 60),
            (CronTrigger(minute=35, timezone="UTC"), 3600),
            (CronTrigger(hour=4, minute=0, timezone="UTC"), 86400),
        ],
    )
    def test_interval_and_cron(self, trigger: Any, expected: int) -> None:
        assert expected_interval_seconds(trigger, NOW) == expected

    def test_monthly_cron_takes_the_longest_gap(self) -> None:
        seconds = expected_interval_seconds(
            CronTrigger(day=1, hour=4, minute=15, timezone="UTC"), NOW
        )
        assert seconds is not None
        assert 31 * 86400 >= seconds >= 28 * 86400

    def test_date_trigger_has_no_interval(self) -> None:
        assert expected_interval_seconds(DateTrigger(NOW + timedelta(days=1)), NOW) is None


class TestSanitizers:
    def test_error_class_keeps_a_class_name(self) -> None:
        assert sanitize_error_class("OperationalError") == "OperationalError"

    @pytest.mark.parametrize(
        "name",
        ["", "has space", "user@example.com", "x" * 101, "1Leading", None, "ws-1234"],
    )
    def test_error_class_falls_back(self, name: Any) -> None:
        assert sanitize_error_class(name) == "Exception"

    def test_counts_keep_int_values_under_snake_keys(self) -> None:
        assert sanitize_counts({"deleted": 3, "batches": 1, "budget_exhausted": False}) == {
            "batches": 1,
            "budget_exhausted": 0,
            "deleted": 3,
        }

    def test_counts_drop_data_shaped_keys_and_non_ints(self) -> None:
        hex_id = uuid.uuid4().hex
        counts = sanitize_counts(
            {
                f"ws_{hex_id}": 1,
                "Deleted": 2,
                "user@example.com": 3,
                "by_reason": {"stale": 4},
                "ratio": 0.5,
                "label": "text",
                "ok": 5,
            }
        )
        assert counts == {"ok": 5}

    def test_counts_clip_values(self) -> None:
        assert sanitize_counts({"released_bytes": 2**60, "negative": -4}) == {
            "negative": 0,
            "released_bytes": 2**53 - 1,
        }

    def test_counts_cap_entries_and_size(self) -> None:
        many = {f"k{i:02d}_" + "x" * 58: 2**53 - 1 for i in range(40)}
        counts = sanitize_counts(many)
        assert counts is not None
        assert len(counts) <= 32
        import json

        assert len(json.dumps(counts).encode()) <= JOB_COUNTS_MAX_BYTES

    @pytest.mark.parametrize("value", [None, 3, "text", [], {}, {"x": "y"}])
    def test_counts_none_when_nothing_is_left(self, value: Any) -> None:
        assert sanitize_counts(value) is None


class TestJobFailed:
    def test_built_from_the_exception_type(self) -> None:
        failed = job_failed(ValueError("user 42 at ws-secret"))
        assert failed.error_class == "ValueError"
        assert "ws-secret" not in repr(failed)

    def test_no_free_form_string(self) -> None:
        with pytest.raises(TypeError):
            JobFailed("user 42 at ws-secret")  # type: ignore[arg-type]

    def test_dynamic_type_name_falls_back(self) -> None:
        weird = type("Bad name with data", (Exception,), {})
        assert job_failed(weird()).error_class == "Exception"


# ---------------------------------------------------------------------------
# Every registered job is covered
# ---------------------------------------------------------------------------


def _enable_every_conditional_job(monkeypatch: pytest.MonkeyPatch, *, sleep: bool) -> None:
    monkeypatch.setenv("SLEEP_ENABLED", "true" if sleep else "false")
    monkeypatch.setenv("DEFERRED_POINT_DRAIN_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_SECONDS", "300")
    monkeypatch.setattr(
        "tasks.bm25_drift_tasks.get_settings",
        lambda: Settings(_env_file=None, bm25_drift_cron_enabled=True),
    )
    monkeypatch.setattr(
        "tasks.operations_probe_tasks.get_settings",
        lambda: Settings(_env_file=None, operations_probe_interval_seconds=300),
    )


@pytest.mark.parametrize("sleep", [True, False])
def test_every_registered_job_has_a_registration(
    monkeypatch: pytest.MonkeyPatch, sleep: bool
) -> None:
    """Acceptance: every job ``register_scheduled_jobs`` adds is recorded.

    Run with Sleep on and off: consolidation is registered only when Sleep is
    off, sleep_maintenance only when it is on.
    """
    import tasks

    _enable_every_conditional_job(monkeypatch, sleep=sleep)
    scheduler = AsyncIOScheduler(timezone="UTC")
    tasks.register_scheduled_jobs(scheduler)

    regs = job_registrations(scheduler, NOW)
    ids = {r.job_id for r in regs}
    assert ids == {job.id for job in scheduler.get_jobs()}
    assert len(ids) >= 20
    assert ("sleep_maintenance" in ids) is sleep
    assert ("consolidation" in ids) is (not sleep)
    assert {"operations_lane_snapshot", "operations_probe"} <= ids
    for reg in regs:
        assert JOB_ID_RE.match(reg.job_id), reg.job_id
        assert reg.expected_interval_seconds is not None, reg.job_id
        assert reg.trigger_kind in ("interval", "cron"), reg.job_id
        assert reg.name


@pytest.mark.asyncio
async def test_a_started_scheduler_writes_every_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real attach path: the STARTED event writes the whole registration set."""
    import tasks

    _enable_every_conditional_job(monkeypatch, sleep=True)
    fake = FakeWriter()
    monkeypatch.setattr(job_status, "record_registrations", fake.record_registrations)
    scheduler = AsyncIOScheduler(timezone="UTC")
    tasks.register_scheduled_jobs(scheduler)
    recorder = attach_job_run_recorder(scheduler)
    scheduler.start()
    try:
        await _settle(recorder)
        expected = {job.id for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)
        await recorder.drain(timeout=1)
    assert len(fake.registrations) == 1
    assert {r.job_id for r in fake.registrations[0]} == expected
    assert recorder.registrations_persisted is True


def test_attach_is_idempotent_per_scheduler() -> None:
    """A scheduler whose start failed is reused: a second attach adds no listener."""
    import tasks

    scheduler = AsyncIOScheduler(timezone="UTC")
    tasks.register_scheduled_jobs(scheduler)
    tasks.register_scheduled_jobs(scheduler)
    recorders = [cb for cb, _mask in scheduler._listeners if isinstance(cb, JobRunRecorder)]  # type: ignore[attr-defined]
    assert len(recorders) == 1
    assert scheduler._listeners[0][1] == EVENT_MASK  # type: ignore[attr-defined]


def test_mask_leaves_out_max_instances() -> None:
    assert not EVENT_MASK & EVENT_JOB_MAX_INSTANCES
    for code in (
        EVENT_SCHEDULER_STARTED,
        EVENT_JOB_SUBMITTED,
        EVENT_JOB_EXECUTED,
        EVENT_JOB_ERROR,
        EVENT_JOB_MISSED,
    ):
        assert EVENT_MASK & code


# ---------------------------------------------------------------------------
# Event mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executed_counts_are_ok_with_duration(
    recorder: JobRunRecorder, writer: FakeWriter
) -> None:
    recorder(_started())
    recorder(_submitted("alpha_job"))
    await asyncio.sleep(0.01)
    recorder(_executed("alpha_job", {"deleted": 4, "note": "x"}))
    await _settle(recorder)

    assert writer.started and writer.started[0][0] == "alpha_job"
    (run,) = writer.runs
    assert run.outcome == "ok"
    assert run.counts == {"deleted": 4}
    assert run.success_at is not None and run.reset_failures is True
    assert run.failures_added == 0
    assert run.duration_ms is not None and run.duration_ms >= 0
    assert run.started_at is not None and run.finished_at is not None


@pytest.mark.asyncio
async def test_returned_job_failed_is_failed(recorder: JobRunRecorder, writer: FakeWriter) -> None:
    recorder(_started())
    recorder(_executed("alpha_job", job_failed(TimeoutError("ws 1"))))
    await _settle(recorder)
    (run,) = writer.runs
    assert (run.outcome, run.error_class, run.failures_added) == ("failed", "TimeoutError", 1)
    assert run.success_at is None and run.reset_failures is False


@pytest.mark.asyncio
async def test_raised_error_is_failed_with_its_class_only(
    recorder: JobRunRecorder, writer: FakeWriter
) -> None:
    recorder(_started())
    recorder(_error("alpha_job", ValueError("user@example.com in ws-secret")))
    await _settle(recorder)
    (run,) = writer.runs
    assert (run.outcome, run.error_class) == ("failed", "ValueError")
    assert "example.com" not in repr(run)


@pytest.mark.asyncio
async def test_missed_keeps_success_fields(recorder: JobRunRecorder, writer: FakeWriter) -> None:
    recorder(_started())
    recorder(_missed("alpha_job"))
    await _settle(recorder)
    (run,) = writer.runs
    assert run.outcome == "missed"
    assert run.finished_at is None and run.success_at is None
    assert run.reset_failures is False and run.failures_added == 0


@pytest.mark.asyncio
async def test_skipped_only_run_is_not_a_success(
    recorder: JobRunRecorder, writer: FakeWriter
) -> None:
    recorder(_started())
    recorder(_executed("alpha_job", {"skipped": 1}))
    await _settle(recorder)
    (run,) = writer.runs
    assert run.outcome == "ok"
    assert run.success_at is None and run.reset_failures is False


@pytest.mark.asyncio
async def test_none_return_is_ok_without_counts(
    recorder: JobRunRecorder, writer: FakeWriter
) -> None:
    recorder(_started())
    recorder(_executed("alpha_job", None))
    await _settle(recorder)
    (run,) = writer.runs
    assert run.outcome == "ok" and run.counts is None and run.success_at is not None


@pytest.mark.asyncio
async def test_unregistered_job_is_not_written(
    recorder: JobRunRecorder, writer: FakeWriter
) -> None:
    recorder(_started())
    recorder(_submitted(f"dynamic_{uuid.uuid4().hex}"))
    recorder(_executed(f"dynamic_{uuid.uuid4().hex}", {"n": 1}))
    await _settle(recorder)
    assert writer.runs == [] and writer.started == []


@pytest.mark.asyncio
async def test_runs_of_one_job_coalesce_without_losing_failures(writer: FakeWriter) -> None:
    rec = JobRunRecorder(_scheduler_with("alpha_job"), writer=writer)
    try:
        rec(_started())
        # The consumer has not run yet: both outcomes land on one queued write.
        rec(_executed("alpha_job", job_failed(OSError())))
        rec(_error("alpha_job", RuntimeError()))
        await _settle(rec)
    finally:
        await rec.drain(timeout=1)
    (run,) = writer.runs
    assert (run.outcome, run.error_class, run.failures_added) == ("failed", "RuntimeError", 2)


@pytest.mark.asyncio
async def test_listener_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(job_runs, "logger", log)
    monkeypatch.setattr(job_runs, "_last_warned", {})
    scheduler = MagicMock()
    scheduler.get_jobs.side_effect = RuntimeError("boom ws-secret")
    rec = JobRunRecorder(scheduler, writer=FakeWriter())
    rec(_started())  # must not raise
    log.warning.assert_called_once_with("job_run_recorder_failed", error_class="RuntimeError")
    await rec.drain(timeout=1)


@pytest.mark.asyncio
async def test_a_failed_write_is_logged_by_class_and_does_not_stop_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = MagicMock()
    monkeypatch.setattr(job_runs, "logger", log)
    monkeypatch.setattr(job_runs, "_last_warned", {})
    writer = FakeWriter(fail_runs=1)
    rec = JobRunRecorder(_scheduler_with("alpha_job", "beta_job"), writer=writer)
    try:
        rec(_started())
        rec(_executed("alpha_job", {"n": 1}))
        rec(_executed("beta_job", {"n": 2}))
        await _settle(rec)
    finally:
        await rec.drain(timeout=1)
    assert [r.job_id for r in writer.runs] == ["beta_job"]
    log.warning.assert_called_once_with(
        "job_status_record_failed", error_class="ConnectionError", kind="run"
    )
    assert "10.0.0.1" not in str(log.warning.call_args_list)


@pytest.mark.asyncio
async def test_registration_is_retried_before_the_next_write() -> None:
    """S4: a lost registration write is retried until it succeeded once."""
    writer = FakeWriter(fail_registrations=1)
    rec = JobRunRecorder(_scheduler_with("alpha_job"), writer=writer)
    try:
        rec(_started())
        await _settle(rec)
        assert rec.registrations_persisted is False
        rec(_executed("alpha_job", {"n": 1}))
        await _settle(rec)
        assert rec.registrations_persisted is True
        rec(_executed("alpha_job", {"n": 2}))
        await _settle(rec)
    finally:
        await rec.drain(timeout=1)
    assert writer.calls == [
        "registrations",
        "registrations",
        "run:alpha_job",
        "run:alpha_job",
    ]
    assert len(writer.registrations) == 1


@pytest.mark.asyncio
async def test_queue_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_runs, "QUEUE_MAX", 2)
    monkeypatch.setattr(job_runs, "_last_warned", {})
    log = MagicMock()
    monkeypatch.setattr(job_runs, "logger", log)
    writer = FakeWriter()
    rec = JobRunRecorder(_scheduler_with("a_job", "b_job", "c_job"), writer=writer)
    try:
        rec(_started())  # 1
        rec(_executed("a_job"))  # 2
        rec(_executed("b_job"))  # full: dropped
        await _settle(rec)
    finally:
        await rec.drain(timeout=1)
    assert [r.job_id for r in writer.runs] == ["a_job"]
    log.warning.assert_any_call("job_run_record_dropped", error_class="QueueFull")


# ---------------------------------------------------------------------------
# Real scheduler: behaviour unchanged, cancellation at shutdown, drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_scheduler_records_failed_then_ok_and_the_job_runs_twice() -> None:
    calls: list[int] = []

    async def flaky() -> dict[str, int]:
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("first run fails for ws-secret")
        return {"done": 1}

    writer = FakeWriter()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(flaky, IntervalTrigger(seconds=1), id="flaky_job", name="Flaky")
    rec = JobRunRecorder(scheduler, writer=writer)
    scheduler.add_listener(rec, EVENT_MASK)
    scheduler.start()
    try:
        scheduler.modify_job("flaky_job", next_run_time=datetime.now(UTC))
        for _ in range(400):
            if len(writer.runs) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        scheduler.shutdown(wait=False)
        await rec.drain(timeout=2)
    assert len(calls) >= 2
    assert [r.outcome for r in writer.runs[:2]] == ["failed", "ok"]
    assert writer.runs[0].error_class == "ValueError"
    assert writer.runs[1].counts == {"done": 1}


@pytest.mark.asyncio
async def test_a_run_cancelled_at_shutdown_is_failed_cancelled_error() -> None:
    """S1: ``shutdown(wait=False)`` cancels a running job; drain records it."""
    entered = asyncio.Event()

    async def swallowing_job() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except Exception:  # noqa: BLE001 - a job's own except does not catch the cancel
            return None

    writer = FakeWriter()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        swallowing_job, IntervalTrigger(hours=1), id="long_job", next_run_time=datetime.now(UTC)
    )
    rec = JobRunRecorder(scheduler, writer=writer)
    scheduler.add_listener(rec, EVENT_MASK)
    scheduler.start()
    await asyncio.wait_for(entered.wait(), timeout=5)
    scheduler.shutdown(wait=False)
    await rec.drain(timeout=2)

    (run,) = writer.runs
    assert (run.job_id, run.outcome, run.error_class) == ("long_job", "failed", "CancelledError")
    assert run.failures_added == 1
    assert rec.closed is True


@pytest.mark.asyncio
async def test_after_drain_events_are_dropped_and_no_task_is_created(
    writer: FakeWriter,
) -> None:
    rec = JobRunRecorder(_scheduler_with("alpha_job"), writer=writer)
    rec(_started())
    await _settle(rec)
    await rec.drain(timeout=1)
    before = set(asyncio.all_tasks())
    rec(_executed("alpha_job", {"n": 1}))
    assert set(asyncio.all_tasks()) == before
    assert writer.runs == []


@pytest.mark.asyncio
async def test_drain_is_bounded_when_the_database_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingWriter(FakeWriter):
        async def record_job_run(self, run: JobRunUpdate) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(job_runs, "logger", MagicMock())
    rec = JobRunRecorder(_scheduler_with("alpha_job"), writer=HangingWriter())
    rec(_started())
    rec(_executed("alpha_job"))
    await asyncio.sleep(0.01)
    await asyncio.wait_for(rec.drain(timeout=0.1), timeout=2)
    assert rec.closed is True


@pytest.mark.asyncio
async def test_drain_job_run_recorders_drains_attached_ones() -> None:
    scheduler = AsyncIOScheduler(timezone="UTC")
    rec = attach_job_run_recorder(scheduler)
    assert rec in job_runs._live
    await job_runs.drain_job_run_recorders(timeout=1)
    assert rec.closed is True and rec not in job_runs._live
    # A later attach on the same object gets a fresh recorder.
    assert attach_job_run_recorder(scheduler) is not rec
    await job_runs.drain_job_run_recorders(timeout=1)
