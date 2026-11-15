"""Query validation and gate of the admin workspace list and one-workspace read (#2841).

No database: a refused request never reaches the handler (the session is a
``MagicMock``, so a handler that used it would answer 500), and the
service-mode test answers the handler's own 404 from a session that finds no
row.
"""

from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from config.settings import get_settings
from db.base import get_db

LIST = "/api/v1/admin/plans/workspaces"
ADMIN = {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}


@pytest.fixture
def client():
    async def _admin():
        return dict(ADMIN)

    async def _no_db():
        yield MagicMock()

    app.dependency_overrides[require_admin] = _admin
    app.dependency_overrides[get_db] = _no_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "501"},
        {"limit": "-1"},
        {"offset": "-1"},
        {"plan": "enterprise"},
        {"plan": "PRO"},
        {"plan": ""},
        {"search": "x" * 256},
        {"search": "a\x00b"},
        {"search": "a\tb"},
        {"search": "a\x7fb"},
        {"owner_user_id": ""},
        {"owner_user_id": "u" * 256},
    ],
    ids=lambda params: "&".join(f"{key}={value!r:.12}" for key, value in params.items()),
)
def test_list_refuses_a_bad_value_with_422(client: TestClient, params: dict) -> None:
    response = client.get(LIST, params=params)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "VAL-001"
    # The refusal names the parameter and never repeats the value.
    for value in params.values():
        if len(value) > 3:
            assert value not in response.text


def test_a_refused_search_is_not_logged(client: TestClient) -> None:
    sentinel = "refused-search-sentinel"
    with patch("api.main.logger") as logger:
        response = client.get(LIST, params={"search": f"{sentinel}\x00"})
    assert response.status_code == 422, response.text
    assert logger.mock_calls
    assert sentinel not in repr(logger.mock_calls)
    assert sentinel not in response.text


def test_one_workspace_read_refuses_an_id_that_is_not_a_uuid(client: TestClient) -> None:
    response = client.get(f"{LIST}/not-a-uuid")
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"


def test_one_workspace_read_is_reachable_in_service_mode(monkeypatch) -> None:
    """An operator principal (no ``users`` row) reaches the handler: the 404 is
    the handler's own answer for a workspace the session does not find."""
    token = secrets.token_hex(32)
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "service")
    monkeypatch.setattr(settings, "system_admin_service_token", token)
    monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
    monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")

    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.rollback = AsyncMock()

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            f"{LIST}/00000000-0000-4000-8000-000000000001",
            headers={"Authorization": f"Bearer {token}", "X-Operator-Email": "ops@example.com"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404, response.text
    assert response.json()["error"] == "RES-001"
    assert response.json()["message"] == "Workspace not found"
    assert db.execute.await_count == 1
