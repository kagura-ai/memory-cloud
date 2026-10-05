"""Tests for cost aggregation API routes (Issue #472).

Covers:

- Admin route: admin sees all (200), non-admin gets 403, anonymous gets 401
- Workspace route: owner/admin sees their workspace, non-owner gets 403,
  anonymous gets 401, cross-workspace probe gets 403, query-string
  ``workspace_id`` cannot override the path-bound value
- Query parsing on BOTH routes: invalid ``period`` / ``source`` / ``paid_by``
  return 400; missing required ``from`` / ``to`` return 422
- Response shape: rows, breakdowns, JSON wire format

The SQL pipeline is exercised end-to-end in
``tests/integration/test_cost_aggregation_service.py``; here we mock the
service so the route layer can be unit-tested without Docker/Postgres.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_current_user, require_admin
from db.base import get_db
from services.cost_aggregation_service import (
    CostAggregationRow,
    CostBreakdownByModel,
    CostBreakdownBySource,
)
from services.permission_service import PermissionService
from utils.exceptions import AuthorizationError

_WORKSPACE_ID = uuid4()


def _admin_user() -> dict:
    return {"user_id": "admin_1", "email": "admin@test.com", "role": "admin"}


def _regular_user() -> dict:
    return {"user_id": "user_1", "email": "user@test.com", "role": "user"}


def _mock_session() -> AsyncMock:
    """A session whose queries return no rows: the aggregate is mocked and the
    #1861 label lookups (workspace names, user emails) resolve nothing."""
    result = MagicMock()
    result.all.return_value = []
    return AsyncMock(execute=AsyncMock(return_value=result))


def _make_canned_row() -> CostAggregationRow:
    """A representative row covering both breakdowns."""
    row = CostAggregationRow(
        period_start=date(2026, 4, 5),
        workspace_id=_WORKSPACE_ID,
        user_id="user_1",
    )
    row.calls = 150
    row.tokens_in = 18_000
    row.tokens_out = 3_000
    row.tokens_cached_in = 5_000
    row.tokens_cache_write = 0
    row.embedding_tokens = 1_000_000
    row.cost_usd = 0.073
    row.cost_usd_byok = 0.0
    row.cost_breakdown_by_model = [
        CostBreakdownByModel("claude-sonnet-4-6", 100, 0.06, 0.0),
        CostBreakdownByModel("claude-haiku-4-5", 50, 0.013, 0.0),
    ]
    row.cost_breakdown_by_source = [
        CostBreakdownBySource("sleep", 150, 0.073, 0.0),
    ]
    return row


@pytest.fixture
def client(monkeypatch):
    """TestClient that exposes monkeypatch + auto-clears overrides.

    ``monkeypatch`` is bundled into the fixture so the helpers below
    (``_install_*``) can patch ``CostAggregationService.aggregate`` /
    ``PermissionService.check_workspace_admin`` per-test without
    leaking the patched method into other test modules — the failure
    mode that originally surfaced when the integration tests ran in
    the same session as these route tests.
    """

    class _ClientWithPatch:
        def __init__(self, tc, mp):
            self._tc = tc
            self.monkeypatch = mp

        def __getattr__(self, name):
            return getattr(self._tc, name)

    yield _ClientWithPatch(TestClient(app, raise_server_exceptions=False), monkeypatch)
    app.dependency_overrides.clear()


def _install_admin_overrides(client, canned_rows: list[CostAggregationRow]) -> AsyncMock:
    """Wire mocks for the admin route: admin auth + db that won't be hit.

    The aggregate() call is intercepted by patching CostAggregationService
    via the import path the route uses.
    """

    async def mock_admin():
        return _admin_user()

    async def mock_db():
        yield _mock_session()

    app.dependency_overrides[require_admin] = mock_admin
    app.dependency_overrides[get_db] = mock_db
    return _patch_service(client, canned_rows)


def _install_workspace_overrides(
    client,
    canned_rows: list[CostAggregationRow],
    *,
    user: dict,
    permission_raises: AuthorizationError | None = None,
) -> AsyncMock:
    """Wire mocks for the workspace route.

    ``permission_raises`` lets a test simulate the auth gate rejecting
    the caller (cross-workspace probe, role-too-low, deleted workspace
    — the route should propagate the exception unchanged).
    """

    async def mock_user():
        return user

    async def mock_db():
        yield _mock_session()

    app.dependency_overrides[get_current_user] = mock_user
    app.dependency_overrides[get_db] = mock_db

    # Patch PermissionService.check_workspace_admin so the test does not
    # need real workspace/membership rows. The route always instantiates
    # PermissionService(db); we stub the method on the class.
    if permission_raises is not None:

        async def mock_check_workspace_admin(self, user_id, workspace_id):
            raise permission_raises
    else:

        async def mock_check_workspace_admin(self, user_id, workspace_id):
            return MagicMock()  # the WorkspaceMember return value is unused by route

    client.monkeypatch.setattr(
        PermissionService,
        "check_workspace_admin",
        mock_check_workspace_admin,
    )
    return _patch_service(client, canned_rows)


def _patch_service(client, canned_rows: list[CostAggregationRow]) -> AsyncMock:
    """Replace ``CostAggregationService.aggregate`` with an async mock.

    Uses ``monkeypatch`` so the patched class attribute is restored at
    test teardown; without this the AsyncMock leaks into the integration
    suite and breaks any test that exercises the real SQL pipeline.
    """
    import services.cost_aggregation_service as svc_module

    mock_aggregate = AsyncMock(return_value=canned_rows)
    client.monkeypatch.setattr(svc_module.CostAggregationService, "aggregate", mock_aggregate)
    return mock_aggregate


# ============================================================================
# Admin route
# ============================================================================


class TestAdminRoute:
    def test_returns_aggregated_rows(self, client):
        mock_aggregate = _install_admin_overrides(client, [_make_canned_row()])

        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["rows"]) == 1
        row = data["rows"][0]
        assert row["period_start"] == "2026-04-05"
        assert row["workspace_id"] == str(_WORKSPACE_ID)
        assert row["calls"] == 150
        assert row["cost_usd"] == 0.073
        assert row["cost_usd_byok"] == 0.0
        assert {b["model"] for b in row["cost_breakdown_by_model"]} == {
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        }
        # Service was called with the parsed window.
        assert mock_aggregate.await_count == 1
        kwargs = mock_aggregate.await_args.kwargs
        assert kwargs["period"] == "day"
        # End is exclusive: from=2026-04-01 to=2026-04-07 → end = 2026-04-08T00:00.
        assert kwargs["start"].date() == date(2026, 4, 1)
        assert kwargs["end"].date() == date(2026, 4, 8)

    def test_non_admin_gets_403(self, client):
        # The admin route uses require_admin which raises 403 for non-admin
        # roles. Override get_current_user (which require_admin depends on)
        # to return a regular user, and let require_admin do its job.
        async def mock_user():
            return _regular_user()

        async def mock_db():
            yield _mock_session()

        app.dependency_overrides[get_current_user] = mock_user
        app.dependency_overrides[get_db] = mock_db

        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 403

    def test_invalid_period_returns_400(self, client):
        _install_admin_overrides(client, [])
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=hour&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 400
        assert "Invalid period" in response.json()["message"]

    def test_invalid_source_returns_400(self, client):
        _install_admin_overrides(client, [])
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07&source=manual"
        )
        assert response.status_code == 400
        assert "Invalid source" in response.json()["message"]

    def test_invalid_paid_by_returns_400(self, client):
        _install_admin_overrides(client, [])
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07&paid_by=user"
        )
        assert response.status_code == 400
        assert "Invalid paid_by" in response.json()["message"]

    def test_inverted_window_returns_400(self, client):
        _install_admin_overrides(client, [])
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-08&to=2026-04-01"
        )
        assert response.status_code == 400

    def test_window_exceeding_cap_returns_400(self, client):
        # A non-UI caller bypassing the frontend's 365-day soft cap must be
        # rejected server-side (defense-in-depth, #528). 2026-01-01..2027-01-01
        # is 366 inclusive days — one past the boundary.
        mock_aggregate = _install_admin_overrides(client, [])
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-01-01&to=2027-01-01"
        )
        assert response.status_code == 400
        assert "exceeds" in response.json()["message"]
        assert mock_aggregate.await_count == 0  # rejected before SQL

    def test_window_at_cap_boundary_returns_200(self, client):
        # Exactly 365 inclusive days (2026 is not a leap year) is the UI's
        # maximum permitted window and must pass — pins the off-by-one so the
        # server cap never rejects a selection the UI allows.
        mock_aggregate = _install_admin_overrides(client, [])
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-01-01&to=2026-12-31"
        )
        assert response.status_code == 200, response.text
        assert mock_aggregate.await_count == 1

    def test_missing_required_query_returns_422(self, client):
        _install_admin_overrides(client, [])
        response = client.get("/api/v1/admin/cost-aggregation?period=day")
        assert response.status_code == 422  # missing from/to

    def test_filter_passthrough_to_service(self, client):
        mock_aggregate = _install_admin_overrides(client, [])
        response = client.get(
            "/api/v1/admin/cost-aggregation"
            "?period=week&from=2026-04-01&to=2026-04-30"
            f"&workspace_id={_WORKSPACE_ID}&user_id=user_2"
            "&source=analysis&paid_by=byok"
        )
        assert response.status_code == 200
        kwargs = mock_aggregate.await_args.kwargs
        assert kwargs["period"] == "week"
        assert kwargs["workspace_id"] == _WORKSPACE_ID
        assert kwargs["user_id"] == "user_2"
        assert kwargs["source"] == "analysis"
        assert kwargs["paid_by"] == "byok"


# ============================================================================
# Workspace-scoped route
# ============================================================================


class TestWorkspaceRoute:
    def test_owner_sees_workspace_rows(self, client):
        mock_aggregate = _install_workspace_overrides(
            client, [_make_canned_row()], user=_regular_user()
        )
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["rows"]) == 1
        # Service was called with the path-bound workspace_id (cannot be overridden).
        kwargs = mock_aggregate.await_args.kwargs
        assert kwargs["workspace_id"] == _WORKSPACE_ID

    def test_non_owner_gets_403(self, client):
        # Simulate check_workspace_admin raising 403 for a non-owner.
        _install_workspace_overrides(
            client,
            [],
            user=_regular_user(),
            permission_raises=AuthorizationError("Insufficient permissions"),
        )
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 403

    def test_cross_workspace_probe_gets_403(self, client):
        # A caller hitting a workspace they aren't a member of must 403,
        # not leak rows. The check_workspace_admin gate raises 403 BEFORE
        # the SQL fires, so this test asserts the route propagates the
        # exception and never invokes the service.
        mock_aggregate = _install_workspace_overrides(
            client,
            [_make_canned_row()],  # would leak if route ran the query
            user=_regular_user(),
            permission_raises=AuthorizationError("Insufficient permissions"),
        )
        other_workspace_id = uuid4()
        response = client.get(
            f"/api/v1/workspaces/{other_workspace_id}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 403
        assert mock_aggregate.await_count == 0  # no SQL was issued

    def test_workspace_route_ignores_workspace_id_query_param(self, client):
        # The workspace route does not declare a ``workspace_id`` query
        # arg — only a path arg. Even if a malicious client appends one
        # to the URL, the path-bound value wins (FastAPI ignores unknown
        # query params by default).
        mock_aggregate = _install_workspace_overrides(
            client, [_make_canned_row()], user=_regular_user()
        )
        other_workspace_id = uuid4()
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            f"?period=day&from=2026-04-01&to=2026-04-07&workspace_id={other_workspace_id}"
        )
        assert response.status_code == 200
        kwargs = mock_aggregate.await_args.kwargs
        assert kwargs["workspace_id"] == _WORKSPACE_ID  # path wins

    def test_invalid_period_returns_400(self, client):
        _install_workspace_overrides(client, [], user=_regular_user())
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=hour&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 400

    def test_invalid_source_returns_400(self, client):
        _install_workspace_overrides(client, [], user=_regular_user())
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07&source=manual"
        )
        assert response.status_code == 400
        assert "Invalid source" in response.json()["message"]

    def test_invalid_paid_by_returns_400(self, client):
        _install_workspace_overrides(client, [], user=_regular_user())
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07&paid_by=user"
        )
        assert response.status_code == 400
        assert "Invalid paid_by" in response.json()["message"]

    def test_window_exceeding_cap_returns_400(self, client):
        # Same defense-in-depth cap on the workspace-scoped route (#528).
        # 2026-01-01..2027-01-01 is 366 inclusive days — past the boundary.
        mock_aggregate = _install_workspace_overrides(client, [], user=_regular_user())
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-01-01&to=2027-01-01"
        )
        assert response.status_code == 400
        assert "exceeds" in response.json()["message"]
        assert mock_aggregate.await_count == 0  # rejected before SQL


# ============================================================================
# Anonymous-access regression — both routes
# ============================================================================


class TestAnonymousAccess:
    """Verify both routes reject unauthenticated callers (covers the auth gate
    BEFORE any service or query-parsing logic runs).

    These tests do NOT install dependency overrides for ``get_current_user``
    or ``require_admin`` — the FastAPI dependency raises 401 the same way it
    would for a real anonymous request (no session cookie, no API key).
    """

    def test_admin_route_rejects_anonymous(self, client):
        # No auth dep override → get_current_user / require_admin raises
        # the standard 401 from the auth middleware path.
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 401

    def test_workspace_route_rejects_anonymous(self, client):
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 401


# ============================================================================
# #1861: workspace / user labels
# ============================================================================


class TestRowLabels:
    def test_rows_carry_label_fields_even_when_nothing_resolves(self, client):
        _install_admin_overrides(client, [_make_canned_row()])
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 200, response.text
        row = response.json()["rows"][0]
        assert row["workspace_name"] is None  # the mock session resolves nothing
        assert row["user_email"] is None

    def test_labels_are_resolved_once_per_response_and_attached(self, client):
        import api.routes.cost_aggregation as route_module

        rows = [_make_canned_row(), _make_canned_row()]
        rows[1].user_id = "svc:connector"  # no users row → stays unlabelled
        _install_admin_overrides(client, rows)
        calls: list[int] = []

        async def fake_labels(db, rows_in):
            calls.append(len(rows_in))
            return {_WORKSPACE_ID: "Team Alpha"}, {"user_1": "alice@example.com"}

        client.monkeypatch.setattr(route_module, "_resolve_row_labels", fake_labels)
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 200, response.text
        data = response.json()["rows"]
        assert calls == [2]  # one batch for the whole response, not per row
        assert (data[0]["workspace_name"], data[0]["user_email"]) == (
            "Team Alpha",
            "alice@example.com",
        )
        assert (data[1]["workspace_name"], data[1]["user_email"]) == ("Team Alpha", None)

    @pytest.mark.asyncio
    async def test_resolver_batches_workspaces_and_users(self):
        """Two queries for any number of rows; unknown ids are simply absent."""
        import api.routes.cost_aggregation as route_module

        rows = [_make_canned_row(), _make_canned_row()]
        rows[1].user_id = "user_2"
        ws_result = MagicMock()
        ws_result.all.return_value = [(_WORKSPACE_ID, "Team Alpha")]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=ws_result)
        with patch(
            "services.sleep_reporter_service.SleepReporterService.resolve_user_labels",
            new=AsyncMock(return_value={"user_1": "alice@example.com"}),
        ) as users:
            names, emails = await route_module._resolve_row_labels(db, rows)
        assert names == {_WORKSPACE_ID: "Team Alpha"}
        assert emails == {"user_1": "alice@example.com"}
        assert db.execute.await_count == 1
        users.assert_awaited_once_with({"user_1", "user_2"})

    @pytest.mark.asyncio
    async def test_resolver_skips_the_workspace_query_when_no_row_has_one(self):
        import api.routes.cost_aggregation as route_module

        row = _make_canned_row()
        row.workspace_id = None
        db = AsyncMock()
        with patch(
            "services.sleep_reporter_service.SleepReporterService.resolve_user_labels",
            new=AsyncMock(return_value={}),
        ):
            names, emails = await route_module._resolve_row_labels(db, [row])
        assert (names, emails) == ({}, {})
        db.execute.assert_not_awaited()

    def test_workspace_route_attaches_the_labels_too(self, client):
        import api.routes.cost_aggregation as route_module

        _install_workspace_overrides(client, [_make_canned_row()], user=_regular_user())

        async def fake_labels(db, rows_in):
            return {_WORKSPACE_ID: "Team Alpha"}, {"user_1": "alice@example.com"}

        client.monkeypatch.setattr(route_module, "_resolve_row_labels", fake_labels)
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 200, response.text
        row = response.json()["rows"][0]
        assert (row["workspace_name"], row["user_email"]) == ("Team Alpha", "alice@example.com")
