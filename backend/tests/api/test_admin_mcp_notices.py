"""``/api/v1/admin/mcp-notices``: create, list, withdraw service notices (#2762).

The service runs against Postgres in
``tests/integration/test_mcp_notice_service_db.py``; here it is stubbed so the
routes are tested through the real app:

- create answers 201 with the notice; the actor is the session admin, or
  ``operator:<address>`` in ``service`` mode; the commit happens; a WARNING
  ``mcp_notice_created`` names the actor, the id and the character count,
  never the text;
- every refusal is typed (MCPNOTICE-001/002/003/007, 404 MCPNOTICE-004) and
  never echoes the text; the body is strict (unknown field, naive time → 422);
- the list reports ``delivering`` (``MCP_NOTICES_ENABLED``) and passes the
  state filter and limit; the routes work while delivery is off;
- withdraw is idempotent and logs only when it changed something.

The gate in every ``SYSTEM_ADMIN_MODE`` is pinned by
``test_system_admin_surface.py`` (ADMIN_SURFACE).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import admin_mcp_notices as routes
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from services.mcp_notice_service import NoticeView
from utils.exceptions import (
    McpNoticeActiveLimitError,
    McpNoticeDailyLimitError,
    McpNoticeNotFoundError,
    McpNoticeTextRejectedError,
    McpNoticeWindowInvalidError,
)

URL = "/api/v1/admin/mcp-notices"
ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
OPERATOR = "ops@example.com"
NOTICE_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
T0 = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 21, 2, 0, tzinfo=UTC)
SECRET_TEXT = "Maintenance window on Saturday."


def _view(**overrides) -> NoticeView:
    values = dict(
        id=NOTICE_ID,
        text=SECRET_TEXT,
        starts_at=T0,
        expires_at=T1,
        withdrawn_at=None,
        created_at=T0,
        state="active",
        delivered_count=0,
    )
    values.update(overrides)
    return NoticeView(**values)  # type: ignore[arg-type]


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    yield session
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client(db):
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def session_admin(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "session")

    async def _session():
        return dict(ADMIN_SESSION)

    app.dependency_overrides[get_current_user_optional] = _session


@pytest.fixture
def operator_headers(monkeypatch) -> dict[str, str]:
    token = secrets.token_hex(32)
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "service")
    monkeypatch.setattr(settings, "system_admin_service_token", token)
    monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
    monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")
    app.dependency_overrides.pop(get_current_user_optional, None)
    return {"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}


@pytest.fixture
def log(monkeypatch) -> MagicMock:
    stub = MagicMock()
    monkeypatch.setattr(routes, "logger", stub)
    return stub


@pytest.fixture
def delivering_off(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_notices_enabled", False)


def _body(**overrides) -> dict:
    body = {"text": SECRET_TEXT, "expires_at": T1.isoformat()}
    body.update(overrides)
    return body


class TestCreate:
    def test_created_by_a_session_admin(
        self, client, db, session_admin, log, monkeypatch, delivering_off
    ) -> None:
        create = AsyncMock(return_value=_view())
        monkeypatch.setattr(routes, "create_notice", create)

        response = client.post(URL, json=_body(starts_at=T0.isoformat()))

        assert response.status_code == 201, response.text
        assert response.json() == {
            "id": str(NOTICE_ID),
            "text": SECRET_TEXT,
            "starts_at": "2026-11-20T02:00:00Z",
            "expires_at": "2026-11-21T02:00:00Z",
            "withdrawn_at": None,
            "created_at": "2026-11-20T02:00:00Z",
            "state": "active",
            "delivered_count": 0,
        }
        kwargs = create.await_args.kwargs
        assert kwargs["raw_text"] == SECRET_TEXT
        assert kwargs["starts_at"] == T0 and kwargs["expires_at"] == T1
        assert kwargs["actor"].email == "admin@example.com"
        assert kwargs["actor"].user_id == "1001"
        assert kwargs["actor"].ip_address
        db.commit.assert_awaited_once()
        log.warning.assert_called_once()
        event, fields = log.warning.call_args.args[0], log.warning.call_args.kwargs
        assert event == "mcp_notice_created"
        assert fields == {
            "actor": "admin@example.com",
            "notice_id": str(NOTICE_ID),
            "text_chars": len(SECRET_TEXT),
        }
        assert SECRET_TEXT not in repr(log.mock_calls)

    def test_created_by_an_operator(self, client, operator_headers, log, monkeypatch) -> None:
        create = AsyncMock(return_value=_view())
        monkeypatch.setattr(routes, "create_notice", create)

        response = client.post(URL, json=_body(), headers=operator_headers)

        assert response.status_code == 201, response.text
        actor = create.await_args.kwargs["actor"]
        assert actor.email == actor.user_id == f"operator:{OPERATOR}"
        assert create.await_args.kwargs["starts_at"] is None
        assert log.warning.call_args.kwargs["actor"] == f"operator:{OPERATOR}"

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (McpNoticeTextRejectedError("link"), 422, "MCPNOTICE-001"),
            (McpNoticeWindowInvalidError("too_long", "too long"), 422, "MCPNOTICE-002"),
            (McpNoticeActiveLimitError(3), 409, "MCPNOTICE-003"),
            (McpNoticeDailyLimitError(5), 409, "MCPNOTICE-007"),
        ],
    )
    def test_refusals_are_typed_and_never_echo_the_text(
        self, client, db, session_admin, log, monkeypatch, error, status, code
    ) -> None:
        monkeypatch.setattr(routes, "create_notice", AsyncMock(side_effect=error))

        response = client.post(URL, json=_body())

        assert response.status_code == status
        assert response.json()["error"] == code
        assert SECRET_TEXT not in response.text
        db.commit.assert_not_awaited()
        log.warning.assert_not_called()

    def test_reason_is_in_the_details(self, client, session_admin, monkeypatch) -> None:
        monkeypatch.setattr(
            routes,
            "create_notice",
            AsyncMock(side_effect=McpNoticeTextRejectedError("markup_char")),
        )
        response = client.post(URL, json=_body(text="<b>x</b>"))
        assert response.json()["details"] == {"reason": "markup_char"}
        assert "<b>" not in response.text

    @pytest.mark.parametrize(
        "body",
        [
            _body(extra="field"),
            _body(expires_at="2026-11-21T02:00:00"),
            _body(starts_at="2026-11-21T02:00:00"),
            {"text": SECRET_TEXT},
            {"expires_at": T1.isoformat()},
            _body(text=5),
        ],
    )
    def test_strict_body(self, client, session_admin, monkeypatch, body) -> None:
        create = AsyncMock(return_value=_view())
        monkeypatch.setattr(routes, "create_notice", create)
        response = client.post(URL, json=body)
        assert response.status_code == 422
        create.assert_not_awaited()


class TestList:
    def test_reports_delivering_and_passes_the_filter(
        self, client, session_admin, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "mcp_notices_enabled", True)
        listed = AsyncMock(return_value=[_view(delivered_count=7)])
        monkeypatch.setattr(routes, "list_notices", listed)

        response = client.get(URL, params={"state": "active", "limit": 10})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["delivering"] is True
        assert [n["delivered_count"] for n in body["notices"]] == [7]
        assert listed.await_args.kwargs == {"state": "active", "limit": 10}

    def test_defaults(self, client, session_admin, monkeypatch, delivering_off) -> None:
        listed = AsyncMock(return_value=[])
        monkeypatch.setattr(routes, "list_notices", listed)

        body = client.get(URL).json()

        assert body == {"notices": [], "delivering": False}
        assert listed.await_args.kwargs == {"state": "all", "limit": 50}

    @pytest.mark.parametrize("params", [{"state": "withdrawn"}, {"limit": 0}, {"limit": 101}])
    def test_bad_query(self, client, session_admin, monkeypatch, params) -> None:
        monkeypatch.setattr(routes, "list_notices", AsyncMock(return_value=[]))
        assert client.get(URL, params=params).status_code == 422


class TestWithdraw:
    def test_withdraws_and_logs(self, client, db, session_admin, log, monkeypatch) -> None:
        withdrawn = _view(withdrawn_at=T1, state="ended")
        withdraw = AsyncMock(return_value=(withdrawn, True))
        monkeypatch.setattr(routes, "withdraw_notice", withdraw)

        response = client.post(f"{URL}/{NOTICE_ID}/withdraw")

        assert response.status_code == 200, response.text
        assert response.json()["state"] == "ended"
        assert response.json()["withdrawn_at"] == "2026-11-21T02:00:00Z"
        assert withdraw.await_args.kwargs["notice_id"] == NOTICE_ID
        db.commit.assert_awaited_once()
        assert log.warning.call_args.args[0] == "mcp_notice_withdrawn"
        assert log.warning.call_args.kwargs == {
            "actor": "admin@example.com",
            "notice_id": str(NOTICE_ID),
            "text_chars": len(SECRET_TEXT),
        }

    def test_second_withdraw_is_quiet(self, client, session_admin, log, monkeypatch) -> None:
        withdrawn = _view(withdrawn_at=T1, state="ended")
        monkeypatch.setattr(routes, "withdraw_notice", AsyncMock(return_value=(withdrawn, False)))

        response = client.post(f"{URL}/{NOTICE_ID}/withdraw")

        assert response.status_code == 200
        log.warning.assert_not_called()

    def test_unknown_id(self, client, session_admin, monkeypatch) -> None:
        monkeypatch.setattr(
            routes, "withdraw_notice", AsyncMock(side_effect=McpNoticeNotFoundError())
        )
        response = client.post(f"{URL}/{uuid.uuid4()}/withdraw")
        assert response.status_code == 404
        assert response.json()["error"] == "MCPNOTICE-004"

    def test_malformed_id(self, client, session_admin) -> None:
        assert client.post(f"{URL}/not-a-uuid/withdraw").status_code == 422


def test_no_edit_or_delete_route() -> None:
    from fastapi.routing import iter_route_contexts

    paths = {
        (method, context.path)
        for context in iter_route_contexts(app.routes)
        if context.path.startswith(URL)
        for method in context.methods
    }
    assert paths == {
        ("POST", URL),
        ("GET", URL),
        ("POST", f"{URL}/{{notice_id}}/withdraw"),
    }
