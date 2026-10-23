"""Route test for POST /admin/users/{user_id}/erase when the vector store is
down (#2282).

DB-free: dependency overrides + a patched service, with the app's real
``MemoryCloudException`` handler, so the 503 body is the one clients get.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.main import memory_cloud_exception_handler
from api.routes import admin as admin_mod
from auth.dependencies import get_current_user
from db.base import get_db
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import ErasureRetryScheduledError, MemoryCloudException

ADMIN = "admin_user_1"
TARGET = "target_user_1"
_PATH = f"/admin/users/{TARGET}/erase"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_mod.router)
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)  # type: ignore[arg-type]

    async def _user():
        return {"user_id": ADMIN, "email": "admin@test.com", "role": "admin"}

    async def _db():
        yield MagicMock()

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def test_a_vector_store_outage_answers_503_with_the_request_and_the_retry_time():
    scheduled_for = utcnow() + timedelta(minutes=15)
    service = MagicMock()
    service.admin_force_erase = AsyncMock(
        side_effect=ErasureRetryScheduledError(
            request_id="5f0c8f0e-0000-4000-8000-000000000001",
            attempt=1,
            max_attempts=4,
            scheduled_for=scheduled_for,
            error_class="QdrantError",
        )
    )

    with patch("services.account_erasure_service.AccountErasureService", return_value=service):
        resp = _client().post(_PATH, json={"reason_code": "legal_order"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "ERASURE-007"
    assert body["details"] == {
        "request_id": "5f0c8f0e-0000-4000-8000-000000000001",
        "attempt": 1,
        "max_attempts": 4,
        "scheduled_for": to_utc_iso(scheduled_for),
        "error_class": "QdrantError",
    }
    assert to_utc_iso(scheduled_for) in body["message"]
    assert TARGET not in resp.text
    service.admin_force_erase.assert_awaited_once()
    assert service.admin_force_erase.await_args.kwargs["target_user_id"] == TARGET
