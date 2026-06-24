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
- both verifiers fail -> AuthenticationError
- cookie parsing edge cases (no cookie, bad data, missing/non-str user_id,
  UnicodeDecodeError, generic exception, "sub" fallback).

All external I/O (DB session via ``get_db``, Redis ``SessionManager``,
``verify_api_key``, ``verify_oauth_bearer_token``) is mocked -- no network,
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
    _verify_api_key,
    _verify_oauth2_token,
    _verify_session_cookie,
    authenticate_mcp_request,
)
from utils.exceptions import AuthenticationError

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
            result = await authenticate_mcp_request("Bearer key_validkey")
        assert result == ("u-1", None, ws)

    async def test_api_key_success_bytes_header(self):
        """Bytes Authorization header is decoded and authenticates."""
        with patch(
            "auth.dependencies.verify_api_key",
            new=AsyncMock(return_value=_verified_key("u-bytes", None)),
        ):
            result = await authenticate_mcp_request(b"Bearer key_bytes")
        assert result == ("u-bytes", None, None)

    async def test_invalid_key_falls_to_oauth_then_fails(self):
        """API key None AND OAuth None -> AuthenticationError (invalid/expired)."""
        with (
            patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=None)),
            patch("mcp_server.auth._verify_oauth2_token", new=AsyncMock(return_value=None)),
        ):
            with pytest.raises(AuthenticationError, match="Invalid or expired"):
                await authenticate_mcp_request("Bearer key_bogus")


class TestAuthenticateMcpRequestOAuth2:
    """OAuth2 Bearer token branch (key verification returns None first)."""

    async def test_oauth2_success_returns_user_only(self):
        """API key None, OAuth valid -> (user_id, None, None)."""
        with (
            patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=None)),
            patch(
                "mcp_server.auth._verify_oauth2_token",
                new=AsyncMock(return_value="oauth-user"),
            ),
        ):
            result = await authenticate_mcp_request("Bearer opaque_oauth_token")
        assert result == ("oauth-user", None, None)


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

    async def test_exception_is_swallowed_to_none(self):
        """Any exception in verification -> None (graceful degradation)."""
        with patch(
            "auth.dependencies.verify_api_key",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            assert await _verify_api_key("key_x") is None


class TestVerifyOauth2Token:
    """Direct tests of the ``_verify_oauth2_token`` shim (own DB session)."""

    async def test_valid_token_returns_user_id(self):
        """verify_oauth_bearer_token returns (user_id, scope) -> user_id."""
        db = AsyncMock()
        with (
            patch("db.base.get_db", new=_mock_get_db(db)),
            patch(
                "auth.oauth2_bearer.verify_oauth_bearer_token",
                new=AsyncMock(return_value=("oauth-uid", "read write")),
            ),
        ):
            assert await _verify_oauth2_token("tok") == "oauth-uid"

    async def test_invalid_token_returns_none(self):
        """verify_oauth_bearer_token None -> None."""
        db = AsyncMock()
        with (
            patch("db.base.get_db", new=_mock_get_db(db)),
            patch(
                "auth.oauth2_bearer.verify_oauth_bearer_token",
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
