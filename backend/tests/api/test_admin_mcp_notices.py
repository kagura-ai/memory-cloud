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
- withdraw is idempotent and logs only when it changed something;
- ``plans`` (#2800): absent or ``null`` is everyone; a list goes to the
  service in tier order as plain strings and comes back on create, list and
  withdraw; ``[]``, an unknown, miscased, padded or repeated name, a
  non-string entry and a bare string answer 422 ``VAL-001`` under
  ``["body", "plans", ...]`` without echoing the input, and nothing is
  written.

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
    values = {
        "id": NOTICE_ID,
        "text": SECRET_TEXT,
        "starts_at": T0,
        "expires_at": T1,
        "withdrawn_at": None,
        "created_at": T0,
        "state": "active",
        "delivered_count": 0,
        "plans": None,
    }
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
            "plans": None,
        }
        kwargs = create.await_args.kwargs
        assert kwargs["raw_text"] == SECRET_TEXT
        assert kwargs["starts_at"] == T0 and kwargs["expires_at"] == T1
        assert kwargs["actor"].email == "admin@example.com"
        assert kwargs["actor"].user_id == "1001"
        assert kwargs["actor"].ip_address
        assert kwargs["plans"] is None
        db.commit.assert_awaited_once()
        log.warning.assert_called_once()
        event, fields = log.warning.call_args.args[0], log.warning.call_args.kwargs
        assert event == "mcp_notice_created"
        assert fields == {
            "actor": "admin@example.com",
            "notice_id": str(NOTICE_ID),
            "text_chars": len(SECRET_TEXT),
            "plans": None,
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


class TestPlans:
    """#2800: target a notice by plan."""

    def test_plans_go_to_the_service_in_tier_order_as_plain_strings(
        self, client, db, session_admin, log, monkeypatch
    ) -> None:
        create = AsyncMock(return_value=_view(plans=["pro", "promax"]))
        monkeypatch.setattr(routes, "create_notice", create)

        response = client.post(URL, json=_body(plans=["promax", "pro"]))

        assert response.status_code == 201, response.text
        plans = create.await_args.kwargs["plans"]
        assert plans == ["pro", "promax"]
        assert all(type(p) is str for p in plans)
        assert response.json()["plans"] == ["pro", "promax"]
        db.commit.assert_awaited_once()
        assert log.warning.call_args.args[0] == "mcp_notice_created"
        assert log.warning.call_args.kwargs["plans"] == ["pro", "promax"]

    def test_all_four_tiers_stay_a_list(self, client, session_admin, monkeypatch) -> None:
        create = AsyncMock(return_value=_view(plans=["free", "basic", "pro", "promax"]))
        monkeypatch.setattr(routes, "create_notice", create)

        response = client.post(URL, json=_body(plans=["promax", "free", "pro", "basic"]))

        assert response.status_code == 201, response.text
        assert create.await_args.kwargs["plans"] == ["free", "basic", "pro", "promax"]

    @pytest.mark.parametrize("body", [_body(), _body(plans=None)], ids=["absent", "null"])
    def test_absent_or_null_is_everyone(self, client, session_admin, monkeypatch, body) -> None:
        create = AsyncMock(return_value=_view())
        monkeypatch.setattr(routes, "create_notice", create)

        response = client.post(URL, json=body)

        assert response.status_code == 201, response.text
        assert create.await_args.kwargs["plans"] is None
        assert response.json()["plans"] is None

    def test_the_list_maps_plans(self, client, session_admin, monkeypatch) -> None:
        listed = AsyncMock(return_value=[_view(plans=["pro"]), _view(plans=None)])
        monkeypatch.setattr(routes, "list_notices", listed)

        body = client.get(URL).json()

        assert [n["plans"] for n in body["notices"]] == [["pro"], None]

    def test_withdraw_maps_plans(self, client, session_admin, monkeypatch) -> None:
        withdrawn = _view(withdrawn_at=T1, state="ended", plans=["basic", "pro"])
        monkeypatch.setattr(routes, "withdraw_notice", AsyncMock(return_value=(withdrawn, True)))

        response = client.post(f"{URL}/{NOTICE_ID}/withdraw")

        assert response.status_code == 200, response.text
        assert response.json()["plans"] == ["basic", "pro"]

    @pytest.mark.parametrize(
        ("plans", "lists_the_keys"),
        [
            pytest.param([], True, id="empty"),
            pytest.param(["enterprise"], True, id="unknown"),
            pytest.param(["Pro"], True, id="miscased"),
            pytest.param([" pro"], True, id="padded"),
            pytest.param(["pro", "pro"], True, id="repeated"),
            pytest.param([1], False, id="not_a_string"),
            pytest.param("pro", False, id="not_a_list"),
        ],
    )
    def test_refusals(
        self, client, db, session_admin, log, monkeypatch, plans, lists_the_keys
    ) -> None:
        create = AsyncMock(return_value=_view())
        monkeypatch.setattr(routes, "create_notice", create)

        response = client.post(URL, json=_body(plans=plans))

        assert response.status_code == 422, response.text
        payload = response.json()
        assert payload["error"] == "VAL-001"
        errors = payload["details"]["errors"]
        assert errors
        for error in errors:
            assert error["loc"][:2] == ["body", "plans"], error
        if plans == [1]:
            assert [e["loc"] for e in errors] == [["body", "plans", 0]]
        if lists_the_keys:
            assert all("free, basic, pro, promax" in e["msg"] for e in errors)
        assert "enterprise" not in response.text
        create.assert_not_awaited()
        db.commit.assert_not_awaited()
        log.warning.assert_not_called()


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
