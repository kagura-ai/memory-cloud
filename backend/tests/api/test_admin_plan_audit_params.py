"""Query validation of ``GET /api/v1/admin/plans/audit`` (#2830).

No database: a refused request never reaches the handler (the session is a
``MagicMock``, so a handler that used it would answer 500, which is what a
negative ``limit`` answered before).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db

AUDIT = "/api/v1/admin/plans/audit"


@pytest.fixture
def client():
    async def _admin():
        return {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}

    async def _db():
        yield MagicMock()

    app.dependency_overrides[require_admin] = _admin
    app.dependency_overrides[get_db] = _db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "-1"},
        {"limit": "501"},
        {"offset": "-1"},
        {"workspace_id": "not-a-uuid"},
        {"workspace_id": ""},
    ],
    ids=lambda params: "&".join(f"{key}={value}" for key, value in params.items()),
)
def test_audit_refuses_a_bad_value_with_422_not_500(client: TestClient, params: dict) -> None:
    response = client.get(AUDIT, params=params)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
