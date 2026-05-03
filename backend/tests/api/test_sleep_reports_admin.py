"""Tests for Sleep Reports admin API endpoints (Issue #179).

Covers:
- GET /api/v1/admin/sleep-reports — list with pagination and filters
- GET /api/v1/admin/sleep-reports/{id} — detail with actions

Uses dependency_overrides to mock auth and DB — no real Docker/Postgres required.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db


def _mock_admin_user() -> dict:
    return {
        "user_id": "admin_user_1",
        "email": "admin@test.com",
        "role": "admin",
    }


_SENTINEL = object()


def _make_mock_report(
    *,
    status: str = "completed",
    memories_processed: int = 7,
    context_id=_SENTINEL,
    user_id: str = "local:admin",
):
    r = MagicMock()
    r.id = uuid4()
    r.user_id = user_id
    r.workspace_id = uuid4()
    r.context_id = uuid4() if context_id is _SENTINEL else context_id
    r.status = status
    r.started_at = datetime(2026, 4, 6, 3, 0, 0)
    r.completed_at = datetime(2026, 4, 6, 3, 3, 0)
    r.memories_processed = memories_processed
    r.edges_created = 2
    r.memories_merged = 1
    r.memories_promoted = 3
    r.memories_flagged = 0
    r.llm_calls_made = 4
    r.llm_tokens_used = 1200
    r.embedding_calls_made = 2
    r.error_message = None
    r.edge_discovery_result = {"success": True, "details": {"edges_created": 2}}
    r.dedup_result = {"success": True, "details": {"merged": 1}}
    r.importance_result = None
    r.consolidation_result = {"success": True}
    r.reindex_result = {"success": True}
    return r


def _make_mock_context(
    *,
    name: str = "kagura-dev",
    display_name: str | None = "Kagura Dev",
    deleted: bool = False,
):
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.name = name
    ctx.display_name = display_name
    ctx.deleted_at = datetime(2026, 1, 1) if deleted else None
    return ctx


def _make_mock_action(phase: str = "edge_discovery", action_type: str = "create_edge"):
    a = MagicMock()
    a.id = 1
    a.report_id = uuid4()
    a.phase = phase
    a.action_type = action_type
    a.memory_id = uuid4()
    a.target_id = uuid4()
    a.details = {"edge_type": "related_to", "confidence": 0.8}
    a.created_at = datetime(2026, 4, 6, 3, 1, 0)
    return a


def _install_overrides(db_mock):
    """Install dependency overrides for require_admin and get_db."""

    async def mock_admin():
        return _mock_admin_user()

    async def mock_get_db():
        yield db_mock

    app.dependency_overrides[require_admin] = mock_admin
    app.dependency_overrides[get_db] = mock_get_db


@pytest.fixture
def client():
    """Provide a TestClient and clear overrides after the test."""
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestListSleepReports:
    """GET /api/v1/admin/sleep-reports"""

    def test_returns_paginated_list(self, client):
        ctx = _make_mock_context()
        reports = [_make_mock_report(context_id=ctx.id) for _ in range(3)]

        mock_db = AsyncMock()
        # Call 1: count query → scalar=3
        count_result = MagicMock()
        count_result.scalar.return_value = 3
        # Call 2: list query → scalars().all()=reports
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        # Call 3: batch context query → .all()=[(id, name, display_name, deleted_at)]
        ctx_result = MagicMock()
        ctx_result.all.return_value = [(ctx.id, ctx.name, ctx.display_name, ctx.deleted_at)]
        mock_db.execute.side_effect = [count_result, list_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert len(data["reports"]) == 3
        # Verify timestamp has Z suffix (timezone fix)
        assert data["reports"][0]["started_at"].endswith("Z")
        # Verify context_name is resolved via batch lookup
        assert data["reports"][0]["context_name"] == "Kagura Dev"

    def test_filters_by_status(self, client):
        ctx = _make_mock_context()
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = [
            _make_mock_report(status="failed", context_id=ctx.id)
        ]
        ctx_result = MagicMock()
        ctx_result.all.return_value = [(ctx.id, ctx.name, ctx.display_name, ctx.deleted_at)]
        mock_db.execute.side_effect = [count_result, list_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports?status=failed")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["reports"][0]["status"] == "failed"

    def test_invalid_status_returns_400(self, client):
        mock_db = AsyncMock()
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports?status=bogus")
        assert response.status_code == 400
        assert "Invalid status" in response.json()["detail"]

    def test_respects_limit_and_offset(self, client):
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 100
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = []
        # No context query because reports list is empty
        mock_db.execute.side_effect = [count_result, list_result]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports?limit=25&offset=50")
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 25
        assert data["offset"] == 50
        assert data["total"] == 100

    def test_returns_empty_when_no_reports(self, client):
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [count_result, list_result]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["reports"] == []

    def test_list_context_name_null_when_no_context(self, client):
        """Reports with context_id=None should have context_name=None."""
        reports = [_make_mock_report(context_id=None)]

        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        # No context query because all context_ids are None
        mock_db.execute.side_effect = [count_result, list_result]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        data = response.json()
        assert data["reports"][0]["context_name"] is None
        # Only 2 DB calls: count + list (no context batch query)
        assert mock_db.execute.call_count == 2

    def test_list_context_name_falls_back_to_name(self, client):
        """When display_name is None, context_name should fall back to name."""
        ctx = _make_mock_context(name="my-context", display_name=None)
        reports = [_make_mock_report(context_id=ctx.id)]

        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        ctx_result = MagicMock()
        ctx_result.all.return_value = [(ctx.id, ctx.name, ctx.display_name, ctx.deleted_at)]
        mock_db.execute.side_effect = [count_result, list_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        assert response.json()["reports"][0]["context_name"] == "my-context"

    def test_list_context_name_none_when_deleted(self, client):
        """Deleted contexts should resolve to context_name=None."""
        ctx = _make_mock_context(deleted=True)
        reports = [_make_mock_report(context_id=ctx.id)]

        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        ctx_result = MagicMock()
        ctx_result.all.return_value = [(ctx.id, ctx.name, ctx.display_name, ctx.deleted_at)]
        mock_db.execute.side_effect = [count_result, list_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        assert response.json()["reports"][0]["context_name"] is None
        # 3 DB calls: count + list + context batch query (deleted path exercised)
        assert mock_db.execute.call_count == 3

    def test_list_context_name_none_when_context_row_missing(self, client):
        """Reports referencing a non-existent context should have context_name=None."""
        reports = [_make_mock_report()]  # context_id is a random UUID

        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        ctx_result = MagicMock()
        ctx_result.all.return_value = []  # No matching context
        mock_db.execute.side_effect = [count_result, list_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        assert response.json()["reports"][0]["context_name"] is None


class TestGetSleepReportDetail:
    """GET /api/v1/admin/sleep-reports/{id}"""

    def test_returns_report_with_actions(self, client):
        report = _make_mock_report()
        ctx = _make_mock_context()
        actions = [
            _make_mock_action(),
            _make_mock_action(phase="dedup_merge", action_type="merge"),
        ]

        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        ctx_result = MagicMock()
        ctx_result.one_or_none.return_value = (ctx.name, ctx.display_name, ctx.deleted_at)
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = actions
        # Order after #526 service extraction + asyncio.gather:
        # 1. service.get_report_detail: SleepReport query → report_result
        # 2. service.get_report_detail: SleepAction query → actions_result
        # 3. service.resolve_context_name: Context query → ctx_result
        mock_db.execute.side_effect = [report_result, actions_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["action_count"] == 2
        assert data["report"]["memories_processed"] == 7
        assert data["report"]["context_name"] == "Kagura Dev"
        assert data["report"]["context_deleted"] is False
        # Verify Z suffix on both report and action timestamps
        assert data["report"]["started_at"].endswith("Z")
        assert data["actions"][0]["created_at"].endswith("Z")
        assert data["actions"][0]["action_type"] == "create_edge"
        assert data["actions"][1]["action_type"] == "merge"

    def test_falls_back_to_name_when_display_name_missing(self, client):
        report = _make_mock_report()
        ctx = _make_mock_context(name="kagura-dev", display_name=None)

        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        ctx_result = MagicMock()
        ctx_result.one_or_none.return_value = (ctx.name, ctx.display_name, ctx.deleted_at)
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [report_result, actions_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")
        assert response.status_code == 200
        body = response.json()["report"]
        assert body["context_name"] == "kagura-dev"
        assert body["context_deleted"] is False

    def test_deleted_context_returns_deleted_flag(self, client):
        report = _make_mock_report()
        ctx = _make_mock_context(deleted=True)

        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        ctx_result = MagicMock()
        ctx_result.one_or_none.return_value = (ctx.name, ctx.display_name, ctx.deleted_at)
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [report_result, actions_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")
        assert response.status_code == 200
        body = response.json()["report"]
        assert body["context_name"] is None
        assert body["context_deleted"] is True

    def test_missing_context_row_returns_deleted_flag(self, client):
        report = _make_mock_report()

        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        ctx_result = MagicMock()
        ctx_result.one_or_none.return_value = None
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [report_result, actions_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")
        assert response.status_code == 200
        body = response.json()["report"]
        assert body["context_name"] is None
        assert body["context_deleted"] is True

    def test_null_context_id_omits_context_fields(self, client):
        report = _make_mock_report(context_id=None)

        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        # No Context query because context_id is None
        mock_db.execute.side_effect = [report_result, actions_result]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")
        assert response.status_code == 200
        body = response.json()["report"]
        assert body["context_name"] is None
        assert body["context_deleted"] is False

    def test_not_found_returns_404(self, client):
        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = None
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        # asyncio.gather still fires the actions query even when report is missing
        mock_db.execute.side_effect = [report_result, actions_result]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{uuid4()}")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_invalid_uuid_returns_422(self, client):
        mock_db = AsyncMock()
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports/not-a-uuid")
        assert response.status_code == 422

    def test_empty_actions_returns_zero_count(self, client):
        report = _make_mock_report()
        ctx = _make_mock_context()

        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        ctx_result = MagicMock()
        ctx_result.one_or_none.return_value = (ctx.name, ctx.display_name, ctx.deleted_at)
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [report_result, actions_result, ctx_result]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["action_count"] == 0
        assert data["actions"] == []
