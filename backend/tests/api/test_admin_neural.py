"""API tests for admin kNN calibration recalibrate endpoint (#406 Phase B).

Pure HTTP / auth / serialization checks — the task-layer behavior
(``enqueue_recalibration_dedup`` Redis + asyncio mechanics) is covered
separately by ``tests/neural/test_calibration.py`` and the integration
harness. We patch the task module at the endpoint's call site so these
tests don't touch Redis or spawn real asyncio tasks.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db


def _mock_admin_user() -> dict:
    return {"user_id": "admin_user_1", "email": "admin@test.com", "role": "admin"}


@pytest.fixture
def admin_client():
    """TestClient with admin auth override."""

    async def mock_admin():
        return _mock_admin_user()

    async def mock_db():
        yield MagicMock()

    app.dependency_overrides[require_admin] = mock_admin
    app.dependency_overrides[get_db] = mock_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client():
    """TestClient without any auth overrides — exercises the 401 path."""
    # Do not override require_admin; the dependency's real 401 behavior fires.
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestAuth:
    """Copilot review PR #420 / QA advisor finding: endpoint is admin-only."""

    def test_unauthenticated_rejected(self, unauth_client):
        response = unauth_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "text-embedding-3-small", "dimensions": 512},
        )
        # Exact status depends on whether the dep short-circuits before reaching
        # the handler; both 401 and 403 are valid rejection signals.
        assert response.status_code in (401, 403)


class TestInputValidation:
    """FastAPI Query validators + defensive model_name sanitization."""

    def test_empty_model_rejected(self, admin_client):
        response = admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "", "dimensions": 512},
        )
        # min_length=1 on Query → 422 (FastAPI's validation error).
        assert response.status_code == 422

    def test_model_with_path_separator_rejected(self, admin_client):
        response = admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "bad/model", "dimensions": 512},
        )
        # Defensive handler-level check → 400.
        assert response.status_code == 400
        assert response.json()["detail"] == "invalid_model_name"

    def test_model_with_backslash_rejected(self, admin_client):
        response = admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "bad\\model", "dimensions": 512},
        )
        assert response.status_code == 400

    def test_model_with_whitespace_rejected(self, admin_client):
        response = admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "bad model", "dimensions": 512},
        )
        assert response.status_code == 400

    def test_dimensions_below_minimum_rejected(self, admin_client):
        response = admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "text-embedding-3-small", "dimensions": 0},
        )
        # ge=1 on Query → 422.
        assert response.status_code == 422

    def test_dimensions_above_maximum_rejected(self, admin_client):
        response = admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "text-embedding-3-small", "dimensions": 100000},
        )
        # le=65536 on Query → 422.
        assert response.status_code == 422


class TestDedupBehavior:
    """``accepted`` flag reflects the dedup lock outcome — always HTTP 202."""

    def test_accepted_true_spawns_task(self, admin_client, monkeypatch):
        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr("api.routes.admin_neural.enqueue_recalibration_dedup", enqueue)

        response = admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "text-embedding-3-small", "dimensions": 512},
        )

        assert response.status_code == 202
        body = response.json()
        assert body == {
            "accepted": True,
            "model_name": "text-embedding-3-small",
            "dimensions": 512,
        }
        enqueue.assert_awaited_once()

    def test_accepted_false_on_dedup_skip(self, admin_client, monkeypatch):
        """Dedup rejected duplicate → accepted=false, still 202 (idempotent)."""
        enqueue = AsyncMock(return_value=False)
        monkeypatch.setattr("api.routes.admin_neural.enqueue_recalibration_dedup", enqueue)

        response = admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "text-embedding-3-small", "dimensions": 512},
        )

        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] is False
        assert body["model_name"] == "text-embedding-3-small"
        assert body["dimensions"] == 512

    def test_enqueue_called_with_context_id_none(self, admin_client, monkeypatch):
        """Admin endpoint only enqueues model-global jobs (per-context is v2)."""
        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr("api.routes.admin_neural.enqueue_recalibration_dedup", enqueue)

        admin_client.post(
            "/api/v1/admin/neural/recalibrate",
            params={"model": "qwen3-embedding:8b", "dimensions": 4096},
        )

        enqueue.assert_awaited_once_with(
            model_name="qwen3-embedding:8b",
            dimensions=4096,
            context_id=None,
        )
