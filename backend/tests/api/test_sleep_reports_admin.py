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
    source: str = "sleep",
):
    r = MagicMock()
    r.id = uuid4()
    r.user_id = user_id
    r.workspace_id = uuid4()
    r.context_id = uuid4() if context_id is _SENTINEL else context_id
    r.status = status
    r.source = source  # #2219
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
    r.merge_retention_result = None  # #1209
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


def _user_result(rows=(("local:admin", "admin@test.com"),)):
    """Mock result for the #1201 batch ``user_id → email`` query."""
    r = MagicMock()
    r.all.return_value = list(rows)
    return r


def _ws_result(rows=()):
    """Mock result for the #2844 batch ``workspace_id → name`` query (live
    workspaces only; no row = a soft-deleted or missing workspace)."""
    r = MagicMock()
    r.all.return_value = list(rows)
    return r


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
        mock_db.execute.side_effect = [
            count_result,
            list_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

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
        assert data["reports"][0]["user_email"] == "admin@test.com"

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
        mock_db.execute.side_effect = [
            count_result,
            list_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

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
        assert "Invalid status" in response.json()["message"]

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
        # No context batch query (all context_ids None); then the workspace
        # name and the user queries.
        mock_db.execute.side_effect = [count_result, list_result, _ws_result(), _user_result()]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        data = response.json()
        assert data["reports"][0]["context_name"] is None
        # 4 DB calls: count + list + workspace names + user resolution
        # (context batch skipped)
        assert mock_db.execute.call_count == 4

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
        mock_db.execute.side_effect = [
            count_result,
            list_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

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
        mock_db.execute.side_effect = [
            count_result,
            list_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        assert response.json()["reports"][0]["context_name"] is None
        # 5 DB calls: count + list + context batch + workspace names + user resolution
        assert mock_db.execute.call_count == 5

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
        mock_db.execute.side_effect = [
            count_result,
            list_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")
        assert response.status_code == 200
        assert response.json()["reports"][0]["context_name"] is None


def _list_sql(mock_db) -> tuple[str, dict]:
    """SQL text and bound params of the list query (the second execute call)."""
    stmt = mock_db.execute.await_args_list[1].args[0]
    compiled = stmt.compile()
    return str(compiled), dict(compiled.params)


class TestListSleepReportsSourceFilter:
    """#2219: ``source`` filter (sleep | analysis | all, default all)."""

    def _db(self, reports):
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = len(reports)
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        mock_db.execute.side_effect = [count_result, list_result, _ws_result(), _user_result()]
        return mock_db

    def test_default_lists_every_source_and_rows_carry_source(self, client):
        mock_db = self._db(
            [
                _make_mock_report(context_id=None, source="sleep"),
                _make_mock_report(context_id=None, source="analysis"),
            ]
        )
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")

        assert response.status_code == 200
        assert [r["source"] for r in response.json()["reports"]] == ["sleep", "analysis"]
        sql, _ = _list_sql(mock_db)
        assert "sleep_reports.source =" not in sql

    @pytest.mark.parametrize("source", ["sleep", "analysis"])
    def test_source_narrows_the_query(self, client, source):
        mock_db = self._db([_make_mock_report(context_id=None, source=source)])
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports?source={source}")

        assert response.status_code == 200
        assert response.json()["reports"][0]["source"] == source
        sql, params = _list_sql(mock_db)
        assert "sleep_reports.source =" in sql
        assert source in params.values()
        count_sql = str(mock_db.execute.await_args_list[0].args[0])
        assert "sleep_reports.source =" in count_sql

    def test_all_does_not_filter(self, client):
        mock_db = self._db([_make_mock_report(context_id=None)])
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports?source=all")

        assert response.status_code == 200
        sql, _ = _list_sql(mock_db)
        assert "sleep_reports.source =" not in sql

    def test_unknown_source_returns_422(self, client):
        mock_db = AsyncMock()
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports?source=bogus")

        assert response.status_code == 422
        mock_db.execute.assert_not_awaited()


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
        mock_db.execute.side_effect = [
            report_result,
            actions_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["action_count"] == 2
        assert data["report"]["memories_processed"] == 7
        assert data["report"]["context_name"] == "Kagura Dev"
        assert data["report"]["context_deleted"] is False
        assert data["report"]["user_email"] == "admin@test.com"
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
        mock_db.execute.side_effect = [
            report_result,
            actions_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

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
        mock_db.execute.side_effect = [
            report_result,
            actions_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

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
        mock_db.execute.side_effect = [
            report_result,
            actions_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

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
        # No Context query (context_id None); then the user query.
        mock_db.execute.side_effect = [report_result, actions_result, _ws_result(), _user_result()]

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
        # Sequential await: only the report query runs when report is missing
        mock_db.execute.side_effect = [report_result]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{uuid4()}")
        assert response.status_code == 404
        assert "not found" in response.json()["message"].lower()

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
        mock_db.execute.side_effect = [
            report_result,
            actions_result,
            ctx_result,
            _ws_result(),
            _user_result(),
        ]

        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["action_count"] == 0
        assert data["actions"] == []


class TestAdminSleepReportsStayUnscoped:
    """#1882: only the workspace routes narrow emails to current members.
    The system-admin routes keep resolving every ``users`` row."""

    def _label_sql(self, mock_db) -> str:
        return str(mock_db.execute.await_args_list[-1].args[0])

    def test_list_label_query_has_no_membership_filter(self, client):
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = [_make_mock_report(context_id=None)]
        mock_db.execute.side_effect = [count_result, list_result, _ws_result(), _user_result()]
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")

        assert response.status_code == 200
        assert response.json()["reports"][0]["user_email"] == "admin@test.com"
        sql = self._label_sql(mock_db)
        assert "users" in sql
        assert "workspace_members" not in sql

    def test_detail_label_query_has_no_membership_filter(self, client):
        report = _make_mock_report(context_id=None)
        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [report_result, actions_result, _ws_result(), _user_result()]
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")

        assert response.status_code == 200
        assert response.json()["report"]["user_email"] == "admin@test.com"
        sql = self._label_sql(mock_db)
        assert "users" in sql
        assert "workspace_members" not in sql


def _statements(mock_db) -> list[str]:
    return [str(call.args[0].compile()) for call in mock_db.execute.await_args_list]


def _bound(mock_db, index: int) -> list:
    """Bound values of one executed statement, IN lists flattened."""
    values: list = []
    for value in mock_db.execute.await_args_list[index].args[0].compile().params.values():
        values.extend(value if isinstance(value, list | tuple | set) else [value])
    return values


class TestListSleepReportsWorkspaceFilter:
    """#2844: ``workspace_id`` filter on the admin list."""

    def _db(self, reports, total=None):
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = len(reports) if total is None else total
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        effects = [count_result, list_result]
        if reports:
            effects += [_ws_result(), _user_result()]
        mock_db.execute.side_effect = effects
        return mock_db

    def test_workspace_id_narrows_the_list_and_the_total(self, client):
        report = _make_mock_report(context_id=None)
        mock_db = self._db([report], total=1)
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports?workspace_id={report.workspace_id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert [r["workspace_id"] for r in body["reports"]] == [str(report.workspace_id)]
        count_sql, list_sql = _statements(mock_db)[:2]
        assert "sleep_reports.workspace_id =" in count_sql
        assert "sleep_reports.workspace_id =" in list_sql
        assert report.workspace_id in _bound(mock_db, 0)
        assert report.workspace_id in _bound(mock_db, 1)

    @pytest.mark.parametrize(
        ("other", "column", "value"),
        [
            ("status=failed", "sleep_reports.status =", "failed"),
            ("source=analysis", "sleep_reports.source =", "analysis"),
            ("user_id=local:someone", "sleep_reports.user_id =", "local:someone"),
        ],
    )
    def test_workspace_id_is_anded_with_another_filter(self, client, other, column, value):
        workspace_id = uuid4()
        mock_db = self._db([])
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports?workspace_id={workspace_id}&{other}")

        assert response.status_code == 200, response.text
        for index in (0, 1):
            sql = _statements(mock_db)[index]
            assert "sleep_reports.workspace_id =" in sql
            assert column in sql
            assert " OR " not in sql
            assert workspace_id in _bound(mock_db, index)
            assert value in _bound(mock_db, index)

    def test_workspace_id_is_anded_with_context_id(self, client):
        workspace_id, context_id = uuid4(), uuid4()
        mock_db = self._db([])
        _install_overrides(mock_db)

        response = client.get(
            f"/api/v1/admin/sleep-reports?workspace_id={workspace_id}&context_id={context_id}"
        )

        assert response.status_code == 200, response.text
        for index in (0, 1):
            sql = _statements(mock_db)[index]
            assert "sleep_reports.workspace_id =" in sql
            assert "sleep_reports.context_id =" in sql
            assert " OR " not in sql
            assert {workspace_id, context_id} <= set(_bound(mock_db, index))

    def test_workspace_id_that_is_not_a_uuid_is_422(self, client):
        mock_db = AsyncMock()
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports?workspace_id=not-a-uuid")

        assert response.status_code == 422
        mock_db.execute.assert_not_awaited()

    @pytest.mark.parametrize(
        "query",
        [
            # past what the database takes as an OFFSET
            "offset=9223372036854775808",
            "offset=99999999999999999999",
            # a NUL is in no stored id and is refused before the statement
            "user_id=local:a%00b",
            "user_id=%00",
        ],
    )
    def test_a_value_the_database_would_refuse_is_422(self, client, query):
        mock_db = AsyncMock()
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports?{query}")

        assert response.status_code == 422, response.text
        assert response.json()["error"] == "VAL-001"
        mock_db.execute.assert_not_awaited()

    def test_a_refused_filter_value_is_not_echoed_or_logged(self, client):
        from structlog.testing import capture_logs

        mock_db = AsyncMock()
        _install_overrides(mock_db)
        user_id = f"local:{uuid4().hex}"

        with capture_logs() as logs:
            response = client.get(
                f"/api/v1/admin/sleep-reports?user_id={user_id}%00&offset=9223372036854775808"
            )

        assert response.status_code == 422, response.text
        assert user_id not in response.text
        assert user_id not in repr(logs)
        mock_db.execute.assert_not_awaited()

    def test_an_id_with_no_report_answers_an_empty_list(self, client):
        mock_db = self._db([], total=0)
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports?workspace_id={uuid4()}&limit=10")

        assert response.status_code == 200, response.text
        assert response.json() == {"reports": [], "total": 0, "limit": 10, "offset": 0}
        # Nothing to label: no lookup beyond the count and the page.
        assert mock_db.execute.await_count == 2

    def test_without_the_parameter_the_query_is_the_one_before(self, client):
        mock_db = self._db([_make_mock_report(context_id=None)])
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")

        assert response.status_code == 200, response.text
        count_sql, list_sql = _statements(mock_db)[:2]
        assert "WHERE" not in count_sql
        assert "WHERE" not in list_sql
        assert "ORDER BY sleep_reports.started_at DESC" in list_sql


class TestSleepReportWorkspaceName:
    """#2844: ``workspace_name`` on the admin list rows and the admin detail."""

    def test_list_rows_name_live_workspaces_and_null_the_rest(self, client):
        live = _make_mock_report(context_id=None)
        gone = _make_mock_report(context_id=None)
        none = _make_mock_report(context_id=None)
        none.workspace_id = None
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 3
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = [live, gone, none]
        # The lookup answers live workspaces only: ``gone`` has no row.
        mock_db.execute.side_effect = [
            count_result,
            list_result,
            _ws_result([(live.workspace_id, "Team Alpha")]),
            _user_result(),
        ]
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")

        assert response.status_code == 200, response.text
        rows = response.json()["reports"]
        assert [r["workspace_name"] for r in rows] == ["Team Alpha", None, None]
        assert [r["workspace_id"] for r in rows] == [
            str(live.workspace_id),
            str(gone.workspace_id),
            None,
        ]

    def test_list_names_are_resolved_in_one_query(self, client):
        reports = [_make_mock_report(context_id=None) for _ in range(5)]
        reports[4].workspace_id = reports[0].workspace_id
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 5
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = reports
        mock_db.execute.side_effect = [count_result, list_result, _ws_result(), _user_result()]
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")

        assert response.status_code == 200, response.text
        on_workspaces = [
            index for index, sql in enumerate(_statements(mock_db)) if "FROM workspaces" in sql
        ]
        assert len(on_workspaces) == 1
        sql = _statements(mock_db)[on_workspaces[0]]
        assert "workspaces.deleted_at IS NULL" in sql
        # Only the id and the name are read: no owner, member or plan column.
        assert sql.split("FROM")[0].strip() == "SELECT workspaces.id, workspaces.name"
        assert set(_bound(mock_db, on_workspaces[0])) == {r.workspace_id for r in reports}
        assert len(_bound(mock_db, on_workspaces[0])) == 4

    def test_list_without_any_workspace_id_runs_no_name_query(self, client):
        report = _make_mock_report(context_id=None)
        report.workspace_id = None
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = [report]
        mock_db.execute.side_effect = [count_result, list_result, _user_result()]
        _install_overrides(mock_db)

        response = client.get("/api/v1/admin/sleep-reports")

        assert response.status_code == 200, response.text
        assert response.json()["reports"][0]["workspace_name"] is None
        assert not [sql for sql in _statements(mock_db) if "FROM workspaces" in sql]

    def _detail_db(self, report, ws_rows):
        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        actions_result = MagicMock()
        actions_result.scalars.return_value.all.return_value = []
        effects = [report_result, actions_result]
        if report.workspace_id is not None:
            effects.append(_ws_result(ws_rows))
        effects.append(_user_result())
        mock_db.execute.side_effect = effects
        return mock_db

    def test_detail_names_a_live_workspace(self, client):
        report = _make_mock_report(context_id=None)
        mock_db = self._detail_db(report, [(report.workspace_id, "Team Alpha")])
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")

        assert response.status_code == 200, response.text
        assert response.json()["report"]["workspace_name"] == "Team Alpha"
        assert len([sql for sql in _statements(mock_db) if "FROM workspaces" in sql]) == 1

    def test_detail_is_null_for_a_deleted_or_missing_workspace(self, client):
        report = _make_mock_report(context_id=None)
        _install_overrides(self._detail_db(report, []))

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")

        assert response.status_code == 200, response.text
        assert response.json()["report"]["workspace_name"] is None
        assert response.json()["report"]["workspace_id"] == str(report.workspace_id)

    def test_detail_is_null_for_a_report_without_a_workspace_id(self, client):
        report = _make_mock_report(context_id=None)
        report.workspace_id = None
        mock_db = self._detail_db(report, [])
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}")

        assert response.status_code == 200, response.text
        assert response.json()["report"]["workspace_name"] is None
        assert not [sql for sql in _statements(mock_db) if "FROM workspaces" in sql]

    def test_detail_without_actions_names_the_workspace_too(self, client):
        report = _make_mock_report(context_id=None)
        mock_db = AsyncMock()
        report_result = MagicMock()
        report_result.scalar_one_or_none.return_value = report
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        mock_db.execute.side_effect = [
            report_result,
            count_result,
            _ws_result([(report.workspace_id, "Team Alpha")]),
            _user_result(),
        ]
        _install_overrides(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}?include_actions=false")

        assert response.status_code == 200, response.text
        assert response.json()["report"]["workspace_name"] == "Team Alpha"


def test_openapi_lists_the_workspace_id_parameter_and_the_workspace_name_field():
    """#2844: both are in the committed OpenAPI snapshot."""
    import json
    from pathlib import Path

    snapshot = json.loads(
        (Path(__file__).parent / "fixtures" / "openapi_schema_snapshot.json").read_text()
    )
    parameters = snapshot["paths"]["/api/v1/admin/sleep-reports"]["get"]["parameters"]
    assert "workspace_id" in {p["name"] for p in parameters}
    schemas = snapshot["components"]["schemas"]
    assert "workspace_name" in schemas["SleepReportSummary"]["properties"]
    assert "workspace_name" in schemas["SleepReportDetail"]["properties"]


class TestSleepReportWorkspaceFilterGate:
    """#2844: the list answers a session admin and a service operator as before."""

    def _db(self, report):
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = [report]
        mock_db.execute.side_effect = [
            count_result,
            list_result,
            _ws_result([(report.workspace_id, "Team Alpha")]),
            _user_result(),
        ]
        return mock_db

    @pytest.fixture
    def gate(self, monkeypatch):
        from config.settings import get_settings

        def _apply(mode: str, token: str = "") -> None:
            settings = get_settings()
            monkeypatch.setattr(settings, "system_admin_mode", mode)
            monkeypatch.setattr(settings, "system_admin_service_token", token)
            monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
            monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")

        return _apply

    def _only_db(self, mock_db):
        async def mock_get_db():
            yield mock_db

        app.dependency_overrides[get_db] = mock_get_db

    def test_session_mode_answers_a_system_admin(self, client, gate):
        from auth.dependencies import get_current_user_optional

        gate("session")
        report = _make_mock_report(context_id=None)
        self._only_db(self._db(report))
        app.dependency_overrides[get_current_user_optional] = _mock_admin_user

        response = client.get(f"/api/v1/admin/sleep-reports?workspace_id={report.workspace_id}")

        assert response.status_code == 200, response.text
        assert response.json()["reports"][0]["workspace_name"] == "Team Alpha"

    def test_service_mode_answers_an_operator(self, client, gate):
        import secrets

        token = secrets.token_hex(32)
        gate("service", token)
        report = _make_mock_report(context_id=None)
        self._only_db(self._db(report))

        response = client.get(
            f"/api/v1/admin/sleep-reports?workspace_id={report.workspace_id}",
            headers={"Authorization": f"Bearer {token}", "X-Operator-Email": "ops@example.com"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["reports"][0]["workspace_name"] == "Team Alpha"
