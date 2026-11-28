"""Scheduling of the public MCP usage event purge (#2678)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tasks import public_mcp_usage_tasks
from tasks.public_mcp_usage_tasks import schedule_public_mcp_usage_tasks
from tests.tasks.conftest import mock_get_db_factory


def _job(scheduler: MagicMock, job_id: str) -> dict:
    [kwargs] = [c.kwargs for c in scheduler.add_job.call_args_list if c.kwargs["id"] == job_id]
    return kwargs


def test_scheduled_daily_at_0450_utc():
    scheduler = MagicMock()
    schedule_public_mcp_usage_tasks(scheduler)
    kwargs = _job(scheduler, "purge_public_mcp_usage_events")
    assert kwargs["replace_existing"] is True
    fields = {f.name: str(f) for f in kwargs["trigger"].fields}
    assert fields["hour"] == "4"
    assert fields["minute"] == "50"


def test_registered_with_the_leader_jobs():
    import tasks

    scheduler = MagicMock()
    tasks.register_scheduled_jobs(scheduler)
    ids = {c.kwargs.get("id") for c in scheduler.add_job.call_args_list}
    assert "purge_public_mcp_usage_events" in ids
    assert "close_public_mcp_usage_days" in ids
    assert "schedule_public_mcp_usage_tasks" in tasks.__all__


@pytest.mark.asyncio
async def test_task_rolls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(public_mcp_usage_tasks, "get_db", mock_get_db_factory(db))
    log = MagicMock()
    monkeypatch.setattr(public_mcp_usage_tasks, "logger", log)

    await public_mcp_usage_tasks.purge_public_mcp_usage_events_task()

    db.rollback.assert_awaited_once()
    log.exception.assert_called_once_with("public_mcp_usage_purge_failed")


class _Result:
    def __init__(self, rows: list | None = None, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def all(self) -> list:
        return list(self._rows)


class _PurgeDB:
    """Answers the purge's statements in order; records each one with its params."""

    def __init__(self, visitor_batches: list[int]) -> None:
        self.statements: list[tuple[str, dict]] = []
        self.visitor_batches = list(visitor_batches)
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, dict(params or {})))
        if "DELETE FROM public_mcp_daily_visitors" in sql:
            n = self.visitor_batches.pop(0) if self.visitor_batches else 0
            return _Result(rows=[(i,) for i in range(n)], rowcount=n)
        return _Result()

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_visitor_rows_are_purged_by_day_after_events_and_doc_keys(monkeypatch) -> None:
    """#3035: bound to ``cutoff.date()``, batched, after the events and doc keys."""
    from datetime import datetime, timedelta

    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "public_search_telemetry_enabled", False)
    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_enabled", False)
    log = MagicMock()
    monkeypatch.setattr(public_mcp_usage_tasks, "logger", log)
    now = datetime(2026, 11, 28, 4, 50)
    db = _PurgeDB(visitor_batches=[3, 2])
    deleted = await public_mcp_usage_tasks.purge_public_mcp_usage_events(
        db,  # type: ignore[arg-type]
        now,
        batch_size=3,
    )
    assert deleted == 0
    sqls = [sql for sql, _ in db.statements]
    events = next(i for i, sql in enumerate(sqls) if "DELETE FROM public_mcp_usage_events" in sql)
    keys = next(i for i, sql in enumerate(sqls) if "DELETE FROM public_mcp_doc_keys" in sql)
    visitors = [i for i, sql in enumerate(sqls) if "DELETE FROM public_mcp_daily_visitors" in sql]
    assert events < keys < visitors[0]
    assert len(visitors) == 2  # a short batch ends the loop
    cutoff = now - timedelta(days=get_settings().mcp_public_telemetry_retention_days)
    for i in visitors:
        assert db.statements[i][1] == {"cutoff_day": cutoff.date(), "limit": 3}
        assert "day < :cutoff_day" in sqls[i]
    line = log.info.call_args
    assert line.args == ("public_mcp_usage_purged",)
    assert line.kwargs["visitors_deleted"] == 5


@pytest.mark.asyncio
async def test_the_job_result_is_unchanged(monkeypatch) -> None:
    async def purge(db, now=None, *, batch_size=5000):
        return 7

    monkeypatch.setattr(public_mcp_usage_tasks, "purge_public_mcp_usage_events", purge)
    monkeypatch.setattr(public_mcp_usage_tasks, "get_db", mock_get_db_factory(MagicMock()))
    assert await public_mcp_usage_tasks.purge_public_mcp_usage_events_task() == {"deleted": 7}


# ----------------------------------------------------------------------------
# #3053: closing finished days, and their purge
# ----------------------------------------------------------------------------


def test_days_are_closed_hourly_at_minute_20():
    scheduler = MagicMock()
    schedule_public_mcp_usage_tasks(scheduler)
    kwargs = _job(scheduler, "close_public_mcp_usage_days")
    assert kwargs["replace_existing"] is True
    fields = {f.name: str(f) for f in kwargs["trigger"].fields}
    assert fields["hour"] == "*"
    assert fields["minute"] == "20"
    [call] = [c for c in scheduler.add_job.call_args_list if c.kwargs["id"] == kwargs["id"]]
    assert call.args == (public_mcp_usage_tasks.close_public_mcp_usage_days_task,)


@pytest.mark.asyncio
async def test_the_close_job_reports_the_days_it_closed(monkeypatch) -> None:
    async def close(db, now=None, **kwargs):
        return 3

    monkeypatch.setattr(public_mcp_usage_tasks, "close_usage_days", close)
    monkeypatch.setattr(public_mcp_usage_tasks, "get_db", mock_get_db_factory(MagicMock()))
    assert await public_mcp_usage_tasks.close_public_mcp_usage_days_task() == {"closed": 3}


@pytest.mark.asyncio
async def test_the_close_job_rolls_back_on_error(monkeypatch) -> None:
    from tasks.job_runs import job_failed

    async def close(db, now=None, **kwargs):
        raise TimeoutError

    db = MagicMock()
    db.rollback = AsyncMock()
    log = MagicMock()
    monkeypatch.setattr(public_mcp_usage_tasks, "close_usage_days", close)
    monkeypatch.setattr(public_mcp_usage_tasks, "get_db", mock_get_db_factory(db))
    monkeypatch.setattr(public_mcp_usage_tasks, "logger", log)
    result = await public_mcp_usage_tasks.close_public_mcp_usage_days_task()
    assert result == job_failed(TimeoutError())
    db.rollback.assert_awaited_once()
    log.exception.assert_called_once_with("public_mcp_usage_close_failed")


class _RollupPurgeDB(_PurgeDB):
    def __init__(self, count_batches: list[int]) -> None:
        super().__init__(visitor_batches=[])
        self.count_batches = list(count_batches)

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "DELETE FROM public_mcp_usage_day_counts" in sql:
            self.statements.append((sql, dict(params or {})))
            n = self.count_batches.pop(0) if self.count_batches else 0
            return _Result(rows=[(i,) for i in range(n)], rowcount=n)
        return await super().execute(statement, params)


@pytest.mark.parametrize(
    ("now", "first_kept"),
    [
        # The cutoff at 04:50 of a day: that day lost its first hours, it goes too.
        ("2026-11-28T04:50:00", "2026-08-31"),
        # A cutoff at midnight: the cutoff's day lost nothing and stays.
        ("2026-11-28T00:00:00", "2026-08-30"),
    ],
)
@pytest.mark.asyncio
async def test_day_counts_go_with_every_day_the_purge_reached(monkeypatch, now, first_kept):
    """The closed marks first, then the counts in batches, after the visitors;
    bound to the first day that starts at or after the events' cutoff."""
    from datetime import date, datetime

    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_retention_days", 90)
    log = MagicMock()
    monkeypatch.setattr(public_mcp_usage_tasks, "logger", log)
    db = _RollupPurgeDB(count_batches=[3, 1])
    await public_mcp_usage_tasks.purge_public_mcp_usage_events(
        db,  # type: ignore[arg-type]
        datetime.fromisoformat(now),
        batch_size=3,
    )
    sqls = [sql for sql, _ in db.statements]
    visitors = max(
        i for i, sql in enumerate(sqls) if "DELETE FROM public_mcp_daily_visitors" in sql
    )
    closed = [i for i, sql in enumerate(sqls) if "DELETE FROM public_mcp_usage_closed_days" in sql]
    counts = [i for i, sql in enumerate(sqls) if "DELETE FROM public_mcp_usage_day_counts" in sql]
    assert len(closed) == 1
    assert visitors < closed[0] < counts[0]
    assert len(counts) == 2  # a short batch ends the loop
    expected = date.fromisoformat(first_kept)
    assert db.statements[closed[0]][1] == {"first_kept_day": expected}
    for i in counts:
        assert db.statements[i][1] == {"first_kept_day": expected, "limit": 3}
        assert "day < :first_kept_day" in sqls[i]
    assert log.info.call_args.kwargs["day_counts_deleted"] == 4
