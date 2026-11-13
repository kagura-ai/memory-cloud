"""``scheduled_job_status`` writers against Postgres (Issue #2768).

The writers open their own sessions (the recorder runs outside any request),
so these tests point the module's session factory at the test engine and
clean the table around each test.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.operations_status import ScheduledJobStatus
from services.operations import job_status
from services.operations.job_status import (
    JobRegistration,
    JobRunUpdate,
    is_overdue,
    record_job_run,
    record_job_started,
    record_registrations,
)

T0 = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)


@pytest.fixture
async def factory(async_engine, monkeypatch: pytest.MonkeyPatch):
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(job_status, "_session_factory", maker)
    async with async_engine.begin() as conn:
        await conn.execute(text("DELETE FROM scheduled_job_status"))
    yield maker
    async with async_engine.begin() as conn:
        await conn.execute(text("DELETE FROM scheduled_job_status"))


def _reg(job_id: str, interval: int | None = 60, kind: str = "interval") -> JobRegistration:
    return JobRegistration(
        job_id=job_id,
        name=job_id.replace("_", " "),
        trigger_kind=kind,  # type: ignore[arg-type]
        expected_interval_seconds=interval,
    )


async def _rows(maker) -> dict[str, ScheduledJobStatus]:
    async with maker() as session:
        rows = (await session.execute(select(ScheduledJobStatus))).scalars().all()
        return {r.job_id: r for r in rows}


def _ok(job_id: str, at: datetime, counts: dict[str, int] | None = None) -> JobRunUpdate:
    return JobRunUpdate(
        job_id=job_id,
        outcome="ok",
        outcome_at=at,
        started_at=at - timedelta(seconds=2),
        finished_at=at,
        duration_ms=2000,
        counts=counts,
        success_at=at,
        reset_failures=True,
    )


def _failed(job_id: str, at: datetime, cls: str = "OSError", n: int = 1) -> JobRunUpdate:
    return JobRunUpdate(
        job_id=job_id,
        outcome="failed",
        outcome_at=at,
        started_at=at - timedelta(seconds=1),
        finished_at=at,
        duration_ms=1000,
        error_class=cls,
        failures_added=n,
    )


class TestRegistrations:
    async def test_one_generation_per_statement(self, factory) -> None:
        await record_registrations([_reg("b_job"), _reg("a_job", 86400, "cron")])
        rows = await _rows(factory)
        assert set(rows) == {"a_job", "b_job"}
        assert rows["a_job"].last_registered_at == rows["b_job"].last_registered_at
        assert rows["a_job"].registered_since == rows["a_job"].last_registered_at
        assert rows["a_job"].trigger_kind == "cron"
        assert rows["a_job"].expected_interval_seconds == 86400
        assert rows["a_job"].consecutive_failures == 0
        assert rows["a_job"].last_counts is None

    async def test_re_registration_keeps_registered_since_of_continuous_jobs(self, factory) -> None:
        await record_registrations([_reg("a_job"), _reg("b_job")])
        first = await _rows(factory)
        await asyncio.sleep(0.01)
        # b_job is disabled in the second generation, re-enabled in the third.
        await record_registrations([_reg("a_job")])
        await asyncio.sleep(0.01)
        await record_registrations([_reg("a_job", 120), _reg("b_job")])
        third = await _rows(factory)

        assert third["a_job"].registered_since == first["a_job"].registered_since
        assert third["a_job"].expected_interval_seconds == 120
        assert third["a_job"].first_registered_at == first["a_job"].first_registered_at
        # b_job was absent from the previous generation: its registration restarts.
        assert third["b_job"].registered_since == third["b_job"].last_registered_at
        assert third["b_job"].registered_since > first["b_job"].registered_since
        assert third["a_job"].last_registered_at == third["b_job"].last_registered_at


class TestRunWrites:
    async def test_ok_failed_missed_arithmetic(self, factory) -> None:
        await record_registrations([_reg("a_job")])
        await record_job_run(_ok("a_job", T0, {"deleted": 3}))
        await record_job_run(_failed("a_job", T0 + timedelta(minutes=1)))
        await record_job_run(_failed("a_job", T0 + timedelta(minutes=2), "TimeoutError"))
        row = (await _rows(factory))["a_job"]
        assert row.last_outcome == "failed"
        assert row.consecutive_failures == 2
        assert row.last_error_class == "TimeoutError"
        assert row.last_success_at == T0
        assert row.last_counts is None  # the latest finished run returned none

        await record_job_run(
            JobRunUpdate(job_id="a_job", outcome="missed", outcome_at=T0 + timedelta(minutes=3))
        )
        row = (await _rows(factory))["a_job"]
        assert row.last_outcome == "missed"
        assert row.consecutive_failures == 2  # missed keeps it
        assert row.last_success_at == T0  # and the success fields
        assert row.last_finished_at == T0 + timedelta(minutes=2)

        await record_job_run(_ok("a_job", T0 + timedelta(minutes=4), {"deleted": 1}))
        row = (await _rows(factory))["a_job"]
        assert (row.last_outcome, row.consecutive_failures) == ("ok", 0)
        assert row.last_error_class is None
        assert row.last_counts == {"deleted": 1}
        assert row.last_duration_ms == 2000
        assert row.last_success_at == T0 + timedelta(minutes=4)

    async def test_skipped_only_run_keeps_success_and_failures(self, factory) -> None:
        await record_registrations([_reg("a_job")])
        await record_job_run(_ok("a_job", T0))
        await record_job_run(_failed("a_job", T0 + timedelta(minutes=1)))
        await record_job_run(
            JobRunUpdate(
                job_id="a_job",
                outcome="ok",
                outcome_at=T0 + timedelta(minutes=2),
                finished_at=T0 + timedelta(minutes=2),
                counts={"skipped": 1},
            )
        )
        row = (await _rows(factory))["a_job"]
        assert row.last_outcome == "ok"
        assert row.last_success_at == T0
        assert row.consecutive_failures == 1
        assert row.last_counts == {"skipped": 1}

    async def test_merged_update_adds_failures(self, factory) -> None:
        await record_registrations([_reg("a_job")])
        merged = _failed("a_job", T0).then(_failed("a_job", T0 + timedelta(seconds=30)))
        await record_job_run(merged)
        await record_job_run(_failed("a_job", T0 + timedelta(minutes=1)))
        assert (await _rows(factory))["a_job"].consecutive_failures == 3

    async def test_an_older_write_landing_late_changes_nothing(self, factory) -> None:
        await record_registrations([_reg("a_job")])
        await record_job_run(_ok("a_job", T0 + timedelta(minutes=5)))
        await record_job_run(_failed("a_job", T0))
        row = (await _rows(factory))["a_job"]
        assert (row.last_outcome, row.consecutive_failures) == ("ok", 0)

    async def test_unknown_job_is_not_inserted_and_registration_untouched(self, factory) -> None:
        await record_registrations([_reg("a_job")])
        before = (await _rows(factory))["a_job"].last_registered_at
        await record_job_run(_ok("ghost_job", T0))
        await record_job_run(_ok("a_job", T0))
        rows = await _rows(factory)
        assert set(rows) == {"a_job"}
        assert rows["a_job"].last_registered_at == before

    async def test_started_takes_the_latest(self, factory) -> None:
        await record_registrations([_reg("a_job")])
        await record_job_started("a_job", T0 + timedelta(minutes=2))
        await record_job_started("a_job", T0)
        row = (await _rows(factory))["a_job"]
        assert row.last_started_at == T0 + timedelta(minutes=2)

    async def test_counts_size_check(self, factory) -> None:
        await record_registrations([_reg("a_job")])
        too_big = {f"k{i}_" + "x" * 60: 2**53 - 1 for i in range(40)}
        with pytest.raises(Exception, match="valid_scheduled_job_counts_size"):
            await record_job_run(_ok("a_job", T0, too_big))


async def test_overdue_on_stored_rows(factory) -> None:
    await record_registrations([_reg("a_job", 60), _reg("b_job", None, "other")])
    rows = await _rows(factory)
    latest = max(r.last_registered_at for r in rows.values())
    registered = rows["a_job"].last_registered_at
    assert not is_overdue(
        rows["a_job"],
        now=registered + timedelta(seconds=60),
        grace_seconds=0,
        latest_registration=latest,
    )
    assert is_overdue(
        rows["a_job"],
        now=registered + timedelta(seconds=61),
        grace_seconds=0,
        latest_registration=latest,
    )
    assert not is_overdue(
        rows["b_job"],
        now=registered + timedelta(days=9),
        grace_seconds=0,
        latest_registration=latest,
    )
