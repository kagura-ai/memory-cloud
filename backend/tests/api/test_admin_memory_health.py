"""Route-level tests for GET /api/v1/admin/memory-health (#1211, #1225).

Covers the admin gate (403 for non-admin), the two 200 response shapes
through FastAPI serialization (breakdown vs ?context_id detail — the
dict-to-model boundary the service unit tests can't see), the uniform 404
for un-owned contexts, the 'unattributed' sentinel, the 422 for malformed
scopes, and the HEALTH-001 guard.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_current_user, require_admin
from db.base import get_db

_CTX = str(uuid.uuid4())

_CANNED_BREAKDOWN = {
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

_CANNED_DETAIL = {
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


def _admin_user() -> dict:
    return {"user_id": "admin_user_1", "email": "admin@test.com", "role": "admin"}


async def _mock_get_db():
    yield AsyncMock()


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _as_admin():
    async def admin():
        return _admin_user()

    app.dependency_overrides[require_admin] = admin
    app.dependency_overrides[get_db] = _mock_get_db


class TestAdminMemoryHealthRoute:
    def test_non_admin_gets_403(self, client) -> None:
        async def member_user():
            return {"user_id": "u1", "email": "member@test.com", "role": "member"}

        app.dependency_overrides[get_current_user] = member_user
        app.dependency_overrides[get_db] = _mock_get_db

        resp = client.get("/api/v1/admin/memory-health")
        assert resp.status_code == 403

    def test_breakdown_shape_without_context_id(self, client) -> None:
        _as_admin()
        with patch(
            "api.routes.admin_memory_health.MemoryHealthService.build_breakdown",
            new=AsyncMock(return_value=_CANNED_BREAKDOWN),
        ):
            resp = client.get("/api/v1/admin/memory-health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_status"] == "warn"
        assert len(body["contexts"]) == 2
        assert body["contexts"][0]["context_id"] == _CTX
        assert body["contexts"][0]["sections"] == {
            "consolidation": "warn",
            "graph": "ok",
            "retrieval": "ok",
        }
        # The unattributed entry serializes with null identity fields.
        assert body["contexts"][1]["context_id"] is None
        assert body["contexts"][1]["name"] is None

    def test_detail_shape_with_context_id(self, client) -> None:
        _as_admin()
        with patch(
            "api.routes.admin_memory_health.MemoryHealthService.build_context_report",
            new=AsyncMock(return_value=_CANNED_DETAIL),
        ) as mocked:
            resp = client.get(f"/api/v1/admin/memory-health?context_id={_CTX}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["context_id"] == _CTX
        assert body["context_name"] == "Context A"
        assert set(body["sections"]) == {"consolidation", "graph", "retrieval"}
        note = body["sections"]["consolidation"]["notes"][0]
        assert note == {"code": "judge_failures", "params": {"count": 2, "degraded_runs": 1}}
        # The route parses the query param into a UUID scope.
        assert mocked.await_args.args[1] == uuid.UUID(_CTX)

    def test_unattributed_sentinel_maps_to_null_scope(self, client) -> None:
        _as_admin()
        canned = {**_CANNED_DETAIL, "context_id": None, "context_name": None}
        with patch(
            "api.routes.admin_memory_health.MemoryHealthService.build_context_report",
            new=AsyncMock(return_value=canned),
        ) as mocked:
            resp = client.get("/api/v1/admin/memory-health?context_id=unattributed")

        assert resp.status_code == 200
        assert resp.json()["context_id"] is None
        assert mocked.await_args.args[1] is None

    def test_unowned_context_gets_uniform_404(self, client) -> None:
        _as_admin()
        with patch(
            "api.routes.admin_memory_health.MemoryHealthService.build_context_report",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get(f"/api/v1/admin/memory-health?context_id={uuid.uuid4()}")

        assert resp.status_code == 404
        assert "Context not found" in resp.text

    def test_malformed_context_id_gets_422(self, client) -> None:
        _as_admin()
        resp = client.get("/api/v1/admin/memory-health?context_id=not-a-uuid")
        assert resp.status_code == 422

    def test_missing_user_id_yields_health_001(self, client) -> None:
        async def admin_without_id():
            return {"email": "admin@test.com", "role": "admin"}

        app.dependency_overrides[require_admin] = admin_without_id
        app.dependency_overrides[get_db] = _mock_get_db

        resp = client.get("/api/v1/admin/memory-health")
        assert resp.status_code == 500
        assert "HEALTH-001" in resp.text
