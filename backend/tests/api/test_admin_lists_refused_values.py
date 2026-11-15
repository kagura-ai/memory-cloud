"""Values the database would refuse never reach a statement (#2841, #2831, #2830, #2836).

No database: the session is a ``MagicMock`` that records nothing, so a request
that reached a handler would answer 500. A refused request answers 422 from
the parameter's declaration, and neither the answer nor a log line repeats
the value.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db

WORKSPACES = "/api/v1/admin/plans/workspaces"
AUDIT = "/api/v1/admin/plans/audit"
ALLOWLIST = "/api/v1/admin/signup-gate/allowlist"
REFERRALS = "/api/v1/admin/referrals"


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


@pytest.mark.parametrize(
    "url",
    [
        # past what the database takes as an OFFSET
        f"{WORKSPACES}?offset=9223372036854775808",
        f"{WORKSPACES}?offset=99999999999999999999",
        f"{AUDIT}?offset=9223372036854775808",
        f"{ALLOWLIST}?offset=9223372036854775808",
        f"{REFERRALS}?offset=9223372036854775808",
        # a NUL is in no stored value
        f"{WORKSPACES}?owner_user_id=github:a%00b",
        f"{WORKSPACES}?owner_user_id=%00",
        f"{REFERRALS}?referred_user_id=github:a%00b",
        f"{REFERRALS}?referred_user_id=%00",
        f"{REFERRALS}?referrer_user_id=github:a%00b",
        f"{REFERRALS}?referrer_user_id=%00",
    ],
    ids=lambda url: url.removeprefix("/api/v1/admin/")[:48],
)
def test_a_value_the_database_would_refuse_is_422_before_any_statement(
    client: TestClient, session: MagicMock, url: str
) -> None:
    response = client.get(url)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "VAL-001"
    assert session.mock_calls == []


@pytest.mark.parametrize(
    "query",
    [
        f"{WORKSPACES}?owner_user_id={{value}}%00",
        f"{WORKSPACES}?owner_user_id={{value}}&offset=9223372036854775808",
        f"{REFERRALS}?referred_user_id={{value}}%00",
        f"{REFERRALS}?referrer_user_id={{value}}%00&offset=9223372036854775808",
    ],
    ids=["owner-nul", "owner-offset", "referred-nul", "referrer-nul-offset"],
)
def test_a_refused_filter_value_is_not_echoed_or_logged(client: TestClient, query: str) -> None:
    value = "refused-filter-sentinel@example.com"
    with (
        patch("api.main.logger") as app_logger,
        patch("api.routes.admin_plans.logger") as plans_logger,
        patch("api.routes.admin_referrals.logger") as referrals_logger,
    ):
        response = client.get(query.format(value=value))
    assert response.status_code == 422, response.text
    assert value not in response.text
    assert app_logger.mock_calls
    logged = repr([*app_logger.mock_calls, *plans_logger.mock_calls, *referrals_logger.mock_calls])
    assert value not in logged
