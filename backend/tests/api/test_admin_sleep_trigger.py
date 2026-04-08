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
        #   1. concurrency guard → no running report (.first() → None)
        #   2. target contexts query → one eligible row (.all() → [(ws, ctx)])
        running_result = MagicMock()
        running_result.first.return_value = None
        target_result = MagicMock()
        target_result.all.return_value = [(workspace_id, context_id)]

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [running_result, target_result]

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

        running_result = MagicMock()
        running_result.first.return_value = (running_id, started_at)

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [running_result]

        _install_overrides(mock_db)

        response = client.post(
            "/api/v1/admin/sleep/run",
            json={"context_id": str(uuid4())},
        )

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["error"] == "sleep_run_in_progress"
        assert "already in progress" in body["message"].lower()
        assert body["details"]["running_report_id"] == str(running_id)
        # to_utc_iso renders with a trailing Z.
        assert body["details"]["started_at"].endswith("Z")
        # Must not schedule any background task when rejected.
        assert no_background_tasks == []
        # Must not commit when rejected.
        mock_db.commit.assert_not_awaited()

    def test_returns_404_when_no_eligible_contexts(self, client, no_background_tasks):
        running_result = MagicMock()
        running_result.first.return_value = None
        target_result = MagicMock()
        target_result.all.return_value = []  # admin has no eligible contexts

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [running_result, target_result]

        _install_overrides(mock_db)

        response = client.post(
            "/api/v1/admin/sleep/run",
            json={"context_id": str(uuid4())},
        )

        assert response.status_code == 404, response.text
        body = response.json()
        assert body["error"] == "sleep_target_not_found"
        assert no_background_tasks == []
        mock_db.commit.assert_not_awaited()
