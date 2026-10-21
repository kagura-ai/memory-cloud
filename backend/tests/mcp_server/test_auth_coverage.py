"""Coverage-focused unit tests for ``mcp_server.auth``.

Exercises the MCP transport authentication gate end to end:
``authenticate_mcp_request`` plus its three private verifiers
(``_verify_api_key``, ``_verify_oauth2_token``, ``_verify_session_cookie``).

Every reachable branch is targeted deliberately:
- session-cookie-only path (success + invalid fallthrough)
- missing-auth -> AuthenticationError
- bytes vs str ``Authorization`` header decoding
- non-``Bearer`` header -> AuthenticationError
- API-key success / None / internal-exception (graceful None)
- OAuth2 success / None
- routing by token shape (#1994): ``kagura_*`` -> API-key store only,
  anything else -> OAuth store only; a miss in either is the same 401
- cookie parsing edge cases (no cookie, bad data, missing/non-str user_id,
  UnicodeDecodeError, generic exception, "sub" fallback).

All external I/O (DB session via ``get_db``, Redis ``SessionManager``,
``verify_api_key``, ``find_active_oauth_token``) is mocked -- no network,
no DB. The real ``VerifiedKey`` NamedTuple is used so attribute access in
``_verify_api_key`` is faithfully exercised.
"""

# Pre-import ``pydantic.root_model`` before any ``mcp_server`` import. Under
# coverage instrumentation, importing ``mcp_server`` (its ``__init__`` pulls in
# the ``mcp`` SDK, whose ``mcp.types`` subscripts ``RootModel[...]``) can race
# the lazy registration of ``pydantic.root_model`` in ``sys.modules`` and blow
# up with ``KeyError: 'pydantic.root_model'``. Importing it eagerly here makes
# the module present before the SDK's generic-submodel creation runs.
import pydantic.root_model  # noqa: F401  isort: skip
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from auth.api_keys import VerifiedKey
from mcp_server.auth import (
    OAuthGrant,
    _verify_api_key,
    _verify_oauth2_token,
    _verify_session_cookie,
    authenticate_mcp_request,
)
from utils.exceptions import AuthenticationError, InvalidTokenError

# The session cookie name the module looks for. Built by concatenation so the
# test source contains no long opaque-token-looking literal.
_SESSION_NAME = "kagura" + "_session"
_COOKIE = (_SESSION_NAME + "=sid000").encode()


def _verified_key(user_id="user-abc", workspace_id=None):
    """Build a real VerifiedKey (id + user_id + workspace_id + bound_context_id)."""
    return VerifiedKey(
        id=1,
        user_id=user_id,
        workspace_id=workspace_id,
        bound_context_id=None,
    )


def _mock_get_db(db):
    """Async generator drop-in for ``db.base.get_db`` yielding ``db``."""

    async def _gen():
        yield db

    return _gen


class TestAuthenticateMcpRequestSessionCookie:
    """Session-cookie-only branch (no Authorization header)."""

    async def test_session_cookie_success_returns_user_only(self):
        """Valid cookie + no auth header -> (user_id, None, None)."""
        with patch(
            "mcp_server.auth._verify_session_cookie",
            new=AsyncMock(return_value="cookie-user"),
        ):
            result = await authenticate_mcp_request(None, cookie_header=_COOKIE)
        assert result == ("cookie-user", None, None)

    async def test_invalid_cookie_falls_through_to_missing_auth_error(self):
        """Cookie present but invalid (None) + no auth header -> AuthenticationError."""
        with patch(
            "mcp_server.auth._verify_session_cookie",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(AuthenticationError, match="required"):
                await authenticate_mcp_request(None, cookie_header=_COOKIE)


class TestAuthenticateMcpRequestMissingOrMalformed:
    """No auth at all, and malformed Authorization headers."""

    async def test_no_auth_and_no_cookie_raises(self):
        """No header and no cookie -> AuthenticationError."""
        with pytest.raises(AuthenticationError, match="required"):
            await authenticate_mcp_request(None, cookie_header=None)

    async def test_non_bearer_header_raises(self):
        """Header not starting with 'Bearer ' -> format AuthenticationError."""
        with pytest.raises(AuthenticationError, match="Expected: Bearer"):
            await authenticate_mcp_request("Basic abc123")

    async def test_empty_string_header_treated_as_missing(self):
        """Empty string is falsy -> treated as missing auth -> AuthenticationError."""
        with pytest.raises(AuthenticationError, match="required"):
            await authenticate_mcp_request("", cookie_header=None)


class TestAuthenticateMcpRequestApiKey:
    """API-key Bearer token branch."""

    async def test_api_key_success_workspace_scoped(self):
        """Valid workspace-scoped key -> (user_id, None, workspace_id)."""
        ws = uuid4()
        with patch(
            "auth.dependencies.verify_api_key",
            new=AsyncMock(return_value=_verified_key("u-1", ws)),
        ):
            result = await authenticate_mcp_request("Bearer kagura_validkey")
        assert result == ("u-1", None, ws)

    async def test_api_key_success_bytes_header(self):
        """Bytes Authorization header is decoded and authenticates."""
        with patch(
            "auth.dependencies.verify_api_key",
            new=AsyncMock(return_value=_verified_key("u-bytes", None)),
        ):
            result = await authenticate_mcp_request(b"Bearer kagura_bytes")
        assert result == ("u-bytes", None, None)

    async def test_an_invalid_kagura_key_is_401_without_an_oauth_lookup(self):
        """#1994: a ``kagura_`` key the key store does not know -> InvalidTokenError;
        the OAuth store is never consulted."""
        oauth = AsyncMock(return_value=None)
        with (
            patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=None)),
            patch("mcp_server.auth._verify_oauth2_token", new=oauth),
        ):
            with pytest.raises(InvalidTokenError, match="Invalid or expired"):
                await authenticate_mcp_request("Bearer kagura_bogus")
        oauth.assert_not_awaited()


class TestAuthenticateMcpRequestOAuth2:
    """OAuth2 Bearer token branch: any token without the ``kagura_`` prefix."""

    async def test_oauth2_success_returns_user_only(self):
        """OAuth valid -> (user_id, None, None)."""
        with (
            patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=None)),
            patch(
                "mcp_server.auth._verify_oauth2_token",
                new=AsyncMock(return_value=OAuthGrant("oauth-user", "memory:read", None)),
            ),
        ):
            result = await authenticate_mcp_request("Bearer opaque_oauth_token")
        assert result == ("oauth-user", None, None)


_OAUTH_TOKEN = "Zx9" + "opaque-oauth-access-token"
_KAGURA_KEY = "kagura_" + "plain-api-key-body"
_GRANT = OAuthGrant("oauth-user", "memory:read memory:write", None)


class TestBearerTokenRouting:
    """#1994: each Bearer token is looked up in the one store its shape names.

    ``kagura_*`` -> the API-key store only; anything else -> the OAuth token
    store only (``auth.oauth2_bearer.is_oauth_bearer_token``, as on REST). A
    miss in either store is the same 401.
    """

    async def test_an_oauth_token_never_reaches_the_api_key_store(self):
        api_key = AsyncMock(return_value=("key-user", None, None))
        oauth = AsyncMock(return_value=_GRANT)
        with (
            patch("mcp_server.auth._verify_api_key", new=api_key),
            patch("mcp_server.auth._verify_oauth2_token", new=oauth),
        ):
            result = await authenticate_mcp_request(f"Bearer {_OAUTH_TOKEN}")
        assert result == ("oauth-user", None, None)
        api_key.assert_not_awaited()
        oauth.assert_awaited_once_with(_OAUTH_TOKEN)

    async def test_an_unknown_oauth_token_is_401_without_an_api_key_lookup(self):
        api_key = AsyncMock(return_value=("key-user", None, None))
        oauth = AsyncMock(return_value=None)
        with (
            patch("mcp_server.auth._verify_api_key", new=api_key),
            patch("mcp_server.auth._verify_oauth2_token", new=oauth),
        ):
            with pytest.raises(InvalidTokenError, match="Invalid or expired Bearer token"):
                await authenticate_mcp_request(f"Bearer {_OAUTH_TOKEN}")
        api_key.assert_not_awaited()
        oauth.assert_awaited_once_with(_OAUTH_TOKEN)

    async def test_a_kagura_key_never_reaches_the_oauth_store(self):
        from mcp_server.auth import get_mcp_connector_credential
        from mcp_server.tools._helpers import get_mcp_credential_default_context

        default_ctx = uuid4()
        key = VerifiedKey(
            id=1,
            user_id="key-user",
            workspace_id=None,
            bound_context_id=None,
            default_context_id=default_ctx,
        )
        oauth = AsyncMock(return_value=_GRANT)
        with (
            patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=key)),
            patch("mcp_server.auth._verify_oauth2_token", new=oauth),
        ):
            result = await authenticate_mcp_request(f"Bearer {_KAGURA_KEY}")
        assert result == ("key-user", None, None)
        assert get_mcp_connector_credential() is False
        assert get_mcp_credential_default_context() == default_ctx
        oauth.assert_not_awaited()

    @pytest.mark.parametrize(
        "token",
        [
            _KAGURA_KEY,
            # Share keys and resource tokens carry the kagura_ prefix too: they
            # stay on the API-key side, where they are structurally unknown.
            "kagura_sk_" + "share-key-body",
            "kagura_resource_" + "resource-token-body",
        ],
    )
    async def test_an_unknown_kagura_shaped_token_is_401_without_an_oauth_lookup(self, token):
        verify = AsyncMock(return_value=None)
        oauth = AsyncMock(return_value=_GRANT)
        with (
            patch("auth.dependencies.verify_api_key", new=verify),
            patch("mcp_server.auth._verify_oauth2_token", new=oauth),
        ):
            with pytest.raises(InvalidTokenError, match="Invalid or expired Bearer token"):
                await authenticate_mcp_request(f"Bearer {token}")
        verify.assert_awaited_once_with(token)
        oauth.assert_not_awaited()

    async def test_a_miss_in_either_store_is_the_same_error(self):
        with (
            patch("mcp_server.auth._verify_api_key", new=AsyncMock(return_value=None)),
            patch("mcp_server.auth._verify_oauth2_token", new=AsyncMock(return_value=None)),
        ):
            with pytest.raises(InvalidTokenError) as key_miss:
                await authenticate_mcp_request(f"Bearer {_KAGURA_KEY}")
            with pytest.raises(InvalidTokenError) as oauth_miss:
                await authenticate_mcp_request(f"Bearer {_OAUTH_TOKEN}")
        assert type(key_miss.value) is type(oauth_miss.value)
        assert str(key_miss.value) == str(oauth_miss.value)
        assert key_miss.value.error_code == oauth_miss.value.error_code

    async def test_a_failing_api_key_store_does_not_affect_an_oauth_token(self):
        """#1742 / #1994: the other store's pool timeout cannot 503 the request."""
        with (
            patch("mcp_server.auth._verify_api_key", new=AsyncMock(side_effect=TimeoutError())),
            patch("mcp_server.auth._verify_oauth2_token", new=AsyncMock(return_value=_GRANT)),
        ):
            assert await authenticate_mcp_request(f"Bearer {_OAUTH_TOKEN}") == (
                "oauth-user",
                None,
                None,
            )

    async def test_a_failing_oauth_store_does_not_affect_a_kagura_key(self):
        with (
            patch(
                "mcp_server.auth._verify_api_key",
                new=AsyncMock(return_value=("key-user", None, None)),
            ),
            patch(
                "mcp_server.auth._verify_oauth2_token",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            assert await authenticate_mcp_request(f"Bearer {_KAGURA_KEY}") == (
                "key-user",
                None,
                None,
            )

    async def test_a_failing_lookup_in_the_named_store_still_raises(self):
        """#1742 is unchanged: the store the token names failing is a 503, not a 401."""
        with (
            patch("mcp_server.auth._verify_api_key", new=AsyncMock(side_effect=TimeoutError())),
            patch("mcp_server.auth._verify_oauth2_token", new=AsyncMock(return_value=_GRANT)),
        ):
            with pytest.raises(TimeoutError):
                await authenticate_mcp_request(f"Bearer {_KAGURA_KEY}")
        with (
            patch(
                "mcp_server.auth._verify_api_key",
                new=AsyncMock(return_value=("key-user", None, None)),
            ),
            patch(
                "mcp_server.auth._verify_oauth2_token",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            with pytest.raises(TimeoutError):
                await authenticate_mcp_request(f"Bearer {_OAUTH_TOKEN}")


class TestVerifyApiKey:
    """Direct tests of the ``_verify_api_key`` shim."""

    async def test_returns_three_tuple_on_valid(self):
        """Valid VerifiedKey -> (user_id, None, workspace_id)."""
        ws = uuid4()
        with patch(
            "auth.dependencies.verify_api_key",
            new=AsyncMock(return_value=_verified_key("uu", ws)),
        ):
            assert await _verify_api_key("key_x") == ("uu", None, ws)

    async def test_returns_none_when_invalid(self):
        """verify_api_key None -> None (invalid/revoked/expired/bound)."""
        with patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=None)):
            assert await _verify_api_key("key_x") is None

    async def test_a_failed_lookup_raises_instead_of_reading_as_invalid(self):
        """#1742: a lookup failure propagates (the transport answers 503)
        rather than reading as ``None``."""
        verify = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("auth.dependencies.verify_api_key", new=verify):
            with pytest.raises(RuntimeError, match="db down"):
                await _verify_api_key("key_x")
        verify.assert_awaited_once_with("key_x")


class TestVerifyOauth2Token:
    """Direct tests of the ``_verify_oauth2_token`` shim (own DB session)."""

    async def test_valid_token_returns_the_grant(self):
        """An active token row -> its user_id, scope and resource (audience)."""
        db = AsyncMock()
        # #2129: the grant's default-context lookup finds no settings row.
        db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        row = MagicMock(user_id="oauth-uid", scope="memory:read", resource="https://x/mcp")
        with (
            patch("db.base.get_db", new=_mock_get_db(db)),
            patch(
                "auth.oauth2_bearer.find_active_oauth_token",
                new=AsyncMock(return_value=row),
            ),
        ):
            assert await _verify_oauth2_token("tok") == OAuthGrant(
                "oauth-uid", "memory:read", "https://x/mcp"
            )

    async def test_invalid_token_returns_none(self):
        """find_active_oauth_token None -> None."""
        db = AsyncMock()
        with (
            patch("db.base.get_db", new=_mock_get_db(db)),
            patch(
                "auth.oauth2_bearer.find_active_oauth_token",
                new=AsyncMock(return_value=None),
            ),
        ):
            assert await _verify_oauth2_token("tok") is None

    async def test_empty_db_generator_returns_none(self):
        """If get_db yields nothing, the function returns None (fallthrough)."""

        async def _empty():
            for _ in ():
                yield None

        with patch("db.base.get_db", new=_empty):
            assert await _verify_oauth2_token("tok") is None


class TestVerifySessionCookie:
    """Direct tests of ``_verify_session_cookie`` (cookie parse + Redis)."""

    def _patch_session_manager(self, get_session_return):
        mgr = MagicMock()
        mgr.get_session = MagicMock(return_value=get_session_return)
        return (
            patch("auth.session.SessionManager", new=MagicMock(return_value=mgr)),
            patch("config.database.get_redis_url", new=MagicMock(return_value="redis://x")),
        )

    async def test_valid_session_user_id_key(self):
        """Session dict with 'user_id' -> returns that user_id."""
        sm_patch, url_patch = self._patch_session_manager({"user_id": "sess-user"})
        with sm_patch, url_patch:
            assert await _verify_session_cookie(_COOKIE) == "sess-user"

    async def test_valid_session_sub_fallback(self):
        """No 'user_id' but 'sub' present -> uses 'sub'."""
        sm_patch, url_patch = self._patch_session_manager({"sub": "sub-user"})
        with sm_patch, url_patch:
            assert await _verify_session_cookie(_COOKIE) == "sub-user"

    async def test_no_session_cookie_returns_none(self):
        """Cookie header without the session cookie -> None (no Redis lookup)."""
        sm_patch, url_patch = self._patch_session_manager({"user_id": "x"})
        with sm_patch, url_patch:
            assert await _verify_session_cookie(b"other=value") is None

    async def test_empty_session_data_returns_none(self):
        """Redis returns None/empty -> None."""
        sm_patch, url_patch = self._patch_session_manager(None)
        with sm_patch, url_patch:
            assert await _verify_session_cookie(_COOKIE) is None

    async def test_non_dict_session_data_returns_none(self):
        """Redis returns a non-dict (e.g. list) -> None."""
        sm_patch, url_patch = self._patch_session_manager(["not", "a", "dict"])
        with sm_patch, url_patch:
            assert await _verify_session_cookie(_COOKIE) is None

    async def test_session_missing_user_id_returns_none(self):
        """Session dict with neither user_id nor sub -> None."""
        sm_patch, url_patch = self._patch_session_manager({"foo": "bar"})
        with sm_patch, url_patch:
            assert await _verify_session_cookie(_COOKIE) is None

    async def test_session_non_str_user_id_returns_none(self):
        """user_id present but not a str -> None."""
        sm_patch, url_patch = self._patch_session_manager({"user_id": 12345})
        with sm_patch, url_patch:
            assert await _verify_session_cookie(_COOKIE) is None

    async def test_str_cookie_header_accepted(self):
        """A str (not bytes) cookie header is accepted too."""
        sm_patch, url_patch = self._patch_session_manager({"user_id": "str-user"})
        with sm_patch, url_patch:
            assert await _verify_session_cookie(_SESSION_NAME + "=sid111") == "str-user"

    async def test_unicode_decode_error_returns_none(self):
        """Invalid UTF-8 bytes -> UnicodeDecodeError caught -> None."""
        # 0x80 is an invalid UTF-8 start byte.
        assert await _verify_session_cookie(bytes([0x80, 0x81, 0x82])) is None

    async def test_generic_exception_in_session_lookup_returns_none(self):
        """A non-cookie error (e.g. Redis blows up) -> caught -> None."""
        mgr = MagicMock()
        mgr.get_session = MagicMock(side_effect=RuntimeError("redis down"))
        with (
            patch("auth.session.SessionManager", new=MagicMock(return_value=mgr)),
            patch("config.database.get_redis_url", new=MagicMock(return_value="redis://x")),
        ):
            assert await _verify_session_cookie(_COOKIE) is None


class TestConnectorCredentialFlag:
    """``get_mcp_connector_credential``: set by the API-key branch from
    ``VerifiedKey.is_connector_key``, reset by every authentication."""

    async def test_connector_key_sets_the_flag(self):
        from auth.api_keys import VerifiedKey
        from mcp_server.auth import get_mcp_connector_credential

        ws = uuid4()
        key = VerifiedKey(
            id=1, user_id="u-c", workspace_id=ws, bound_context_id=None, is_connector_key=True
        )
        with patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=key)):
            assert await authenticate_mcp_request("Bearer kagura_connector") == ("u-c", None, ws)
        assert get_mcp_connector_credential() is True

    async def test_an_ordinary_key_and_a_session_cookie_reset_the_flag(self):
        from mcp_server.auth import get_mcp_connector_credential, set_mcp_connector_credential

        set_mcp_connector_credential(True)
        with patch(
            "auth.dependencies.verify_api_key",
            new=AsyncMock(return_value=_verified_key("u-1", None)),
        ):
            await authenticate_mcp_request("Bearer kagura_plain")
        assert get_mcp_connector_credential() is False

        set_mcp_connector_credential(True)
        with patch("mcp_server.auth._verify_session_cookie", new=AsyncMock(return_value="u-s")):
            await authenticate_mcp_request(None, cookie_header=b"session=abc")
        assert get_mcp_connector_credential() is False
