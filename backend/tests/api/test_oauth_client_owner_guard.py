"""Ownership guard for OAuth2 client update/delete (issue #1021).

`update_oauth2_client` and `delete_oauth2_client` historically loaded the
client by ``client_id`` alone and mutated/deleted it WITHOUT verifying the
caller owns it — an IDOR (CWE-639). Any authenticated session user could
overwrite another tenant's ``redirect_uris`` (authorization-code interception)
or delete the client (CASCADE-deleting its tokens = forced logout / DoS).

These tests pin the guard that the sibling handlers (``get`` / ``hide`` /
``regenerate``) already enforce: a non-owner gets ``AuthorizationError`` (403)
and NO mutation is committed. Global DCR clients (``owner_id is None``, e.g.
ChatGPT / Claude / Cursor connectors) must never be mutable via this
session-user endpoint.

These are unit tests on the handler coroutines directly (the handlers use
``get_sync_session()`` + ``get_current_user_id(request)`` rather than DI
overrides), so the auth boundary is exercised without the full HTTP/DB stack.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Match the sys.path layout the rest of the backend tests use.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402

from api.routes import oauth as oauth_routes  # noqa: E402
from api.routes.oauth import (  # noqa: E402
    OAuth2ClientUpdateRequest,
    delete_oauth2_client,
    update_oauth2_client,
)
from utils.datetime import utcnow  # noqa: E402
from utils.exceptions import AuthorizationError  # noqa: E402

_OWNER = "user-a"
_OTHER = "user-b"


class _FakeQuery:
    def __init__(self, client: object) -> None:
        self._client = client

    def filter_by(self, **_kwargs: object) -> _FakeQuery:
        return self

    def first(self) -> object:
        return self._client


class _FakeSession:
    """Minimal stand-in for the sync SQLAlchemy session the handlers use."""

    def __init__(self, client: object) -> None:
        self._client = client
        self.committed = False
        self.deleted: object | None = None
        self.rolled_back = False
        self.closed = False

    def query(self, _model: object) -> _FakeQuery:
        return _FakeQuery(self._client)

    def commit(self) -> None:
        self.committed = True

    def refresh(self, _obj: object) -> None:
        pass

    def delete(self, obj: object) -> None:
        self.deleted = obj

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _make_client(owner_id: str | None) -> SimpleNamespace:
    """A client row with every attribute the success-path response needs."""
    return SimpleNamespace(
        id=1,
        client_id="oauth_abc123",
        client_name="Victim App",
        redirect_uris=["https://victim.example/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="openid",
        token_endpoint_auth_method="client_secret_post",
        owner_id=owner_id,
        provider="custom",
        created_at=utcnow(),
        hidden_at=None,
        visibility_expires_at=None,
    )


def _request_as(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"user_id": user_id}))


# --------------------------------------------------------------------------- #
# Non-owner is rejected (the core IDOR fix)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_rejects_non_owner() -> None:
    session = _FakeSession(_make_client(owner_id=_OWNER))
    with patch.object(oauth_routes, "get_sync_session", return_value=session):
        with pytest.raises(AuthorizationError):
            await update_oauth2_client(
                request=_request_as(_OTHER),
                client_id="oauth_abc123",
                data=OAuth2ClientUpdateRequest(redirect_uris=["https://attacker.example/cb"]),
                user=None,
            )
    assert session.committed is False, "non-owner update must not be committed"


@pytest.mark.asyncio
async def test_delete_rejects_non_owner() -> None:
    session = _FakeSession(_make_client(owner_id=_OWNER))
    with patch.object(oauth_routes, "get_sync_session", return_value=session):
        with pytest.raises(AuthorizationError):
            await delete_oauth2_client(
                request=_request_as(_OTHER),
                client_id="oauth_abc123",
                user=None,
            )
    assert session.deleted is None, "non-owner delete must not delete the client"
    assert session.committed is False


# --------------------------------------------------------------------------- #
# DCR clients (owner_id is None) are not mutable by a session user
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_rejects_dcr_client() -> None:
    session = _FakeSession(_make_client(owner_id=None))
    with patch.object(oauth_routes, "get_sync_session", return_value=session):
        with pytest.raises(AuthorizationError):
            await update_oauth2_client(
                request=_request_as(_OTHER),
                client_id="oauth_abc123",
                data=OAuth2ClientUpdateRequest(redirect_uris=["https://attacker.example/cb"]),
                user=None,
            )
    assert session.committed is False


@pytest.mark.asyncio
async def test_delete_rejects_dcr_client() -> None:
    session = _FakeSession(_make_client(owner_id=None))
    with patch.object(oauth_routes, "get_sync_session", return_value=session):
        with pytest.raises(AuthorizationError):
            await delete_oauth2_client(
                request=_request_as(_OTHER),
                client_id="oauth_abc123",
                user=None,
            )
    assert session.deleted is None
    assert session.committed is False


# --------------------------------------------------------------------------- #
# Happy path: the owner can still update / delete (no regression)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_allows_owner() -> None:
    session = _FakeSession(_make_client(owner_id=_OWNER))
    with patch.object(oauth_routes, "get_sync_session", return_value=session):
        result = await update_oauth2_client(
            request=_request_as(_OWNER),
            client_id="oauth_abc123",
            data=OAuth2ClientUpdateRequest(client_name="Renamed"),
            user=None,
        )
    assert session.committed is True
    assert result.client_id == "oauth_abc123"


@pytest.mark.asyncio
async def test_delete_allows_owner() -> None:
    client = _make_client(owner_id=_OWNER)
    session = _FakeSession(client)
    with patch.object(oauth_routes, "get_sync_session", return_value=session):
        await delete_oauth2_client(
            request=_request_as(_OWNER),
            client_id="oauth_abc123",
            user=None,
        )
    assert session.deleted is client
    assert session.committed is True
