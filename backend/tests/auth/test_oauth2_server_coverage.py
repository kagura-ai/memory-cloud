"""Comprehensive coverage tests for ``auth.oauth2_server``.

The module is an Authlib OAuth2 authorization server adapted for FastAPI with
*synchronous* SQLAlchemy sessions (Authlib requires sync access). Because the
project's shared ``conftest.py`` only exposes an async ``db_session``, and the
production code reaches into ``request`` objects and the session purely through
attribute / duck-typed access, these tests follow the house pattern established
in ``tests/auth/test_device_code_grant.py``: drive the grant + query + save
functions with ``MagicMock`` sessions and lightweight stub request objects, and
assert against real (un-persisted) ORM model instances.

Covered surfaces:
  * ``query_client`` (found / not-found)
  * ``save_token`` (authorization-code path, refresh path, resource from
    credential vs request data, and the missing-user_id ``ValueError`` branch)
  * ``_generate_token_with_expiry`` (shape) via the grant ``generate_token``
    overrides
  * ``AuthorizationCodeGrant``: ``save_authorization_code`` (payload dict /
    payload.data / request.data / no-data branches, PKCE + resource from
    request, Redis restore for resource & PKCE, missing user_id error),
    ``query_authorization_code`` (found / not-found / expired),
    ``delete_authorization_code``, ``authenticate_user``
  * ``RefreshTokenGrant``: ``authenticate_refresh_token`` (active / inactive /
    not-found), ``authenticate_user``, ``revoke_old_credential``
  * ``OAuth2AuthorizationServer`` construction + ``_register_grants`` PKCE
    kill-switch (on / off) and the thin delegating wrapper methods +
    ``create_authorization_server`` factory.

Nothing here touches the network; Redis is replaced with an in-memory fake.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402

from auth import oauth2_server as mod  # noqa: E402
from auth.oauth2_server import (  # noqa: E402
    AuthorizationCodeGrant,
    DeviceAuthorizationGrant,
    OAuth2AuthorizationServer,
    RefreshTokenGrant,
    _generate_token_with_expiry,
    _OAuthUser,
    create_authorization_server,
    query_client,
    save_token,
)
from models.auth import (  # noqa: E402
    OAuth2AuthorizationCode,
    OAuth2Client,
    OAuth2DeviceCode,
    OAuth2Token,
)
from utils.datetime import utcnow  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(client_id: str = "oauth_test123", **overrides: Any) -> OAuth2Client:
    """Build an un-persisted OAuth2Client ORM instance for attribute access."""
    kwargs: dict[str, Any] = {
        "client_id": client_id,
        "client_secret_hash": "x" * 64,
        "client_name": "Test Client",
        "redirect_uris": ["https://example.com/cb"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "memory:read memory:write",
        "token_endpoint_auth_method": "client_secret_post",
    }
    kwargs.update(overrides)
    return OAuth2Client(**kwargs)


def _make_token(**overrides: Any) -> OAuth2Token:
    kwargs: dict[str, Any] = {
        "client_id": "oauth_test123",
        "user_id": "user-abc",
        "token_type": "Bearer",
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
        "scope": "memory:read",
        "expires_in": 3600,
        "issued_at": utcnow(),
    }
    kwargs.update(overrides)
    return OAuth2Token(**kwargs)


def _make_authz_code(**overrides: Any) -> OAuth2AuthorizationCode:
    kwargs: dict[str, Any] = {
        "code": "the-code-1234567890",
        "client_id": "oauth_test123",
        "user_id": "user-abc",
        "redirect_uri": "https://example.com/cb",
        "scope": "memory:read",
        "auth_time": utcnow(),
        "expires_at": utcnow() + timedelta(seconds=600),
    }
    kwargs.update(overrides)
    return OAuth2AuthorizationCode(**kwargs)


def _make_authz_grant() -> AuthorizationCodeGrant:
    grant = AuthorizationCodeGrant.__new__(AuthorizationCodeGrant)
    grant.server = MagicMock()
    grant.server.db_session = MagicMock()
    return grant


def _make_refresh_grant() -> RefreshTokenGrant:
    grant = RefreshTokenGrant.__new__(RefreshTokenGrant)
    grant.server = MagicMock()
    grant.server.db_session = MagicMock()
    return grant


def _make_device_grant() -> DeviceAuthorizationGrant:
    grant = DeviceAuthorizationGrant.__new__(DeviceAuthorizationGrant)
    grant.server = MagicMock()
    grant.server.db_session = MagicMock()
    return grant


def _make_device(**overrides: Any) -> OAuth2DeviceCode:
    kwargs: dict[str, Any] = {
        "device_code": "dev-code-abc",
        "user_code": "ABCD1234",
        "client_id": "oauth_test123",
        "user_id": None,
        "scope": "memory:read",
        "expires_at": utcnow() + timedelta(seconds=600),
        "last_polled_at": None,
        "denied_at": None,
        "authorized_at": None,
    }
    kwargs.update(overrides)
    return OAuth2DeviceCode(**kwargs)


class _FakeRedis:
    """In-memory stand-in for ``redis.Redis`` (get/delete + from_url)."""

    _store: dict[str, str] = {}

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        # Each instance shares the class-level store wired by the test.
        if mapping is not None:
            type(self)._store = dict(mapping)

    @classmethod
    def from_url(cls, url: str, decode_responses: bool = True) -> _FakeRedis:  # noqa: ARG003
        return cls()

    def get(self, key: str) -> str | None:
        return type(self)._store.get(key)

    def delete(self, key: str) -> None:
        type(self)._store.pop(key, None)


# ===========================================================================
# query_client
# ===========================================================================


class TestQueryClient:
    """``query_client`` filters by client_id and returns first / None."""

    def test_returns_client_when_found(self) -> None:
        session = MagicMock()
        client = _make_client()
        session.query.return_value.filter_by.return_value.first.return_value = client

        result = query_client(session, "oauth_test123")

        assert result is client
        session.query.return_value.filter_by.assert_called_once_with(client_id="oauth_test123")

    def test_returns_none_when_not_found(self) -> None:
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None

        assert query_client(session, "nope") is None


# ===========================================================================
# save_token
# ===========================================================================


class TestSaveToken:
    """Token persistence covers user_id resolution + RFC 8707 resource."""

    def test_saves_token_with_user_from_request_user(self) -> None:
        session = MagicMock()
        client = _make_client()
        # authorization_code path: request.user present, no credential/data.
        request = SimpleNamespace(client=client, user=SimpleNamespace(user_id="user-xyz"))
        token = {
            "token_type": "Bearer",
            "access_token": "at-123",
            "refresh_token": "rt-123",
            "scope": "memory:read",
            "expires_in": 1800,
        }

        save_token(token, request, session)

        added = session.add.call_args.args[0]
        assert isinstance(added, OAuth2Token)
        assert added.client_id == "oauth_test123"
        assert added.user_id == "user-xyz"
        assert added.access_token == "at-123"
        assert added.refresh_token == "rt-123"
        assert added.scope == "memory:read"
        assert added.expires_in == 1800
        assert added.resource is None
        assert added.revoked is False
        session.commit.assert_called_once()

    def test_user_id_from_credential_when_request_user_missing(self) -> None:
        session = MagicMock()
        client = _make_client()
        # Refresh path: no .user, user comes from request.credential.
        credential = SimpleNamespace(user_id="cred-user", resource="https://api.example.com")
        request = SimpleNamespace(client=client, credential=credential)
        token = {"access_token": "at", "refresh_token": "rt"}

        save_token(token, request, session)

        added = session.add.call_args.args[0]
        assert added.user_id == "cred-user"
        # Resource taken from the credential (authorization code) branch.
        assert added.resource == "https://api.example.com"
        # Defaults applied for absent token fields.
        assert added.token_type == "Bearer"
        assert added.expires_in == 3600
        assert added.scope == ""

    def test_resource_from_request_data_when_no_credential(self) -> None:
        session = MagicMock()
        client = _make_client()
        # No credential attribute, but request.data carries the resource param.
        request = SimpleNamespace(
            client=client,
            user=SimpleNamespace(user_id="u1"),
            data={"resource": "https://from-data"},
        )
        token = {"access_token": "at", "refresh_token": "rt"}

        save_token(token, request, session)

        added = session.add.call_args.args[0]
        assert added.resource == "https://from-data"

    def test_raises_value_error_when_user_id_missing(self) -> None:
        session = MagicMock()
        client = _make_client()
        # request.user.user_id is falsy AND no credential → ValueError branch.
        request = SimpleNamespace(client=client, user=SimpleNamespace(user_id=None))
        token = {"access_token": "at"}

        with pytest.raises(ValueError, match="User ID required"):
            save_token(token, request, session)
        session.add.assert_not_called()


# ===========================================================================
# _generate_token_with_expiry + grant generate_token overrides
# ===========================================================================


class TestGenerateToken:
    """The shared token generator + both grant overrides produce a token dict."""

    def test_generate_token_with_expiry_shape(self) -> None:
        client = _make_client()
        token = _generate_token_with_expiry("authorization_code", client, 600, "memory:read")

        assert token["token_type"] == "Bearer"
        assert isinstance(token["access_token"], str) and token["access_token"]
        assert isinstance(token["refresh_token"], str) and token["refresh_token"]
        assert token["expires_in"] == 600
        assert token["scope"] == "memory:read"
        # access_token and refresh_token are independently random.
        assert token["access_token"] != token["refresh_token"]

    def _grant_with_request(self, grant: Any) -> Any:
        grant.request = MagicMock()
        grant.request.client = _make_client()
        return grant

    def test_authorization_code_generate_token_defaults(self) -> None:
        grant = self._grant_with_request(_make_authz_grant())
        grant.GRANT_TYPE = "authorization_code"
        token = grant.generate_token(scope="memory:read")
        assert token["expires_in"] == AuthorizationCodeGrant.TOKEN_EXPIRES_IN == 3600
        assert token["scope"] == "memory:read"

    def test_authorization_code_generate_token_explicit_args(self) -> None:
        grant = self._grant_with_request(_make_authz_grant())
        token = grant.generate_token(scope="s", grant_type="authorization_code", expires_in=99)
        assert token["expires_in"] == 99

    def test_refresh_generate_token_defaults(self) -> None:
        grant = self._grant_with_request(_make_refresh_grant())
        grant.GRANT_TYPE = "refresh_token"
        token = grant.generate_token(scope="memory:write")
        assert token["expires_in"] == RefreshTokenGrant.TOKEN_EXPIRES_IN == 3600
        assert token["scope"] == "memory:write"

    def test_refresh_generate_token_explicit_expires_in(self) -> None:
        grant = self._grant_with_request(_make_refresh_grant())
        token = grant.generate_token(scope="s", grant_type="refresh_token", expires_in=7)
        assert token["expires_in"] == 7


# ===========================================================================
# AuthorizationCodeGrant.save_authorization_code
# ===========================================================================


class TestSaveAuthorizationCode:
    """Cover every request-data shape + PKCE/resource + Redis restore."""

    def _request(self, **kw: Any) -> SimpleNamespace:
        base = {
            "client": _make_client(),
            "user": SimpleNamespace(user_id="user-abc"),
            "redirect_uri": "https://example.com/cb",
            "scope": "memory:read",
        }
        base.update(kw)
        return SimpleNamespace(**base)

    def _added(self, grant: AuthorizationCodeGrant) -> OAuth2AuthorizationCode:
        return grant.server.db_session.add.call_args.args[0]

    def test_payload_dict_branch_with_pkce_and_resource(self) -> None:
        grant = _make_authz_grant()
        request = self._request(
            payload={
                "code_challenge": "challenge-xyz",
                "code_challenge_method": "S256",
                "resource": "https://api.example.com",
            }
        )

        result = grant.save_authorization_code("authcode-1", request)

        added = self._added(grant)
        assert result is added
        assert added.code == "authcode-1"
        assert added.client_id == "oauth_test123"
        assert added.user_id == "user-abc"
        assert added.code_challenge == "challenge-xyz"
        assert added.code_challenge_method == "S256"
        assert added.resource == "https://api.example.com"
        assert added.redirect_uri == "https://example.com/cb"
        # 10-minute expiry window stamped.
        assert added.expires_at > added.auth_time
        grant.server.db_session.commit.assert_called_once()

    def test_payload_data_attribute_branch(self) -> None:
        grant = _make_authz_grant()
        # payload is not a dict but exposes ``.data`` (Authlib 1.x form payload).
        payload = SimpleNamespace(data={"code_challenge": "cc", "resource": "r"})
        request = self._request(payload=payload)

        grant.save_authorization_code("authcode-2", request)

        added = self._added(grant)
        assert added.code_challenge == "cc"
        assert added.resource == "r"

    def test_payload_data_none_falls_back_to_empty(self) -> None:
        grant = _make_authz_grant()
        # payload.data is None → request_data stays {} → no challenge/resource.
        payload = SimpleNamespace(data=None)
        request = self._request(payload=payload)

        grant.save_authorization_code("authcode-3", request)

        added = self._added(grant)
        assert added.code_challenge is None
        assert added.resource is None

    def test_payload_unknown_type_branch(self) -> None:
        grant = _make_authz_grant()
        # payload is truthy but neither dict nor has ``.data`` → warning branch,
        # request_data stays {}.
        request = self._request(payload=12345)

        grant.save_authorization_code("authcode-4", request)

        added = self._added(grant)
        assert added.code_challenge is None
        assert added.resource is None

    def test_request_data_branch_authlib_0x(self) -> None:
        grant = _make_authz_grant()
        # No payload attribute at all → fall through to request.data.
        request = self._request(data={"code_challenge": "old", "resource": "old-res"})

        grant.save_authorization_code("authcode-5", request)

        added = self._added(grant)
        assert added.code_challenge == "old"
        assert added.resource == "old-res"

    def test_no_payload_no_data_branch(self) -> None:
        grant = _make_authz_grant()
        # payload absent, data absent → final ``else`` warning branch.
        request = self._request()

        grant.save_authorization_code("authcode-6", request)

        added = self._added(grant)
        assert added.code_challenge is None
        assert added.resource is None

    def test_restores_resource_and_pkce_from_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        grant = _make_authz_grant()
        # request data has only ``state`` → both resource + PKCE come from Redis.
        request = self._request(payload={"state": "st-1"})

        store = {
            "oauth_state:st-1:resource": "https://redis-resource",
            "oauth_state:st-1:code_challenge": "redis-challenge",
            "oauth_state:st-1:code_challenge_method": "S256",
        }
        monkeypatch.setattr(_FakeRedis, "_store", dict(store))
        monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=_FakeRedis))
        monkeypatch.setattr("config.database.get_redis_url", lambda: "redis://localhost:6379/0")

        grant.save_authorization_code("authcode-7", request)

        added = self._added(grant)
        assert added.resource == "https://redis-resource"
        assert added.code_challenge == "redis-challenge"
        assert added.code_challenge_method == "S256"
        # One-time-use keys deleted on restore.
        assert _FakeRedis._store.get("oauth_state:st-1:resource") is None
        assert _FakeRedis._store.get("oauth_state:st-1:code_challenge") is None

    def test_redis_branch_with_no_stored_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        grant = _make_authz_grant()
        # state present but Redis has nothing → resource/PKCE stay None.
        request = self._request(payload={"state": "st-empty"})
        monkeypatch.setattr(_FakeRedis, "_store", {})
        monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=_FakeRedis))
        monkeypatch.setattr("config.database.get_redis_url", lambda: "redis://localhost:6379/0")

        grant.save_authorization_code("authcode-8", request)

        added = self._added(grant)
        assert added.resource is None
        assert added.code_challenge is None

    def test_redis_challenge_without_method(self, monkeypatch: pytest.MonkeyPatch) -> None:
        grant = _make_authz_grant()
        # Restore a code_challenge from Redis but with no stored method → the
        # ``if code_challenge_method`` delete branch is skipped.
        request = self._request(payload={"state": "st-2"})
        store = {"oauth_state:st-2:code_challenge": "cc-only"}
        monkeypatch.setattr(_FakeRedis, "_store", dict(store))
        monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=_FakeRedis))
        monkeypatch.setattr("config.database.get_redis_url", lambda: "redis://localhost:6379/0")

        grant.save_authorization_code("authcode-9", request)

        added = self._added(grant)
        assert added.code_challenge == "cc-only"
        assert added.code_challenge_method is None

    def test_raises_value_error_when_user_id_missing(self) -> None:
        grant = _make_authz_grant()
        request = self._request(user=SimpleNamespace(user_id=None), payload={})

        with pytest.raises(ValueError, match="User ID required for authorization"):
            grant.save_authorization_code("authcode-x", request)
        grant.server.db_session.add.assert_not_called()


# ===========================================================================
# AuthorizationCodeGrant query / delete / authenticate
# ===========================================================================


class TestAuthorizationCodeQueryDelete:
    def test_query_authorization_code_found(self) -> None:
        grant = _make_authz_grant()
        code = _make_authz_code()
        grant.server.db_session.query().filter_by().first.return_value = code

        result = grant.query_authorization_code("the-code-1234567890", _make_client())
        assert result is code

    def test_query_authorization_code_not_found(self) -> None:
        grant = _make_authz_grant()
        grant.server.db_session.query().filter_by().first.return_value = None

        result = grant.query_authorization_code("missing", _make_client())
        assert result is None

    def test_query_authorization_code_expired_returns_none(self) -> None:
        grant = _make_authz_grant()
        expired = _make_authz_code(expires_at=utcnow() - timedelta(seconds=1))
        grant.server.db_session.query().filter_by().first.return_value = expired

        result = grant.query_authorization_code("the-code-1234567890", _make_client())
        assert result is None

    def test_delete_authorization_code(self) -> None:
        grant = _make_authz_grant()
        code = _make_authz_code()

        grant.delete_authorization_code(code)

        grant.server.db_session.delete.assert_called_once_with(code)
        grant.server.db_session.commit.assert_called_once()

    def test_authenticate_user_wraps_user_id(self) -> None:
        grant = _make_authz_grant()
        code = _make_authz_code(user_id="user-deadbeef")

        user = grant.authenticate_user(code)
        assert isinstance(user, _OAuthUser)
        assert user.user_id == "user-deadbeef"
        assert user.get_user_id() == "user-deadbeef"


# ===========================================================================
# RefreshTokenGrant
# ===========================================================================


class TestRefreshTokenGrant:
    def test_authenticate_refresh_token_active(self) -> None:
        grant = _make_refresh_grant()
        token = _make_token(refresh_token="rt-active", refresh_token_revoked_at=None)
        grant.server.db_session.query().filter_by().first.return_value = token

        result = grant.authenticate_refresh_token("rt-active")
        assert result is token

    def test_authenticate_refresh_token_inactive_returns_none(self) -> None:
        grant = _make_refresh_grant()
        # Revoked refresh token → is_refresh_token_active() False → None + warn.
        token = _make_token(refresh_token="rt-revoked", refresh_token_revoked_at=utcnow())
        grant.server.db_session.query().filter_by().first.return_value = token

        result = grant.authenticate_refresh_token("rt-revoked")
        assert result is None

    def test_authenticate_refresh_token_not_found(self) -> None:
        grant = _make_refresh_grant()
        grant.server.db_session.query().filter_by().first.return_value = None

        result = grant.authenticate_refresh_token("nope")
        assert result is None

    def test_authenticate_user_wraps_credential(self) -> None:
        grant = _make_refresh_grant()
        token = _make_token(user_id="refresh-user")

        user = grant.authenticate_user(token)
        assert isinstance(user, _OAuthUser)
        assert user.user_id == "refresh-user"

    def test_revoke_old_credential_stamps_both_revocations(self) -> None:
        grant = _make_refresh_grant()
        token = _make_token(access_token_revoked_at=None, refresh_token_revoked_at=None)

        grant.revoke_old_credential(token)

        assert token.access_token_revoked_at is not None
        assert token.refresh_token_revoked_at is not None
        grant.server.db_session.commit.assert_called_once()


# ===========================================================================
# OAuth2AuthorizationServer construction + wrappers + factory
# ===========================================================================


class _SettingsStub:
    def __init__(self, pkce_required: bool = True) -> None:
        self.oauth_pkce_required = pkce_required
        self.oauth_device_polling_interval = 5


class TestServerConstruction:
    """Full ``__init__`` path: build a real server over a mock session."""

    def test_init_registers_grants_with_pkce_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "get_settings", lambda: _SettingsStub(pkce_required=True))
        session = MagicMock()

        wrapper = OAuth2AuthorizationServer(session)

        assert wrapper.db_session is session
        # The inner server is a real Authlib AuthorizationServer subclass and
        # carries the session for grant access.
        assert wrapper.server.db_session is session
        # Authlib stores registered token grants internally as
        # ``[(grant_cls, extensions_or_None), ...]``; the three grant classes
        # must all be present after construction.
        registered = {grant_cls for grant_cls, _ext in wrapper.server._token_grants}
        assert AuthorizationCodeGrant in registered
        assert RefreshTokenGrant in registered
        assert len(wrapper.server._token_grants) == 3
        # query_client / save_token wired as callables on the inner server.
        assert callable(wrapper.server.query_client)
        assert callable(wrapper.server.save_token)

    def test_register_grants_pkce_disabled_skips_codechallenge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "get_settings", lambda: _SettingsStub(pkce_required=False))
        wrapper = OAuth2AuthorizationServer.__new__(OAuth2AuthorizationServer)
        wrapper.server = MagicMock()

        wrapper._register_grants()

        # The authorization-code grant is registered WITHOUT the CodeChallenge
        # extension list (emergency rollback path → single positional arg).
        authz_calls = [
            c
            for c in wrapper.server.register_grant.call_args_list
            if c.args and c.args[0] is AuthorizationCodeGrant
        ]
        assert len(authz_calls) == 1
        assert len(authz_calls[0].args) == 1  # no extensions list

    def test_register_grants_pkce_enabled_attaches_codechallenge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "get_settings", lambda: _SettingsStub(pkce_required=True))
        wrapper = OAuth2AuthorizationServer.__new__(OAuth2AuthorizationServer)
        wrapper.server = MagicMock()

        wrapper._register_grants()

        authz_calls = [
            c
            for c in wrapper.server.register_grant.call_args_list
            if c.args and c.args[0] is AuthorizationCodeGrant
        ]
        assert len(authz_calls) == 1
        # PKCE-on path passes an extensions list as the second positional arg.
        assert len(authz_calls[0].args) == 2
        extensions = authz_calls[0].args[1]
        assert isinstance(extensions, list) and len(extensions) == 1

    def test_query_client_func_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "get_settings", _SettingsStub)
        client = _make_client()
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = client

        wrapper = OAuth2AuthorizationServer(session)
        # The closure assigned to server.query_client uses module ``query_client``.
        assert wrapper.server.query_client("oauth_test123") is client

    def test_save_token_func_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "get_settings", _SettingsStub)
        session = MagicMock()
        wrapper = OAuth2AuthorizationServer(session)

        request = SimpleNamespace(client=_make_client(), user=SimpleNamespace(user_id="u-save"))
        wrapper.server.save_token({"access_token": "a", "refresh_token": "r"}, request)

        added = session.add.call_args.args[0]
        assert isinstance(added, OAuth2Token)
        assert added.user_id == "u-save"


class TestServerWrapperDelegation:
    """The thin public methods forward to the wrapped Authlib server."""

    def _wrapper(self) -> OAuth2AuthorizationServer:
        wrapper = OAuth2AuthorizationServer.__new__(OAuth2AuthorizationServer)
        wrapper.db_session = MagicMock()
        wrapper.server = MagicMock()
        return wrapper

    def test_get_consent_grant_delegates(self) -> None:
        wrapper = self._wrapper()
        wrapper.server.get_consent_grant.return_value = "consent"
        result = wrapper.get_consent_grant(request="req", end_user="eu")
        assert result == "consent"
        wrapper.server.get_consent_grant.assert_called_once_with(request="req", end_user="eu")

    def test_create_authorization_response_delegates(self) -> None:
        wrapper = self._wrapper()
        wrapper.server.create_authorization_response.return_value = "authz-resp"
        result = wrapper.create_authorization_response("req", grant_user="gu")
        assert result == "authz-resp"
        wrapper.server.create_authorization_response.assert_called_once_with("req", grant_user="gu")

    def test_create_token_response_delegates(self) -> None:
        wrapper = self._wrapper()
        wrapper.server.create_token_response.return_value = "tok-resp"
        result = wrapper.create_token_response("req")
        assert result == "tok-resp"
        wrapper.server.create_token_response.assert_called_once_with("req")

    def test_validate_consent_request_delegates(self) -> None:
        wrapper = self._wrapper()
        wrapper.server.validate_consent_request.return_value = "validated"
        result = wrapper.validate_consent_request("req")
        assert result == "validated"
        wrapper.server.validate_consent_request.assert_called_once_with("req")


class TestFactory:
    def test_create_authorization_server_returns_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "get_settings", _SettingsStub)
        session = MagicMock()

        server = create_authorization_server(session)

        assert isinstance(server, OAuth2AuthorizationServer)
        assert server.db_session is session


# ===========================================================================
# _OAuthUser
# ===========================================================================


class TestOAuthUser:
    def test_defaults(self) -> None:
        user = _OAuthUser()
        assert user.user_id == ""
        assert user.email is None
        assert user.get_user_id() == ""

    def test_with_values(self) -> None:
        user = _OAuthUser(user_id="sub-1", email="a@b.com")
        assert user.get_user_id() == "sub-1"
        assert user.email == "a@b.com"


# ===========================================================================
# DeviceAuthorizationGrant
# ===========================================================================


class TestDeviceAuthorizationGrant:
    """Polling-loop grant: credential lookup, user-grant resolution, backoff."""

    def test_generate_token_without_user_skips_identity(self) -> None:
        grant = _make_device_grant()
        grant.request = MagicMock()
        grant.request.client = _make_client()
        grant.GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

        token = grant.generate_token(scope="memory:read")

        assert token["token_type"] == "Bearer"
        assert token["scope"] == "memory:read"
        # No user → no identity lookup, so no user_email/workspace keys.
        assert "user_email" not in token

    @staticmethod
    def _stub_identity_lookups(grant: Any, user_row: Any, workspace: Any) -> None:
        """Wire ``db_session.query(...)`` to distinct chains for User vs Workspace.

        Workspace uses the longer ``filter_by(...).filter(...).first()`` chain
        because production filters soft-deleted rows.
        """
        from models.auth import User

        user_chain = MagicMock()
        user_chain.filter_by.return_value.first.return_value = user_row
        ws_chain = MagicMock()
        ws_chain.filter_by.return_value.filter.return_value.first.return_value = workspace
        grant.server.db_session.query.side_effect = lambda model: (
            user_chain if model is User else ws_chain
        )

    def _device_grant_with_request(self) -> DeviceAuthorizationGrant:
        grant = _make_device_grant()
        grant.request = MagicMock()
        grant.request.client = _make_client()
        grant.GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
        return grant

    def test_generate_token_injects_identity(self) -> None:
        from uuid import uuid4

        grant = self._device_grant_with_request()
        ws_uuid = uuid4()
        user_row = MagicMock(email="dave@example.com", current_workspace_id=ws_uuid)
        workspace = MagicMock(id=ws_uuid)
        workspace.name = "dave-ws"
        self._stub_identity_lookups(grant, user_row, workspace)

        token = grant.generate_token(user=MagicMock(user_id="sub-dave"), scope="memory:read")

        assert token["user_email"] == "dave@example.com"
        assert token["workspace_id"] == str(ws_uuid)
        assert token["workspace_name"] == "dave-ws"

    def test_generate_token_skips_soft_deleted_workspace(self) -> None:
        from uuid import uuid4

        grant = self._device_grant_with_request()
        user_row = MagicMock(email="erin@example.com", current_workspace_id=uuid4())
        # workspace=None mimics the soft-delete filter excluding the row.
        self._stub_identity_lookups(grant, user_row, None)

        token = grant.generate_token(user=MagicMock(user_id="sub-erin"), scope="memory:read")

        assert token["user_email"] == "erin@example.com"
        assert "workspace_id" not in token
        assert "workspace_name" not in token

    def test_generate_token_user_without_current_workspace(self) -> None:
        grant = self._device_grant_with_request()
        user_row = MagicMock(email="frank@example.com", current_workspace_id=None)
        self._stub_identity_lookups(grant, user_row, None)

        token = grant.generate_token(user=MagicMock(user_id="sub-frank"), scope="memory:read")

        assert token["user_email"] == "frank@example.com"
        assert "workspace_id" not in token

    def test_generate_token_user_row_not_found(self) -> None:
        grant = self._device_grant_with_request()
        # user_id present but no matching User row → identity skipped entirely.
        self._stub_identity_lookups(grant, None, None)

        token = grant.generate_token(user=MagicMock(user_id="sub-ghost"), scope="memory:read")

        assert "user_email" not in token
        assert "workspace_id" not in token

    def test_query_device_credential_found(self) -> None:
        grant = _make_device_grant()
        device = _make_device()
        grant.server.db_session.query().filter_by().first.return_value = device

        assert grant.query_device_credential("dev-code-abc") is device

    def test_query_device_credential_not_found(self) -> None:
        grant = _make_device_grant()
        grant.server.db_session.query().filter_by().first.return_value = None

        assert grant.query_device_credential("missing") is None

    def test_query_user_grant_authorized(self) -> None:
        grant = _make_device_grant()
        device = _make_device(authorized_at=utcnow() - timedelta(seconds=10), user_id="user-ok")
        grant.server.db_session.query().filter_by().first.return_value = device

        user, approved = grant.query_user_grant("ABCD1234")
        assert approved is True
        assert user.get_user_id() == "user-ok"

    def test_query_user_grant_denied(self) -> None:
        grant = _make_device_grant()
        device = _make_device(denied_at=utcnow() - timedelta(seconds=5))
        grant.server.db_session.query().filter_by().first.return_value = device

        _user, approved = grant.query_user_grant("ABCD1234")
        assert approved is False

    def test_query_user_grant_pending_returns_none(self) -> None:
        grant = _make_device_grant()
        device = _make_device()  # neither authorized nor denied
        grant.server.db_session.query().filter_by().first.return_value = device

        assert grant.query_user_grant("ABCD1234") is None

    def test_query_user_grant_expired_returns_none(self) -> None:
        grant = _make_device_grant()
        device = _make_device(expires_at=utcnow() - timedelta(seconds=1))
        grant.server.db_session.query().filter_by().first.return_value = device

        assert grant.query_user_grant("ABCD1234") is None

    def test_query_user_grant_not_found(self) -> None:
        grant = _make_device_grant()
        grant.server.db_session.query().filter_by().first.return_value = None

        assert grant.query_user_grant("NONEXIST") is None

    def test_should_slow_down_first_poll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "get_settings", _SettingsStub)
        grant = _make_device_grant()
        device = _make_device(last_polled_at=None)

        assert grant.should_slow_down(device) is False
        assert device.last_polled_at is not None

    def test_should_slow_down_rapid_poll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "get_settings", _SettingsStub)
        grant = _make_device_grant()
        device = _make_device(last_polled_at=utcnow() - timedelta(seconds=1))

        # polling_interval is 5s; 1s elapsed → slow down.
        assert grant.should_slow_down(device) is True

    def test_should_slow_down_after_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "get_settings", _SettingsStub)
        grant = _make_device_grant()
        device = _make_device(last_polled_at=utcnow() - timedelta(seconds=10))

        assert grant.should_slow_down(device) is False


# ===========================================================================
# Inner CustomAuthorizationServer methods
# ===========================================================================


class TestCustomAuthorizationServerMethods:
    """create_oauth2_request / handle_response / send_signal on the inner server."""

    def _inner_server(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setattr(mod, "get_settings", _SettingsStub)
        return OAuth2AuthorizationServer(MagicMock()).server

    def test_create_oauth2_request_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from authlib.oauth2 import OAuth2Request

        server = self._inner_server(monkeypatch)
        # An already-OAuth2Request instance is returned unchanged.
        existing = OAuth2Request("POST", "https://example.com/token")
        assert server.create_oauth2_request(existing) is existing

    def test_create_oauth2_request_wraps_starlette(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from starlette.requests import Request as StarletteRequest

        from auth.starlette_oauth2_request import StarletteOAuth2Request

        server = self._inner_server(monkeypatch)
        # Use an https scheme so Authlib's StarletteOAuth2Request does not raise
        # InsecureTransportError during construction.
        scope = {
            "type": "http",
            "scheme": "https",
            "method": "POST",
            "path": "/token",
            "server": ("example.com", 443),
            "query_string": b"",
            "headers": [],
        }
        starlette_req = StarletteRequest(scope)

        wrapped = server.create_oauth2_request(starlette_req)
        assert isinstance(wrapped, StarletteOAuth2Request)

    def test_create_oauth2_request_rejects_unknown_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = self._inner_server(monkeypatch)
        with pytest.raises(TypeError, match="Unsupported request type"):
            server.create_oauth2_request(object())

    def test_handle_response_builds_simple_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = self._inner_server(monkeypatch)
        headers = [("Content-Type", "application/json"), ("Location", "https://cb?code=x")]

        resp = server.handle_response(302, '{"a": 1}', headers)

        assert resp.status_code == 302
        assert resp.body == '{"a": 1}'
        assert resp.headers == headers
        # Location header extracted (case-insensitive) for redirect handling.
        assert resp.location == "https://cb?code=x"

    def test_handle_response_no_location(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = self._inner_server(monkeypatch)
        resp = server.handle_response(200, "body", [("Content-Type", "application/json")])
        assert resp.location is None

    def test_send_signal_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = self._inner_server(monkeypatch)
        # No-op: returns None, does not raise, accepts arbitrary kwargs.
        assert server.send_signal("after_authenticate_client", client="c", grant="g") is None
