"""Tests for workspace-scoped Sleep Reports API endpoints (Issue #526).

Covers:
- GET /api/v1/workspaces/{workspace_id}/sleep-reports — list scoped to one workspace
- GET /api/v1/workspaces/{workspace_id}/sleep-reports/{id} — detail scoped to one workspace

Uses dependency_overrides to mock auth and DB — no real Docker/Postgres required.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_current_user
from db.base import get_db
from services.permission_service import PermissionService

_WORKSPACE_ID = uuid4()
_OTHER_WORKSPACE_ID = uuid4()


def _owner_user() -> dict:
    return {"user_id": "owner_1", "email": "owner@test.com", "role": "user"}


def _regular_user() -> dict:
    return {"user_id": "user_1", "email": "user@test.com", "role": "user"}


def _make_mock_report(
    *,
    status: str = "completed",
    workspace_id=_WORKSPACE_ID,
    context_id=None,
):
    r = MagicMock()
    r.id = uuid4()
    r.user_id = "local:owner"
    r.workspace_id = workspace_id
    r.context_id = context_id
    r.status = status
    r.started_at = datetime(2026, 4, 6, 3, 0, 0)
    r.completed_at = datetime(2026, 4, 6, 3, 3, 0)
    r.memories_processed = 7
    r.edges_created = 2
    r.memories_merged = 1
    r.memories_promoted = 3
    r.memories_flagged = 0
    r.llm_calls_made = 4
    r.llm_tokens_used = 1200
    r.embedding_calls_made = 2
    r.error_message = None
    r.edge_discovery_result = {"success": True}
    r.dedup_result = {"success": True}
    r.importance_result = None
    r.consolidation_result = {"success": True}
    r.reindex_result = {"success": True}
    return r


def _make_mock_action():
    a = MagicMock()
    a.id = 1
    a.report_id = uuid4()
    a.phase = "edge_discovery"
    a.action_type = "create_edge"
    a.memory_id = uuid4()
    a.target_id = uuid4()
    a.details = {"edge_type": "related_to"}
    a.created_at = datetime(2026, 4, 6, 3, 1, 0)
    return a


@pytest.fixture
def client(monkeypatch):
    """TestClient that exposes monkeypatch + auto-clears overrides."""

    class _ClientWithPatch:
        def __init__(self, tc, mp):
            self._tc = tc
            self.monkeypatch = mp

        def __getattr__(self, name):
            return getattr(self._tc, name)

    yield _ClientWithPatch(TestClient(app, raise_server_exceptions=False), monkeypatch)
    app.dependency_overrides.clear()


def _install_workspace_overrides(
    client,
    *,
    user: dict,
    db_mock=None,
    permission_raises: HTTPException | None = None,
):
    """Wire mocks for the workspace route."""

    async def mock_user():
        return user

    async def mock_db():
        yield db_mock if db_mock is not None else AsyncMock()

    app.dependency_overrides[get_current_user] = mock_user
    app.dependency_overrides[get_db] = mock_db

    if permission_raises is not None:

        async def _mock_check_reject(_self, _user_id, _workspace_id):
            raise permission_raises

        client.monkeypatch.setattr(
            PermissionService,
            "check_workspace_admin",
            _mock_check_reject,
        )
    else:

        async def _mock_check_allow(_self, _user_id, _workspace_id):
            return MagicMock()

        client.monkeypatch.setattr(
            PermissionService,
            "check_workspace_admin",
            _mock_check_allow,
        )


# ============================================================================
# Workspace list route
# ============================================================================


class TestWorkspaceListSleepReports:
    """GET /api/v1/workspaces/{workspace_id}/sleep-reports"""

    def test_owner_sees_workspace_reports(self, client):
        reports = [_make_mock_report() for _ in range(3)]

        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 3
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        # No context query because all context_ids are None
        mock_db.execute.side_effect = [count_result, list_result]

        _install_workspace_overrides(client, user=_owner_user(), db_mock=mock_db)

        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert len(data["reports"]) == 3
        # All reports belong to the requested workspace
        for r in data["reports"]:
            assert r["workspace_id"] == str(_WORKSPACE_ID)

    def test_non_owner_gets_403(self, client):
        _install_workspace_overrides(
            client,
            user=_regular_user(),
            permission_raises=HTTPException(
                status_code=403,
                detail="Requires 'admin' role or higher",
            ),
        )

        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports")
        assert response.status_code == 403

    def test_cross_workspace_probe_gets_403(self, client):
        """A caller hitting a workspace they aren't a member of must 403."""
        _install_workspace_overrides(
            client,
            user=_regular_user(),
            permission_raises=HTTPException(
                status_code=403,
                detail="Not a member of workspace",
            ),
        )

        response = client.get(f"/api/v1/workspaces/{_OTHER_WORKSPACE_ID}/sleep-reports")
        assert response.status_code == 403

    def test_invalid_status_returns_400(self, client):
        _install_workspace_overrides(client, user=_owner_user())

        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports?status=bogus")
        assert response.status_code == 400
        assert "Invalid status" in response.json()["detail"]

    def test_filters_by_status(self, client):
        reports = [_make_mock_report(status="failed")]

        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        mock_db.execute.side_effect = [count_result, list_result]

        _install_workspace_overrides(client, user=_owner_user(), db_mock=mock_db)

        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports?status=failed")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["reports"][0]["status"] == "failed"

    def test_respects_limit_and_offset(self, client):
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 100
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [count_result, list_result]

        _install_workspace_overrides(client, user=_owner_user(), db_mock=mock_db)

        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports?limit=25&offset=50"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 25
        assert data["offset"] == 50
        assert data["total"] == 100


# ============================================================================
# Workspace detail route
# ============================================================================


class TestWorkspaceGetSleepReportDetail:
    """GET /api/v1/workspaces/{workspace_id}/sleep-reports/{id}"""

    def test_owner_sees_report_detail(self, client):
        report = _make_mock_report()
        actions = [_make_mock_action()]

        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = actions
        # No context query because context_id is None
        mock_db.execute.side_effect = [report_result, actions_result]

        _install_workspace_overrides(client, user=_owner_user(), db_mock=mock_db)

        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports/{report.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["action_count"] == 1
        assert data["report"]["memories_processed"] == 7

    def test_report_from_other_workspace_returns_404(self, client):
        """Cross-workspace report probe returns 404 (not 403) — CWE-639 uniform disclosure."""
        report = _make_mock_report(workspace_id=_OTHER_WORKSPACE_ID)

        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [report_result, actions_result]

        _install_workspace_overrides(client, user=_owner_user(), db_mock=mock_db)

        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports/{report.id}")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_not_found_returns_404(self, client):
        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = None
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        # Sequential await: only the report query runs when report is missing
        mock_db.execute.side_effect = [report_result]

        _install_workspace_overrides(client, user=_owner_user(), db_mock=mock_db)

        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports/{uuid4()}")
        assert response.status_code == 404

    def test_non_owner_gets_403(self, client):
        _install_workspace_overrides(
            client,
            user=_regular_user(),
            permission_raises=HTTPException(
                status_code=403,
                detail="Requires 'admin' role or higher",
            ),
        )

        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports/{uuid4()}")
        assert response.status_code == 403


# ============================================================================
# Anonymous access
# ============================================================================


class TestAnonymousAccess:
    def test_list_rejects_anonymous(self, client):
        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports")
        assert response.status_code == 401

    def test_detail_rejects_anonymous(self, client):
        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/sleep-reports/{uuid4()}")
        assert response.status_code == 401
