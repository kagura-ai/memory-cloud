"""``/api/v1/me/mcp-notices``: the per-user opt-in for service notices (#2762).

The contract the account-settings page builds against (fixed for the
frontend run):

- GET -> ``{offered, mcp_notices, answered, source, recorded_at,
  wording_version}``; ``wording_version`` is the version to send back when
  turning it on, null while not offered;
- PUT ``{mcp_notices, wording_version}`` -> the GET shape plus ``recorded``;
  turning it on needs ``MCP_NOTICES_ENABLED`` (409 ``MCPNOTICE-005``) and the
  current wording (409 ``MCPNOTICE-006``); turning it off always works; 20
  requests an hour per user, unchanged answers included (429, fails open); session only (an API key or an
  OAuth bearer is refused, 401 without a session).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import mcp_notices as routes
from auth.dependencies import require_session_auth
from config.settings import get_settings
from db.base import get_db
from services.mcp_notice_service import (
    MCP_NOTICES_WORDING_VERSION as V,
)
from services.mcp_notice_service import (
    McpNoticeConsentRecordResult,
    McpNoticeConsentState,
)
from utils.exceptions import RedisError

URL = "/api/v1/me/mcp-notices"
USER = {"user_id": "u1", "email": "u@example.test"}
WHEN = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
GET_KEYS = {"offered", "mcp_notices", "answered", "source", "recorded_at", "wording_version"}


@pytest.fixture
def enabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_notices_enabled", True)


@pytest.fixture
def disabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_notices_enabled", False)


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture
def client(db):
    async def _db():
        yield db

    async def _user():
        return dict(USER)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_session_auth] = _user
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def counter(monkeypatch) -> AsyncMock:
    stub = AsyncMock(return_value=1)
    monkeypatch.setattr(routes, "increment_counter", stub)
    return stub


@pytest.fixture
def record(monkeypatch) -> AsyncMock:
    async def _record(db, *, user_id, value, wording_version):
        return McpNoticeConsentRecordResult(
            state=McpNoticeConsentState(
                value=value, source="settings", wording_version=wording_version, recorded_at=WHEN
            ),
            recorded=True,
        )

    stub = AsyncMock(side_effect=_record)
    monkeypatch.setattr(routes, "record_mcp_notice_consent", stub)
    return stub


def _current(monkeypatch, state: McpNoticeConsentState | None) -> None:
    monkeypatch.setattr(routes, "current_mcp_notice_consent", AsyncMock(return_value=state))


class TestGet:
    def test_never_answered_while_offered(self, client, enabled, monkeypatch) -> None:
        _current(monkeypatch, None)

        body = client.get(URL).json()

        assert body == {
            "offered": True,
            "mcp_notices": False,
            "answered": False,
            "source": None,
            "recorded_at": None,
            "wording_version": V,
        }

    def test_on_while_not_offered_still_shows_it(self, client, disabled, monkeypatch) -> None:
        _current(
            monkeypatch,
            McpNoticeConsentState(
                value=True, source="settings", wording_version=V, recorded_at=WHEN
            ),
        )

        body = client.get(URL).json()

        assert set(body) == GET_KEYS
        assert body["offered"] is False
        assert body["mcp_notices"] is True
        assert body["answered"] is True
        assert body["source"] == "settings"
        assert body["recorded_at"] == "2026-11-13T09:00:00Z"
        assert body["wording_version"] is None


class TestPut:
    def test_on_records_with_the_version(self, client, db, enabled, counter, record) -> None:
        response = client.put(URL, json={"mcp_notices": True, "wording_version": V})

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == GET_KEYS | {"recorded"}
        assert body["mcp_notices"] is True and body["recorded"] is True
        assert body["answered"] is True and body["source"] == "settings"
        assert body["wording_version"] == V
        assert record.await_args.kwargs == {"user_id": "u1", "value": True, "wording_version": V}
        db.commit.assert_awaited_once()
        assert counter.await_args.args[0] == "mcp_notice_pref_user:u1"

    def test_off_records_without_a_version(self, client, enabled, counter, record) -> None:
        response = client.put(URL, json={"mcp_notices": False, "wording_version": V})

        assert response.status_code == 200
        assert record.await_args.kwargs["wording_version"] is None

    def test_off_works_while_not_offered(self, client, disabled, counter, record) -> None:
        response = client.put(URL, json={"mcp_notices": False, "wording_version": None})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["offered"] is False and body["mcp_notices"] is False
        assert body["wording_version"] is None

    def test_on_while_not_offered_is_409(self, client, db, disabled, counter, record) -> None:
        response = client.put(URL, json={"mcp_notices": True, "wording_version": V})

        assert response.status_code == 409
        assert response.json()["error"] == "MCPNOTICE-005"
        record.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.parametrize("version", [None, "", "2020-01", V + "x"])
    def test_on_with_stale_wording_is_409(self, client, enabled, counter, record, version) -> None:
        response = client.put(URL, json={"mcp_notices": True, "wording_version": version})

        assert response.status_code == 409
        assert response.json()["error"] == "MCPNOTICE-006"
        record.assert_not_awaited()

    def test_per_user_limit(self, client, enabled, record, monkeypatch) -> None:
        monkeypatch.setattr(routes, "increment_counter", AsyncMock(return_value=21))

        response = client.put(URL, json={"mcp_notices": False, "wording_version": None})

        assert response.status_code == 429
        record.assert_not_awaited()

    def test_redis_outage_fails_open(self, client, enabled, record, monkeypatch) -> None:
        monkeypatch.setattr(routes, "increment_counter", AsyncMock(side_effect=RedisError("x")))

        response = client.put(URL, json={"mcp_notices": False, "wording_version": None})

        assert response.status_code == 200
        assert response.json()["recorded"] is True

    @pytest.mark.parametrize(
        "body",
        [
            {"mcp_notices": "true", "wording_version": V},
            {"mcp_notices": 1, "wording_version": V},
            {"wording_version": V},
            {"mcp_notices": True, "wording_version": "x" * 65},
            {"mcp_notices": True, "wording_version": V, "extra": 1},
        ],
    )
    def test_strict_body(self, client, enabled, counter, record, body) -> None:
        assert client.put(URL, json=body).status_code == 422
        record.assert_not_awaited()


class TestSessionOnly:
    @pytest.mark.parametrize("token", ["kagura_not_a_real_key", "oauth-access-token-value"])
    def test_bearer_credentials_are_refused(self, token) -> None:
        client = TestClient(app, raise_server_exceptions=False)
        for method in ("get", "put"):
            response = getattr(client, method)(
                URL,
                headers={"Authorization": f"Bearer {token}"},
                **(
                    {"json": {"mcp_notices": True, "wording_version": V}} if method == "put" else {}
                ),
            )
            assert response.status_code in (401, 403), (method, token)

    def test_the_routes_depend_on_the_session_dependency_only(self) -> None:
        """Structural: the bearer test above gets 401 from an invalid token
        whatever the dependency is, so pin the dependency itself. Each route
        takes ``require_session_auth`` and nothing in its dependency tree
        accepts an API key or an OAuth bearer."""
        from fastapi.routing import APIRoute

        from auth.dependencies import get_user_from_api_key_or_session

        def calls(dependant) -> set:
            found = set()
            for dep in dependant.dependencies:
                found.add(dep.call)
                found |= calls(dep)
            return found

        routes_here = [r for r in routes.router.routes if isinstance(r, APIRoute)]
        assert {m for r in routes_here for m in r.methods} == {"GET", "PUT"}
        for route in routes_here:
            direct = {d.call for d in route.dependant.dependencies}
            assert require_session_auth in direct, route.methods
            assert get_user_from_api_key_or_session not in calls(route.dependant)

    def test_no_session_is_401(self) -> None:
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get(URL).status_code == 401
        assert client.put(URL, json={"mcp_notices": False}).status_code == 401
