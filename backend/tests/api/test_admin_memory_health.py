"""Route-level tests for GET /api/v1/admin/memory-health (#1211).

Covers the admin gate (403 for non-admin), the 200 response shape through
FastAPI serialization (MemoryHealthResponse(**report) — the dict-to-model
boundary the service unit tests can't see), and the HEALTH-001 guard.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_current_user, require_admin
from db.base import get_db

_CANNED_REPORT = {
    "generated_at": "2026-07-08T00:00:00Z",
    "overall_status": "warn",
    "sections": {
        "consolidation": {
            "status": "warn",
            "metrics": {"reports_in_window": 3, "llm_call_failures": 2},
            "notes": ["judge failures in the window: 2"],
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


class TestAdminMemoryHealthRoute:
    def test_non_admin_gets_403(self, client) -> None:
        async def member_user():
            return {"user_id": "u1", "email": "member@test.com", "role": "member"}

        app.dependency_overrides[get_current_user] = member_user
        app.dependency_overrides[get_db] = _mock_get_db

        resp = client.get("/api/v1/admin/memory-health")
        assert resp.status_code == 403

    def test_admin_gets_200_report_shape(self, client) -> None:
        async def admin():
            return _admin_user()

        app.dependency_overrides[require_admin] = admin
        app.dependency_overrides[get_db] = _mock_get_db

        with patch(
            "api.routes.admin_memory_health.MemoryHealthService.build_report",
            new=AsyncMock(return_value=_CANNED_REPORT),
        ):
            resp = client.get("/api/v1/admin/memory-health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_status"] == "warn"
        assert body["generated_at"] == "2026-07-08T00:00:00Z"
        assert set(body["sections"]) == {"consolidation", "graph", "retrieval"}
        assert body["sections"]["consolidation"]["notes"] == ["judge failures in the window: 2"]
        assert body["sections"]["graph"]["metrics"]["total_edges"] == 5

    def test_missing_user_id_yields_health_001(self, client) -> None:
        async def admin_without_id():
            return {"email": "admin@test.com", "role": "admin"}

        app.dependency_overrides[require_admin] = admin_without_id
        app.dependency_overrides[get_db] = _mock_get_db

        resp = client.get("/api/v1/admin/memory-health")
        assert resp.status_code == 500
        assert "HEALTH-001" in resp.text
