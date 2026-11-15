"""A NUL on the admin delete and erase routes answers 422 before any statement (#2857).

``DELETE /api/v1/admin/users/{user_id}`` and ``POST /api/v1/admin/users/{user_id}/erase``
declare ``NO_NUL_PATTERN`` on the path ``user_id``, and the erase body on
``reason_detail``. No database: the session is a ``MagicMock`` that records
nothing, so a request that reached a handler would answer 500. Neither the
answer nor a log line repeats the value.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db

USERS = "/api/v1/admin/users"
ERASE_BODY = {"reason_code": "user_request_via_support"}
SENTINEL = "refused-sentinel-2857"


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


@pytest.fixture
def client(session: MagicMock):
    async def _admin():
        return {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}

    async def _db():
        yield session

    app.dependency_overrides[require_admin] = _admin
    app.dependency_overrides[get_db] = _db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _send(client: TestClient, request: str, value: str):
    if request == "delete":
        return client.delete(f"{USERS}/{value}%00")
    if request == "erase-path":
        return client.post(f"{USERS}/{value}%00/erase", json=ERASE_BODY)
    return client.post(
        f"{USERS}/local:target/erase", json={**ERASE_BODY, "reason_detail": f"{value}\x00"}
    )


@pytest.mark.parametrize("request_kind", ["delete", "erase-path", "erase-reason-detail"])
def test_a_nul_on_the_admin_delete_and_erase_routes_is_422_before_any_statement(
    client: TestClient, session: MagicMock, request_kind: str
) -> None:
    response = _send(client, request_kind, "local:a")
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
    assert session.mock_calls == []


@pytest.mark.parametrize("request_kind", ["delete", "erase-path", "erase-reason-detail"])
def test_a_refused_nul_value_is_not_echoed(client: TestClient, request_kind: str) -> None:
    response = _send(client, request_kind, SENTINEL)
    assert response.status_code == 422, response.text
    assert SENTINEL not in response.text


def test_a_refused_reason_detail_is_not_logged(client: TestClient) -> None:
    # The validation log line names the request path, which holds a path
    # value; a body value is in no log line.
    with patch("api.main.logger") as app_logger, patch("api.routes.admin.logger") as admin_logger:
        response = _send(client, "erase-reason-detail", SENTINEL)
    assert response.status_code == 422, response.text
    assert app_logger.mock_calls
    assert SENTINEL not in repr([*app_logger.mock_calls, *admin_logger.mock_calls])


def test_a_reason_detail_without_a_nul_still_reaches_the_erasure(client: TestClient) -> None:
    record = MagicMock(id="r-1", status="complete", deleted_data_summary={})
    erase = AsyncMock(return_value=record)
    with patch("services.account_erasure_service.AccountErasureService.admin_force_erase", erase):
        response = client.post(
            f"{USERS}/local:target/erase",
            json={**ERASE_BODY, "reason_detail": "line one\nline two\ttabbed"},
        )
    assert response.status_code == 200, response.text
    assert erase.await_args.kwargs["reason_detail"] == "line one\nline two\ttabbed"
    assert erase.await_args.kwargs["target_user_id"] == "local:target"
