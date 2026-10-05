"""Response bodies that dropped the integer ``users`` PK (#1813, #1882).

``test_public_id_openapi.py`` guards the schema. These tests go through the
routes and read the JSON a client receives, so a handler that puts the
integer PK back into a body fails here even where the schema cannot see it.

Uses dependency_overrides to mock auth and DB — no real Docker/Postgres required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin, require_session_auth
from db.base import get_db

_INTEGER_PK = 4242


def _user_row(**overrides) -> SimpleNamespace:
    """Stand-in for a ``User`` ORM row; ``id`` is the integer PK that must
    not reach a response body."""
    fields = {
        "id": _INTEGER_PK,
        "user_id": "google-oauth2|abc",
        "email": "person@example.com",
        "name": "Person",
        "picture": None,
        "timezone": "UTC",
        "locale": "en",
        "role": "admin",
        "is_initial_admin": True,
        "current_workspace_id": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_login_at": None,
        "auth_method": "oauth",
        "auth_provider": "google",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _scalar(value) -> MagicMock:
    result = MagicMock()
    result.scalar.return_value = value
    result.scalar_one_or_none.return_value = value
    return result


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _install(db: AsyncMock) -> None:
    async def _principal():
        return {"user_id": "google-oauth2|abc", "email": "person@example.com", "role": "admin"}

    async def _db():
        yield db

    app.dependency_overrides[require_admin] = _principal
    app.dependency_overrides[require_session_auth] = _principal
    app.dependency_overrides[get_db] = _db


def _holds(body: object, value: object) -> bool:
    """True if ``value`` appears anywhere in a decoded JSON body."""
    if isinstance(body, dict):
        return any(_holds(v, value) for v in body.values())
    if isinstance(body, list):
        return any(_holds(v, value) for v in body)
    return body == value and type(body) is type(value)


class TestAdminUserStatsBody:
    """GET /api/v1/admin/users/{user_id}/stats (#1882)."""

    def test_user_carries_string_user_id_and_no_integer_id(self, client):
        target = _user_row(role="user")
        by_type = MagicMock()
        by_type.all.return_value = [("note", 3)]
        db = AsyncMock()
        # user row, total, working, by type, active API keys
        db.execute.side_effect = [_scalar(target), _scalar(3), _scalar(1), by_type, _scalar(2)]
        _install(db)

        response = client.get(f"/api/v1/admin/users/{target.user_id}/stats")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"] == {
            "user_id": "google-oauth2|abc",
            "email": "person@example.com",
            "name": "Person",
            "role": "user",
        }
        assert "id" not in body["user"]
        assert not _holds(body, _INTEGER_PK)
        assert body["memories"] == {
            "total": 3,
            "working": 1,
            "persistent": 2,
            "by_type": {"note": 3},
        }
        assert body["api_usage"]["active_api_keys"] == 2


class TestSystemAdminListBody:
    """GET /api/v1/admin/system-admins (#1813)."""

    def test_entries_carry_user_id_and_flag_and_no_integer_ids(self, client):
        initial = _user_row()
        promoted = _user_row(
            id=_INTEGER_PK + 1,
            user_id="github|def",
            email="second@example.com",
            is_initial_admin=False,
        )
        listing = MagicMock()
        listing.scalars.return_value.all.return_value = [initial, promoted]
        db = AsyncMock()
        # admin rows, then one memory count per admin
        db.execute.side_effect = [listing, _scalar(5), _scalar(0)]
        _install(db)

        response = client.get("/api/v1/admin/system-admins")

        assert response.status_code == 200, response.text
        body = response.json()
        assert "initial_admin_id" not in body
        assert body["total"] == 2
        assert [(a["user_id"], a["is_initial_admin"]) for a in body["admins"]] == [
            ("google-oauth2|abc", True),
            ("github|def", False),
        ]
        for admin in body["admins"]:
            assert "id" not in admin
        assert not _holds(body, _INTEGER_PK)
        assert not _holds(body, _INTEGER_PK + 1)


class TestUserProfileBody:
    """GET /api/v1/users/profile (#1813)."""

    def test_profile_carries_string_user_id_and_no_integer_id(self, client):
        db = AsyncMock()
        db.execute.return_value = _scalar(_user_row())
        _install(db)

        response = client.get("/api/v1/users/profile")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user_id"] == "google-oauth2|abc"
        assert isinstance(body["user_id"], str)
        assert "id" not in body
        assert not _holds(body, _INTEGER_PK)
