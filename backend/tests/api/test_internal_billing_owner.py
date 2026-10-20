"""Unit tests for the internal billing ownership re-check (Issue #2181).

``GET /internal/workspaces/{workspace_id}/owner?sub=...`` — TestClient + mocked
DB, mirroring ``test_internal_billing_plan.py``. Pins the real service-token
auth (503/401), input validation (422), 404 only for a missing row, the
soft-deleted → ``is_owner: false`` rule, and that ``sub`` never reaches the
application logs (a server access log is a deployment setting, out of scope).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.base import get_db

_WS_ID = str(uuid4())
_PATH = f"/internal/workspaces/{_WS_ID}/owner"
_OWNER = "user_owner_2181"
_AUTH = {"Authorization": "Bearer secret"}

# Sentinel: the mocked lookup found no workspace row.
_NO_ROW = object()


@pytest.fixture
def billing(monkeypatch):
    """Harness: patched service token + a mocked DB answering the owner lookup.

    ``billing.client(row)`` returns a TestClient whose DB answers the
    ``(owner_user_id, deleted_at)`` lookup with ``row`` (``_NO_ROW`` → no row).
    ``billing.db`` is the last mocked session, for asserting it was not written.
    """
    mock_settings = MagicMock()
    mock_settings.billing_service_token = "secret"
    monkeypatch.setattr("api.routes.internal_billing.get_settings", lambda: mock_settings)

    class _Harness:
        db: MagicMock | None = None

        @property
        def token(self) -> str:
            return mock_settings.billing_service_token

        @token.setter
        def token(self, value: str) -> None:
            mock_settings.billing_service_token = value

        def client(self, row=(_OWNER, None)) -> TestClient:
            harness = self

            async def mock_db():
                db = MagicMock()
                exec_result = MagicMock()
                exec_result.one_or_none.return_value = None if row is _NO_ROW else row
                db.execute = AsyncMock(return_value=exec_result)
                db.commit = AsyncMock()
                db.add = MagicMock()
                harness.db = db
                yield db

            app.dependency_overrides[get_db] = mock_db
            return TestClient(app, raise_server_exceptions=False)

    yield _Harness()
    app.dependency_overrides.clear()


# --- auth (shared dependency with the sibling routes) ---------------------------


def test_unset_token_returns_503(billing):
    billing.token = ""
    resp = billing.client().get(
        _PATH, params={"sub": _OWNER}, headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 503


def test_missing_authorization_returns_401(billing):
    resp = billing.client().get(_PATH, params={"sub": _OWNER})
    assert resp.status_code == 401


def test_invalid_token_returns_401(billing):
    resp = billing.client().get(
        _PATH, params={"sub": _OWNER}, headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401


# --- validation ------------------------------------------------------------------


def test_non_uuid_workspace_id_returns_422(billing):
    resp = billing.client().get(
        "/internal/workspaces/not-a-uuid/owner", params={"sub": _OWNER}, headers=_AUTH
    )
    assert resp.status_code == 422


def test_missing_sub_returns_422(billing):
    resp = billing.client().get(_PATH, headers=_AUTH)
    assert resp.status_code == 422


@pytest.mark.parametrize("sub", ["", "   "])
def test_blank_sub_returns_422(billing, sub):
    resp = billing.client().get(_PATH, params={"sub": sub}, headers=_AUTH)
    assert resp.status_code == 422


def test_oversized_sub_returns_422(billing):
    resp = billing.client().get(_PATH, params={"sub": "u" * 256}, headers=_AUTH)
    assert resp.status_code == 422


# --- answers ---------------------------------------------------------------------


def test_unknown_workspace_returns_404(billing):
    resp = billing.client(_NO_ROW).get(_PATH, params={"sub": _OWNER}, headers=_AUTH)
    assert resp.status_code == 404


def test_owner_sub_is_owner(billing):
    resp = billing.client((_OWNER, None)).get(_PATH, params={"sub": _OWNER}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"workspace_id": _WS_ID, "sub": _OWNER, "is_owner": True}


@pytest.mark.parametrize("sub", ["user_member_2181", "user_stranger_2181"])
def test_non_owner_sub_is_not_owner(billing, sub):
    """A member or an unrelated user is not the owner — no membership fallback."""
    resp = billing.client((_OWNER, None)).get(_PATH, params={"sub": sub}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"workspace_id": _WS_ID, "sub": sub, "is_owner": False}


def test_soft_deleted_workspace_answers_false_not_404(billing):
    """An existing-but-deleted workspace must never 404 (the caller reads a 404
    as "route not deployed"); it fails closed with ``is_owner: false``."""
    row = (_OWNER, datetime.now(UTC))
    resp = billing.client(row).get(_PATH, params={"sub": _OWNER}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["is_owner"] is False


def test_workspace_without_owner_pointer_answers_false(billing):
    resp = billing.client((None, None)).get(_PATH, params={"sub": _OWNER}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["is_owner"] is False


def test_read_only(billing):
    resp = billing.client().get(_PATH, params={"sub": _OWNER}, headers=_AUTH)
    assert resp.status_code == 200
    billing.db.commit.assert_not_awaited()
    billing.db.add.assert_not_called()


def test_sub_is_never_logged(billing, caplog, monkeypatch):
    """``sub`` is a user identifier: no app log call carries it, at any level."""
    secret_sub = "user_never_logged_2181"
    # A recording logger sees every call, including debug ones that the
    # configured level would drop before any capture.
    recorder = MagicMock()
    monkeypatch.setattr("api.routes.internal_billing.logger", recorder)
    caplog.set_level(logging.DEBUG)
    resp = billing.client((secret_sub, None)).get(_PATH, params={"sub": secret_sub}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["is_owner"] is True
    assert recorder.mock_calls, "the route is expected to log the check (workspace id only)"
    assert all(secret_sub not in repr(c) for c in recorder.mock_calls)
    # The test client's own ``httpx`` request line carries the URL; it is not app logging.
    app_records = [r for r in caplog.records if not r.name.startswith("httpx")]
    assert all(secret_sub not in r.getMessage() for r in app_records)
