"""Route-level tests for GET /api/v1/memory-health (#2764).

The per-context memory-health report moved out of the system-admin surface:
any signed-in user reads the report for the contexts they created, through a
browser session only. Covers the session-only gate (no session -> 401, an API
key or an OAuth bearer token -> 403 without reaching the service), a caller
who is not a system admin getting both response shapes, the caller's own id
reaching the service, the uniform 404 for a context created by someone else,
the 'unattributed' sentinel, the 422 for malformed scopes and the HEALTH-001
guard.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_session_auth
from db.base import get_db

SERVICE = "api.routes.memory_health.MemoryHealthService"
URL = "/api/v1/memory-health"

_CTX = str(uuid.uuid4())

CANNED_BREAKDOWN = {
    "generated_at": "2026-07-12T00:00:00Z",
    "overall_status": "warn",
    "contexts": [
        {
            "context_id": _CTX,
            "name": "Context A",
            "overall_status": "warn",
            "sections": {"consolidation": "warn", "graph": "ok", "retrieval": "ok"},
        },
        {
            "context_id": None,
            "name": None,
            "overall_status": "ok",
            "sections": {"consolidation": "ok", "graph": "ok", "retrieval": "ok"},
        },
    ],
}

CANNED_DETAIL = {
    "generated_at": "2026-07-12T00:00:00Z",
    "context_id": _CTX,
    "context_name": "Context A",
    "overall_status": "warn",
    "sections": {
        "consolidation": {
            "status": "warn",
            "metrics": {"reports_in_window": 3, "llm_call_failures": 2},
            "notes": [{"code": "judge_failures", "params": {"count": 2, "degraded_runs": 1}}],
        },
        "graph": {"status": "ok", "metrics": {"total_edges": 5}, "notes": []},
        "retrieval": {"status": "ok", "metrics": {"recall_calls": 7}, "notes": []},
    },
}


async def _mock_get_db():
    yield AsyncMock()


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _as_member(user_id: str = "member_user_1") -> None:
    """A signed-in user who is NOT a system admin."""

    async def member():
        return {"user_id": user_id, "email": "member@test.com", "role": "member"}

    app.dependency_overrides[require_session_auth] = member
    app.dependency_overrides[get_db] = _mock_get_db


class TestSessionOnlyGate:
    def test_no_session_gets_401(self, client) -> None:
        app.dependency_overrides[get_db] = _mock_get_db
        called = AsyncMock()
        with patch(f"{SERVICE}.build_breakdown", new=called):
            resp = client.get(URL)
        assert resp.status_code == 401
        called.assert_not_awaited()

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param("kagura_not_a_real_key", id="api-key"),
            # OAuth access tokens are 43-char token_urlsafe strings without
            # the kagura_ prefix (auth.oauth2_bearer.is_oauth_bearer_token).
            pytest.param("o" * 43, id="oauth-bearer"),
        ],
    )
    def test_bearer_credentials_are_refused(self, client, token) -> None:
        app.dependency_overrides[get_db] = _mock_get_db
        breakdown = AsyncMock()
        detail = AsyncMock()
        with (
            patch(f"{SERVICE}.build_breakdown", new=breakdown),
            patch(f"{SERVICE}.build_context_report", new=detail),
        ):
            resp = client.get(URL, headers={"Authorization": f"Bearer {token}"})
            resp_detail = client.get(
                f"{URL}?context_id={_CTX}", headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 403
        assert resp_detail.status_code == 403
        breakdown.assert_not_awaited()
        detail.assert_not_awaited()


class TestNonAdminCaller:
    def test_breakdown_for_a_non_admin_caller(self, client) -> None:
        _as_member("member_user_1")
        with patch(
            f"{SERVICE}.build_breakdown", new=AsyncMock(return_value=CANNED_BREAKDOWN)
        ) as mocked:
            resp = client.get(URL)

        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_status"] == "warn"
        assert body["contexts"][0]["context_id"] == _CTX
        assert body["contexts"][0]["sections"] == {
            "consolidation": "warn",
            "graph": "ok",
            "retrieval": "ok",
        }
        # The unattributed entry serializes with null identity fields.
        assert body["contexts"][1]["context_id"] is None
        assert body["contexts"][1]["name"] is None
        # The report is the caller's own: the service gets the caller's id.
        assert mocked.await_args.args[0] == "member_user_1"

    def test_detail_for_a_non_admin_caller(self, client) -> None:
        _as_member("member_user_1")
        with patch(
            f"{SERVICE}.build_context_report", new=AsyncMock(return_value=CANNED_DETAIL)
        ) as mocked:
            resp = client.get(f"{URL}?context_id={_CTX}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["context_id"] == _CTX
        assert body["context_name"] == "Context A"
        assert set(body["sections"]) == {"consolidation", "graph", "retrieval"}
        note = body["sections"]["consolidation"]["notes"][0]
        assert note == {"code": "judge_failures", "params": {"count": 2, "degraded_runs": 1}}
        assert mocked.await_args.args[0] == "member_user_1"
        assert mocked.await_args.args[1] == uuid.UUID(_CTX)

    def test_context_created_by_someone_else_gets_uniform_404(self, client) -> None:
        """The service answers None for a context the caller did not create;
        the body is the same as for an id that does not exist."""
        _as_member()
        with patch(f"{SERVICE}.build_context_report", new=AsyncMock(return_value=None)):
            someone_elses = client.get(f"{URL}?context_id={_CTX}")
            unknown = client.get(f"{URL}?context_id={uuid.uuid4()}")

        assert someone_elses.status_code == 404
        assert unknown.status_code == 404
        assert someone_elses.json() == unknown.json()
        assert "Context not found" in someone_elses.text

    def test_unattributed_sentinel_maps_to_null_scope(self, client) -> None:
        _as_member()
        canned = {**CANNED_DETAIL, "context_id": None, "context_name": None}
        with patch(f"{SERVICE}.build_context_report", new=AsyncMock(return_value=canned)) as mocked:
            resp = client.get(f"{URL}?context_id=unattributed")

        assert resp.status_code == 200
        assert resp.json()["context_id"] is None
        assert mocked.await_args.args[1] is None

    def test_malformed_context_id_gets_422(self, client) -> None:
        _as_member()
        resp = client.get(f"{URL}?context_id=not-a-uuid")
        assert resp.status_code == 422

    def test_missing_user_id_yields_health_001(self, client) -> None:
        async def user_without_id():
            return {"email": "member@test.com", "role": "member"}

        app.dependency_overrides[require_session_auth] = user_without_id
        app.dependency_overrides[get_db] = _mock_get_db

        resp = client.get(URL)
        assert resp.status_code == 500
        assert "HEALTH-001" in resp.text
        # The old text named an admin; the route is no longer admin-only.
        assert "Admin" not in resp.text


class TestOpenAPI:
    def test_new_route_is_documented_and_not_deprecated(self) -> None:
        operation = app.openapi()["paths"]["/api/v1/memory-health"]["get"]
        assert operation["tags"] == ["memory-health"]
        assert not operation.get("deprecated", False)
