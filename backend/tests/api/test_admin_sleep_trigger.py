"""Tests for the manual Sleep Maintenance trigger endpoint (Issue #247).

Covers:
- POST /api/v1/admin/sleep/run — happy path (single context) returns 202
  with a report_id and schedules one sleep batch task.
- POST /api/v1/admin/sleep/run — 409 structured error when a sleep run is
  already in progress for the calling admin.
- POST /api/v1/admin/sleep/run — 404 when the admin has no eligible
  contexts (no silent orphan report).

Uses dependency_overrides to mock auth and DB, and monkeypatches
``asyncio.create_task`` in the admin_sleep module so background phase
execution never runs during unit tests.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import admin_sleep
from auth.dependencies import require_admin
from db.base import get_db


def _mock_admin_user() -> dict:
    return {
        "user_id": "admin_user_1",
        "email": "admin@test.com",
        "role": "admin",
    }


def _install_overrides(db_mock) -> None:
    async def mock_admin():
        return _mock_admin_user()

    async def mock_get_db():
        yield db_mock

    app.dependency_overrides[require_admin] = mock_admin
    app.dependency_overrides[get_db] = mock_get_db


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def no_background_tasks(monkeypatch):
    """Replace ``asyncio.create_task`` in the admin_sleep module with a no-op.

    The real create_task would try to execute the sleep batch with a
    mocked AsyncSession and blow up on the first DB call. In these unit
    tests we only care that a task was scheduled, not that phases run.
    """

    scheduled: list = []

    def fake_create_task(coro, *_args, **_kwargs):
        # Close the coroutine so Python does not emit
        # "coroutine was never awaited" warnings.
        coro.close()
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = None
        scheduled.append(task)
        return task

    monkeypatch.setattr(admin_sleep.asyncio, "create_task", fake_create_task)
    return scheduled


def _reports(rows: list) -> MagicMock:
    """A result whose ``.scalars().all()`` is ``rows`` (the guard's two reads, #2856)."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _running_report(report_id: UUID, started_at: datetime) -> MagicMock:
    report = MagicMock()
    report.id = report_id
    report.started_at = started_at
    return report


def _patch_create_report(monkeypatch, report_ids: list[UUID]):
    """Replace SleepReporter.create_report so it yields stable UUIDs.

    With an AsyncMock database session, SQLAlchemy's ``default=uuid4`` on
    SleepReport.id never fires, so the real create_report would hand back
    a report with ``id=None`` and the endpoint serialization would break.
    """

    iterator = iter(report_ids)

    async def fake_create_report(_self, user_id, workspace_id=None, context_id=None):
        report = MagicMock()
        report.id = next(iterator)
        report.user_id = user_id
        report.workspace_id = workspace_id
        report.context_id = context_id
        report.status = "running"
        return report

    monkeypatch.setattr(
        admin_sleep.SleepReporter,
        "create_report",
        fake_create_report,
    )


class TestTriggerSleepRun:
    """POST /api/v1/admin/sleep/run"""

    def test_happy_path_returns_202_with_report_id(self, client, monkeypatch, no_background_tasks):
        context_id = uuid4()
        workspace_id = uuid4()
        new_report_id = uuid4()

        # DB execute sequence:
        #   1-2. concurrency guard → no running report held, none running
        #   3. target contexts query → one eligible row (.all() → [(ws, ctx)])
        target_result = MagicMock()
        target_result.all.return_value = [(workspace_id, context_id)]

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [_reports([]), _reports([]), target_result]

        _install_overrides(mock_db)
        _patch_create_report(monkeypatch, [new_report_id])

        response = client.post(
            "/api/v1/admin/sleep/run",
            json={"context_id": str(context_id)},
        )

        assert response.status_code == 202, response.text
        data = response.json()
        assert data == {"report_ids": [str(new_report_id)]}
        # Exactly one background batch task is scheduled regardless of how
        # many contexts are in the batch.
        assert len(no_background_tasks) == 1
        # Endpoint must commit the reports before returning.
        mock_db.commit.assert_awaited()

    def test_returns_409_when_run_in_progress(self, client, no_background_tasks):
        running_id = uuid4()
        started_at = datetime(2026, 4, 9, 11, 30, 0)

        # Not among the rows this request could lock: another transaction
        # holds it, so it is live whatever its age (#2856).
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _reports([]),
            _reports([_running_report(running_id, started_at)]),
        ]

        _install_overrides(mock_db)

        response = client.post(
            "/api/v1/admin/sleep/run",
            json={"context_id": str(uuid4())},
        )

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["error"] == "SLEEP-002"
        assert "already in progress" in body["message"].lower()
        assert body["details"]["running_report_id"] == str(running_id)
        # to_utc_iso renders with a trailing Z.
        assert body["details"]["started_at"].endswith("Z")
        # Must not schedule any background task when rejected.
        assert no_background_tasks == []
        # Must not commit when rejected.
        mock_db.commit.assert_not_awaited()

    def test_returns_404_when_no_eligible_contexts(self, client, no_background_tasks):
        target_result = MagicMock()
        target_result.all.return_value = []  # admin has no eligible contexts

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [_reports([]), _reports([]), target_result]

        _install_overrides(mock_db)

        response = client.post(
            "/api/v1/admin/sleep/run",
            json={"context_id": str(uuid4())},
        )

        assert response.status_code == 404, response.text
        body = response.json()
        assert body["error"] == "SLEEP-003"
        assert no_background_tasks == []
        mock_db.commit.assert_not_awaited()


class TestBatchTaskTracking:
    """#2856: a started batch is held strongly, and its reports count as live
    for this process's guard, until the batch ends."""

    @pytest.mark.asyncio
    async def test_the_task_and_its_report_ids_are_tracked_while_it_runs(self, monkeypatch):
        import asyncio

        release = asyncio.Event()
        seen: list[tuple[list, str]] = []

        async def fake_batch(jobs, user_id):
            seen.append((jobs, user_id))
            await release.wait()

        monkeypatch.setattr(admin_sleep, "_run_sleep_batch", fake_batch)
        report_ids = [uuid4(), uuid4()]
        jobs = [(report_id, "ws", "ctx") for report_id in report_ids]

        admin_sleep._start_sleep_batch(jobs, "user-1")
        [task] = [t for t in admin_sleep._IN_FLIGHT_BATCHES if not t.done()]
        await asyncio.sleep(0)

        assert seen == [(jobs, "user-1")]
        assert set(report_ids) <= admin_sleep._LIVE_REPORT_IDS

        release.set()
        await task
        await asyncio.sleep(0)  # the done callback runs on the next loop step

        assert task not in admin_sleep._IN_FLIGHT_BATCHES
        assert not set(report_ids) & admin_sleep._LIVE_REPORT_IDS

    @pytest.mark.asyncio
    async def test_a_crashed_batch_is_released_and_logged(self, monkeypatch):
        import asyncio

        from structlog.testing import capture_logs

        async def crashing_batch(_jobs, _user_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(admin_sleep, "_run_sleep_batch", crashing_batch)
        report_id = uuid4()

        with capture_logs() as logs:
            admin_sleep._start_sleep_batch([(report_id, "ws", "ctx")], "user-1")
            [task] = [t for t in admin_sleep._IN_FLIGHT_BATCHES if not t.done()]
            with pytest.raises(RuntimeError):
                await task
            await asyncio.sleep(0)

        assert task not in admin_sleep._IN_FLIGHT_BATCHES
        assert report_id not in admin_sleep._LIVE_REPORT_IDS
        assert [e["event"] for e in logs] == ["manual_sleep_background_task_crashed"]

    @pytest.mark.asyncio
    async def test_shutdown_with_no_batch_does_nothing(self):
        await admin_sleep.stop_manual_sleep_runs(timeout=0.1)

    @pytest.mark.asyncio
    async def test_shutdown_cancels_a_running_batch_and_waits_for_it(self, monkeypatch):
        import asyncio

        started, finished = asyncio.Event(), []

        async def endless_batch(_jobs, _user_id):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append("finally")

        monkeypatch.setattr(admin_sleep, "_run_sleep_batch", endless_batch)
        report_id = uuid4()
        admin_sleep._start_sleep_batch([(report_id, "ws", "ctx")], "user-1")
        [task] = [t for t in admin_sleep._IN_FLIGHT_BATCHES if not t.done()]
        await started.wait()

        await admin_sleep.stop_manual_sleep_runs(timeout=1.0)

        # The batch was cancelled and had run its cleanup before the step returned.
        assert task.cancelled()
        assert finished == ["finally"]
        await asyncio.sleep(0)
        assert task not in admin_sleep._IN_FLIGHT_BATCHES
        assert report_id not in admin_sleep._LIVE_REPORT_IDS


class TestLifespanStopsManualSleepRuns:
    def test_shutdown_stops_the_runs_before_redis_qdrant_and_the_db_close(self):
        """Awaited calls after ``yield`` in ``lifespan()``, read from the AST (#2856).

        The cancelled batches close their reports in a database session of
        their own, so the stop step runs while the database is still open.
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text(
            encoding="utf-8"
        )
        lifespan = next(
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
        )
        yield_line = next(n.lineno for n in ast.walk(lifespan) if isinstance(n, ast.Yield))
        awaited = [
            ast.unparse(node.value.func)
            for node in sorted(
                (n for n in ast.walk(lifespan) if isinstance(n, ast.Await)),
                key=lambda n: (n.lineno, n.col_offset),
            )
            if node.lineno > yield_line and isinstance(node.value, ast.Call)
        ]

        assert awaited.count("stop_manual_sleep_runs") == 1
        assert (
            awaited.index("stop_manual_sleep_runs")
            < awaited.index("_close_redis_on_shutdown")
            < awaited.index("_close_qdrant_on_shutdown")
            < awaited.index("close_db")
        )
