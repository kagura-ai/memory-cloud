"""Tests for the refresh-mode short-circuit in the OAuth callbacks (Issue #515).

The actual provider flows (Google / GitHub callback handlers) are large
and exercised by smoke + integration suites. This file targets the
small, security-sensitive helper they delegate to —
``api.routes.auth._maybe_refresh_redirect`` — which decides:

1. Is this callback a refresh round-trip (vs a normal login)?
2. Did the IdP return the SAME user that initiated the refresh?
3. Where do we send the browser next?

If any branch is wrong, a refresh either silently logs the user out
(missing redirect path) or — worse — syncs another account's identity
into the originating session. The tests pin all four branches plus
the bytes/str redis-decode robustness assumption.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.responses import RedirectResponse

from api.routes import auth as auth_module
from api.routes.auth import _maybe_refresh_redirect


def _mock_session_manager_with_redis(values: dict[str, str | None]):
    """Build a session_manager whose Redis ``.get(key)`` returns the
    pre-staged value (or None for absent keys), and whose ``.delete``
    is a no-op spy."""
    redis = MagicMock()
    redis.get.side_effect = lambda key: values.get(key)
    redis.delete = MagicMock()
    sm = MagicMock()
    sm._redis = redis
    return sm, redis


class TestMaybeRefreshRedirect:
    @pytest.mark.asyncio
    async def test_no_intent_returns_none_for_normal_login(self):
        """No ``oauth2_state_intent:{state}`` key set → this is a normal
        login callback. Helper returns None so the caller proceeds with
        session creation as before."""
        sm, redis = _mock_session_manager_with_redis({})
        with patch.object(auth_module, "_session_manager", sm):
            result = await _maybe_refresh_redirect(state="s1", idp_sub="u-1")
        assert result is None
        # Helper must NOT touch state/return_to keys when intent is absent —
        # those still belong to the login flow that called us.
        redis.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_intent_login_returns_none(self):
        """A non-refresh intent value (e.g. literal 'login' if some
        future code sets it) is treated as not-refresh — helper returns
        None and leaves state alone."""
        sm, _redis = _mock_session_manager_with_redis({"oauth2_state_intent:s1": "login"})
        with patch.object(auth_module, "_session_manager", sm):
            result = await _maybe_refresh_redirect(state="s1", idp_sub="u-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_with_matching_user_redirects_to_return_to(self, monkeypatch):
        """Happy path: same user came back, return_to was set → redirect
        to that URL."""
        monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
        sm, redis = _mock_session_manager_with_redis(
            {
                "oauth2_state_intent:s1": "refresh",
                "oauth2_state_user:s1": "u-42",
                "oauth2_return_to:s1": "/profile?refreshed=1&from=button",
            }
        )
        with patch.object(auth_module, "_session_manager", sm):
            result = await _maybe_refresh_redirect(state="s1", idp_sub="u-42")

        assert isinstance(result, RedirectResponse)
        assert result.status_code == 303
        assert result.headers["location"] == "/profile?refreshed=1&from=button"
        # The four keys the helper consumes must all be deleted on the
        # happy path (intent, user, return_to). State key itself is
        # cleaned by the surrounding callback before this helper is
        # called, so we don't check it here.
        deleted_keys = {call.args[0] for call in redis.delete.call_args_list}
        assert "oauth2_state_intent:s1" in deleted_keys
        assert "oauth2_state_user:s1" in deleted_keys
        assert "oauth2_return_to:s1" in deleted_keys

    @pytest.mark.asyncio
    async def test_refresh_without_return_to_falls_back_to_default(self, monkeypatch):
        """No ``oauth2_return_to:{state}`` set → default to /profile?refreshed=1.

        Frontend reads the ``refreshed=1`` query string to flash a
        success toast.
        """
        monkeypatch.setenv("FRONTEND_URL", "http://example.com")
        sm, _redis = _mock_session_manager_with_redis(
            {
                "oauth2_state_intent:s1": "refresh",
                "oauth2_state_user:s1": "u-42",
            }
        )
        with patch.object(auth_module, "_session_manager", sm):
            result = await _maybe_refresh_redirect(state="s1", idp_sub="u-42")

        assert isinstance(result, RedirectResponse)
        assert result.headers["location"] == "http://example.com/profile?refreshed=1"

    @pytest.mark.asyncio
    async def test_refresh_with_user_mismatch_redirects_to_error(self, monkeypatch):
        """SECURITY: the IdP returned a different account than the one
        that initiated the refresh — never apply that account's identity
        to the originating session. Surface the mismatch on /profile."""
        monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
        sm, _redis = _mock_session_manager_with_redis(
            {
                "oauth2_state_intent:s1": "refresh",
                "oauth2_state_user:s1": "u-orig",
                "oauth2_return_to:s1": "/profile?refreshed=1",
            }
        )
        with patch.object(auth_module, "_session_manager", sm):
            result = await _maybe_refresh_redirect(state="s1", idp_sub="u-different")

        assert isinstance(result, RedirectResponse)
        assert (
            result.headers["location"]
            == "http://localhost:3000/profile?error=refresh_user_mismatch"
        )
        # return_to must NOT be honoured on the mismatch path: the
        # frontend's ``refreshed=1`` toast would falsely claim success.

    @pytest.mark.asyncio
    async def test_refresh_with_expired_user_record_redirects_to_error(self, monkeypatch):
        """Intent set but user_id key already TTL'd → cannot enforce
        same-user. Refuse and tell the user to retry."""
        monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
        sm, _redis = _mock_session_manager_with_redis(
            {
                "oauth2_state_intent:s1": "refresh",
                # No oauth2_state_user:s1 entry.
            }
        )
        with patch.object(auth_module, "_session_manager", sm):
            result = await _maybe_refresh_redirect(state="s1", idp_sub="u-42")

        assert isinstance(result, RedirectResponse)
        assert (
            result.headers["location"]
            == "http://localhost:3000/profile?error=refresh_state_expired"
        )

    @pytest.mark.asyncio
    async def test_session_manager_missing_returns_none(self):
        """Module-level ``_session_manager`` is None during early app
        boot or in degraded mode. Helper returns None safely; the caller
        will then 500 elsewhere on its own state validation."""
        with patch.object(auth_module, "_session_manager", None):
            result = await _maybe_refresh_redirect(state="s1", idp_sub="u-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_return_to_deleted_on_user_mismatch(self, monkeypatch):
        """Delete-on-read contract: oauth2_return_to:{state} must be
        cleared on the mismatch branch too, not just on happy path —
        otherwise repeated mismatch errors leak stale keys until TTL."""
        monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
        sm, redis = _mock_session_manager_with_redis(
            {
                "oauth2_state_intent:s1": "refresh",
                "oauth2_state_user:s1": "u-orig",
                "oauth2_return_to:s1": "/profile?refreshed=1",
            }
        )
        with patch.object(auth_module, "_session_manager", sm):
            await _maybe_refresh_redirect(state="s1", idp_sub="u-different")
        deleted = {call.args[0] for call in redis.delete.call_args_list}
        assert "oauth2_return_to:s1" in deleted, (
            "return_to key must be cleared even when the user-mismatch "
            "branch fires, to honour the delete-on-read contract"
        )

    @pytest.mark.asyncio
    async def test_return_to_deleted_on_state_expired(self, monkeypatch):
        """Same delete-on-read contract for the expired-state branch."""
        monkeypatch.setenv("FRONTEND_URL", "http://localhost:3000")
        sm, redis = _mock_session_manager_with_redis(
            {
                "oauth2_state_intent:s1": "refresh",
                # No oauth2_state_user — expired branch.
                "oauth2_return_to:s1": "/profile?refreshed=1",
            }
        )
        with patch.object(auth_module, "_session_manager", sm):
            await _maybe_refresh_redirect(state="s1", idp_sub="u-1")
        deleted = {call.args[0] for call in redis.delete.call_args_list}
        assert "oauth2_return_to:s1" in deleted, (
            "return_to key must be cleared even when the state-expired branch fires"
        )
