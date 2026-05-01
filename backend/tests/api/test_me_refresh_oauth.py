"""Route-level tests for POST /api/v1/me/refresh-oauth (Issue #515).

The endpoint takes the current user's stored ``auth_provider``, generates
a CSRF state token, persists it alongside an intent marker and the
originating user_id, and returns the IdP authorization URL. The actual
OAuth round-trip is exercised by the callback tests in
``tests/api/test_auth_refresh_callback.py``; here we pin only the
endpoint contract: input validation, rate limiting, and the four Redis
keys the callback later reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes import me_oauth
from api.routes.me_oauth import (
    RefreshOAuthRequest,
    RefreshOAuthResponse,
    refresh_oauth,
)


def _session(*, user_id: str = "u-1") -> dict:
    """Minimal SessionUser dict — only the fields the handler reads."""
    return {"user_id": user_id}


def _db_user(*, auth_method: str, auth_provider: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        auth_method=auth_method,
        auth_provider=auth_provider,
    )


def _mock_db(db_user: SimpleNamespace | None) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = db_user
    db.execute.return_value = result
    return db


def _mock_managers():
    """Patch the auth module's manager singletons that ``me_oauth`` reads.

    Returns the redis mock so tests can assert on the four state keys
    the endpoint must write: ``oauth2_state:*``, ``oauth2_state_intent:*``,
    ``oauth2_state_user:*``, ``oauth2_return_to:*``.
    """
    redis = MagicMock()
    session_manager = MagicMock()
    session_manager._redis = redis

    oauth2_manager = MagicMock()
    oauth2_manager.get_authorization_url_web.return_value = (
        "https://accounts.google.com/o/oauth2/auth?state=...&..."
    )
    return session_manager, oauth2_manager, redis


class TestRefreshOAuthValidation:
    """Input validation paths — no Redis writes expected when the user
    cannot legitimately initiate a refresh."""

    @pytest.mark.asyncio
    async def test_password_user_rejected_400(self):
        """auth_method='password' → 400 with explanation."""
        session_manager, oauth2_manager, redis = _mock_managers()
        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="password", auth_provider=None))
            with pytest.raises(Exception) as exc_info:
                await refresh_oauth(
                    payload=RefreshOAuthRequest(),
                    user=_session(),
                    db=db,
                )
            assert exc_info.value.status_code == 400  # type: ignore[attr-defined]
        # No Redis writes on the rejected path.
        redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_oauth_user_with_null_provider_rejected_400(self):
        """Pre-#361 OAuth user (auth_method='oauth' but auth_provider IS NULL)
        → 400 prompting them to log out and back in."""
        session_manager, oauth2_manager, redis = _mock_managers()
        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider=None))
            with pytest.raises(Exception) as exc_info:
                await refresh_oauth(
                    payload=RefreshOAuthRequest(),
                    user=_session(),
                    db=db,
                )
            assert exc_info.value.status_code == 400  # type: ignore[attr-defined]
        redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_db_row_returns_404(self):
        """Phantom session (DB row deleted out from under us) → 404."""
        session_manager, oauth2_manager, redis = _mock_managers()
        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(None)
            with pytest.raises(Exception) as exc_info:
                await refresh_oauth(
                    payload=RefreshOAuthRequest(),
                    user=_session(),
                    db=db,
                )
            assert exc_info.value.status_code == 404  # type: ignore[attr-defined]


class TestRefreshOAuthHappyPath:
    @pytest.mark.asyncio
    async def test_google_user_returns_authorization_url_and_state(self, monkeypatch):
        """Google OAuth user → response carries the authorization URL +
        state. The endpoint must persist all four state keys."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
            result = await refresh_oauth(
                payload=RefreshOAuthRequest(),
                user=_session(user_id="u-google"),
                db=db,
            )

        assert isinstance(result, RefreshOAuthResponse)
        assert result.authorization_url.startswith("https://accounts.google.com/")
        assert result.state  # non-empty CSRF token

        # All four Redis keys were written with TTL=300 (matches the
        # state TTL the existing google_callback expects).
        keys_written = [call.args[0] for call in redis.setex.call_args_list]
        assert any(k == f"oauth2_state:{result.state}" for k in keys_written)
        assert any(k == f"oauth2_state_intent:{result.state}" for k in keys_written)
        assert any(k == f"oauth2_state_user:{result.state}" for k in keys_written)
        assert any(k == f"oauth2_return_to:{result.state}" for k in keys_written)

        # Intent and user_id values are pinned literally — these are what
        # _maybe_refresh_redirect reads in the callback.
        intent_call = next(
            c
            for c in redis.setex.call_args_list
            if c.args[0] == f"oauth2_state_intent:{result.state}"
        )
        assert intent_call.args[2] == "refresh"
        user_call = next(
            c
            for c in redis.setex.call_args_list
            if c.args[0] == f"oauth2_state_user:{result.state}"
        )
        assert user_call.args[2] == "u-google"

    @pytest.mark.asyncio
    async def test_github_user_returns_github_authorization_url(self, monkeypatch):
        """GitHub OAuth user → URL points at github.com/login/oauth/authorize
        and includes ``state=`` query."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client")
        monkeypatch.setenv(
            "GITHUB_REDIRECT_URI", "http://localhost:8080/api/v1/auth/github/callback"
        )

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="github"))
            result = await refresh_oauth(
                payload=RefreshOAuthRequest(),
                user=_session(user_id="u-gh"),
                db=db,
            )

        assert "github.com/login/oauth/authorize" in result.authorization_url
        assert f"state={result.state}" in result.authorization_url

    @pytest.mark.asyncio
    async def test_custom_return_to_persisted_to_redis(self, monkeypatch):
        """Frontend can override the post-callback landing page."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
            result = await refresh_oauth(
                payload=RefreshOAuthRequest(return_to="/profile?refreshed=1&from=button"),
                user=_session(),
                db=db,
            )

        return_to_call = next(
            c for c in redis.setex.call_args_list if c.args[0] == f"oauth2_return_to:{result.state}"
        )
        assert return_to_call.args[2] == "/profile?refreshed=1&from=button"


class TestRefreshOAuthReturnToValidation:
    """Block open-redirect via crafted ``return_to`` (defence in depth —
    SameSite=Lax + CORS already block cross-origin POST exploitation)."""

    @pytest.mark.parametrize(
        "bad_value",
        [
            "//evil.com/x",  # protocol-relative
            "https://evil.com",  # absolute URL
            "http://evil.com",  # absolute URL
            "javascript:alert(1)",  # data scheme
            "/\\evil.com",  # backslash trick
            "/%2F%2Fevil.com",  # URL-encoded protocol-relative
            "/%5Cevil.com",  # URL-encoded backslash
            "",  # empty
            "   ",  # whitespace
            "profile",  # missing leading slash
            "/profile\r\nLocation: https://evil.com",  # CRLF injection
            "/profile\nX-Header: x",  # bare LF
            "/profile\r",  # bare CR
            "/profile\x00null",  # NUL byte
            "/profile\x07bell",  # BEL (C0 control)
        ],
    )
    @pytest.mark.asyncio
    async def test_unsafe_return_to_rejected_400(self, bad_value: str, monkeypatch):
        """Each of these shapes must 400 before any Redis writes happen."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
            with pytest.raises(Exception) as exc_info:
                await refresh_oauth(
                    payload=RefreshOAuthRequest(return_to=bad_value),
                    user=_session(),
                    db=db,
                )
            assert exc_info.value.status_code == 400  # type: ignore[attr-defined]
        # The validator runs before any Redis write — no oauth2_state* keys
        # may leak when an attack payload arrives.
        redis.setex.assert_not_called()

    @pytest.mark.parametrize(
        "good_value",
        [
            "/profile",
            "/profile?refreshed=1",
            "/profile?refreshed=1&from=button",
            "/workspace/dashboard",
        ],
    )
    @pytest.mark.asyncio
    async def test_safe_return_to_accepted(self, good_value: str, monkeypatch):
        """Same-origin relative paths must continue to flow through unchanged."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
            result = await refresh_oauth(
                payload=RefreshOAuthRequest(return_to=good_value),
                user=_session(),
                db=db,
            )
        return_to_call = next(
            c for c in redis.setex.call_args_list if c.args[0] == f"oauth2_return_to:{result.state}"
        )
        assert return_to_call.args[2] == good_value

    @pytest.mark.asyncio
    async def test_return_to_whitespace_is_normalized_before_persist(self, monkeypatch):
        """Outer whitespace is stripped before the value reaches Redis,
        so the eventual ``Location`` header carries no padding."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
            result = await refresh_oauth(
                payload=RefreshOAuthRequest(return_to="  /profile?refreshed=1  "),
                user=_session(),
                db=db,
            )
        return_to_call = next(
            c for c in redis.setex.call_args_list if c.args[0] == f"oauth2_return_to:{result.state}"
        )
        # Persisted value is the trimmed form, NOT the raw input.
        assert return_to_call.args[2] == "/profile?refreshed=1"


class TestRefreshOAuthConfigGuardOrdering:
    """Provider-specific config (GOOGLE_REDIRECT_URI / GITHUB_CLIENT_ID)
    must be validated BEFORE any Redis write so a missing env var doesn't
    leave four orphaned ``oauth2_state*`` keys behind for 5 minutes
    (Copilot review #3)."""

    @pytest.mark.asyncio
    async def test_missing_google_redirect_uri_does_not_pollute_redis(self, monkeypatch):
        """Google flow with GOOGLE_REDIRECT_URI unset → 500, no setex."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
            with pytest.raises(Exception) as exc_info:
                await refresh_oauth(
                    payload=RefreshOAuthRequest(),
                    user=_session(),
                    db=db,
                )
            assert exc_info.value.status_code == 500  # type: ignore[attr-defined]
        redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_github_client_id_does_not_pollute_redis(self, monkeypatch):
        """GitHub flow with GITHUB_CLIENT_ID unset → 500, no setex."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=1)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="github"))
            with pytest.raises(Exception) as exc_info:
                await refresh_oauth(
                    payload=RefreshOAuthRequest(),
                    user=_session(),
                    db=db,
                )
            assert exc_info.value.status_code == 500  # type: ignore[attr-defined]
        redis.setex.assert_not_called()


class TestRefreshOAuthRateLimit:
    @pytest.mark.asyncio
    async def test_second_call_in_same_minute_returns_429(self, monkeypatch):
        """``increment_counter`` returning 2 → 429 with Retry-After header.

        The handler treats the counter value as the source of truth: if
        it sees > 1, the user has already burned their per-minute budget.
        """
        session_manager, oauth2_manager, _redis = _mock_managers()
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(me_oauth, "increment_counter", AsyncMock(return_value=2)),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
            with pytest.raises(Exception) as exc_info:
                await refresh_oauth(
                    payload=RefreshOAuthRequest(),
                    user=_session(),
                    db=db,
                )

        assert exc_info.value.status_code == 429  # type: ignore[attr-defined]
        # Retry-After is a soft hint to the frontend; pin it so we
        # don't accidentally drop the header on a future refactor.
        assert exc_info.value.headers.get("Retry-After") == "60"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_redis_failure_does_not_fail_the_request(self, monkeypatch):
        """If ``increment_counter`` raises, we fall through with count=1
        rather than 503ing the user. The trade-off: a Redis outage means
        the rate limit goes dark, but the OAuth flow keeps working."""
        session_manager, oauth2_manager, _redis = _mock_managers()
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        with (
            patch.object(me_oauth.auth_module, "_session_manager", session_manager),
            patch.object(me_oauth.auth_module, "_oauth2_manager", oauth2_manager),
            patch.object(
                me_oauth,
                "increment_counter",
                AsyncMock(side_effect=RuntimeError("redis down")),
            ),
        ):
            db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
            result = await refresh_oauth(
                payload=RefreshOAuthRequest(),
                user=_session(),
                db=db,
            )
        assert isinstance(result, RefreshOAuthResponse)
