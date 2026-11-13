"""The backlog registry and reader loop (#2768), without a database."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import Settings
from services.operations import backlogs
from services.operations.backlogs import (
    BACKLOG_NAME_RE,
    BUILTIN_BACKLOGS,
    DEADLINE_EXCEEDED,
    BacklogSpec,
    read_backlogs,
)
from services.operations.job_status import JOB_ID_RE

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)

# Asked for by the issue; the rest complete the list from the jobs' queues.
REQUIRED = {
    "embedding_pending",
    "embedding_failed_retryable",
    "embedding_failed_stalled",
    "deferred_point_deletes",
    "erasure_pending",
    "erasure_cooling_off",
    "erasure_in_progress",
    "erasure_failed",
    "force_transfer_requests_pending",
}


def test_registry_names_are_valid_unique_and_sorted() -> None:
    names = [spec.name for spec in BUILTIN_BACKLOGS]
    assert names == sorted(set(names))
    assert REQUIRED <= set(names)
    for spec in BUILTIN_BACKLOGS:
        assert BACKLOG_NAME_RE.match(spec.name), spec.name
        for job_id in spec.drained_by:
            assert JOB_ID_RE.match(job_id), job_id


def test_no_file_scan_entry_on_this_branch() -> None:
    assert not [s for s in BUILTIN_BACKLOGS if s.name.startswith("files_")]


def test_every_drainer_is_a_registered_job(monkeypatch: pytest.MonkeyPatch) -> None:
    import tasks

    monkeypatch.setenv("DEFERRED_POINT_DRAIN_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_SECONDS", "300")
    scheduler = AsyncIOScheduler(timezone="UTC")
    tasks.register_scheduled_jobs(scheduler)
    ids = {job.id for job in scheduler.get_jobs()}
    for spec in BUILTIN_BACKLOGS:
        assert set(spec.drained_by) <= ids, (spec.name, set(spec.drained_by) - ids)


def test_referral_entry_follows_the_sweep_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = next(s for s in BUILTIN_BACKLOGS if s.name == "referral_grants_pending")
    settings = Settings(_env_file=None)
    monkeypatch.setenv("REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_SECONDS", "0")
    assert spec.enabled(settings) is False
    monkeypatch.setenv("REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_SECONDS", "300")
    assert spec.enabled(settings) is True


def _fake_db() -> Any:
    db = MagicMock()
    db.execute = AsyncMock()

    @asynccontextmanager
    async def _nested():
        yield

    db.begin_nested = _nested
    return db


async def _ok(db: Any, now: datetime) -> tuple[int, datetime | None]:
    return 3, datetime(2026, 11, 13, 11, 0)  # naive = UTC


async def _boom(db: Any, now: datetime) -> tuple[int, datetime | None]:
    raise ConnectionResetError("workspace 1234 user someone@example.com")


async def test_a_failing_reader_names_its_class_and_the_rest_are_read(monkeypatch) -> None:
    monkeypatch.setattr(
        backlogs,
        "BUILTIN_BACKLOGS",
        (
            BacklogSpec("a_queue", ("job_a",), _boom),
            BacklogSpec("b_queue", (), _ok),
            BacklogSpec("c_queue", (), _ok, enabled=lambda s: False),
        ),
    )
    readings = await read_backlogs(_fake_db(), NOW, Settings(_env_file=None))
    assert [r.name for r in readings] == ["a_queue", "b_queue"]
    a, b = readings
    assert (a.count, a.oldest_at, a.error_class) == (None, None, "ConnectionResetError")
    assert "example.com" not in repr(a)
    assert b.count == 3
    assert b.oldest_at == datetime(2026, 11, 13, 11, 0, tzinfo=UTC)
    assert b.drained_by == ()


async def test_entries_past_the_deadline_are_not_read(monkeypatch) -> None:
    reader = AsyncMock(side_effect=_ok)
    monkeypatch.setattr(
        backlogs,
        "BUILTIN_BACKLOGS",
        (BacklogSpec("a_queue", (), reader), BacklogSpec("b_queue", (), reader)),
    )
    left = iter([1.0, 0.0])
    db = _fake_db()
    readings = await read_backlogs(db, NOW, Settings(_env_file=None), deadline=lambda: next(left))
    assert [r.error_class for r in readings] == [None, DEADLINE_EXCEEDED]
    assert reader.await_count == 1
    # The statement timeout is capped at what is left of the budget.
    assert db.execute.await_args.args[1] == {"ms": "1000"}
