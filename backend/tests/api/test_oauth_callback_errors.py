"""Tests for non-cancel failure redirects on the login OAuth callbacks (#1381).

#727 gave the google/github callbacks a friendly redirect for the *cancel*
lane. Every other failure class (missing params, expired/replayed state,
exchange failure, DB trouble) still dead-ended the browser on raw JSON.
#1381 converts those lanes to a 303 back to ``/login?error=<token>`` where
``<token>`` is one of the internal literals ``oauth_failed`` /
``oauth_expired`` — the same well-known-token channel the login page already
uses for ``registration_disabled`` / ``email_in_use``.

These lanes are self-contained (they fire before or instead of the heavy
Authlib / DB success path), so they are exercised by calling the handlers
directly with the module-global managers patched — same style as
``test_oauth_callback_cancel.py``.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from api.routes import auth as auth_module
from api.routes.auth import (
    _oauth_error_redirect,
    github_callback,
    google_callback,
)


def _location(resp: RedirectResponse) -> str:
    return resp.headers["location"]


class TestErrorRedirectHelper:
    """``_oauth_error_redirect`` — internal-token allowlist, fixed origin."""

    def test_redirects_to_login_with_token_and_provider(self):
        resp = _oauth_error_redirect("google", "oauth_failed")
        assert resp.status_code == 303
        loc = _location(resp)
        assert "/login?error=oauth_failed" in loc
        assert "provider=google" in loc

    def test_uses_frontend_origin(self):
        with patch.dict(os.environ, {"FRONTEND_URL": "https://app.example.com"}):
            resp = _oauth_error_redirect("github", "oauth_expired")
        assert _location(resp).startswith("https://app.example.com/login?error=oauth_expired")

    def test_unknown_reason_collapses_to_oauth_failed(self):
        # Defensive: the reason is an internal literal, but an unknown value
        # must not widen the URL vocabulary — collapse to the generic token.
        resp = _oauth_error_redirect("google", "some_new_reason")
        assert "error=oauth_failed" in _location(resp)


class TestMissingParams:
    """No ``error`` and missing/empty ``code``/``state`` → oauth_failed redirect."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"code": None, "state": None},
            {"code": "", "state": "s"},  # empty string must not reach validation
            {"code": "c", "state": ""},
        ],
    )
    async def test_google_missing_params_redirects_failed(self, kwargs):
        resp = await google_callback(None, error=None, error_description=None, **kwargs)
        assert isinstance(resp, RedirectResponse)
        assert resp.status_code == 303
        loc = _location(resp)
        assert "/login?error=oauth_failed" in loc
        assert "provider=google" in loc

    async def test_github_missing_params_redirects_failed(self):
        resp = await github_callback(
            None, code=None, state=None, error=None, error_description=None
        )
        assert resp.status_code == 303
        assert "/login?error=oauth_failed" in _location(resp)
        assert "provider=github" in _location(resp)


class TestEmptyErrorParam:
    """A proxy echoing ``?error=`` (empty) must not take the cancel lane (#1375 lesson)."""

    async def test_google_empty_error_is_not_a_cancel(self):
        # With managers unset the request still fails — but through the
        # failure lane, never the "cancelled" banner.
        with (
            patch.object(auth_module, "_oauth2_manager", None),
            patch.object(auth_module, "_session_manager", None),
        ):
            resp = await google_callback(
                None, code="c", state="s", error="", error_description=None
            )
        loc = _location(resp)
        assert "cancelled=1" not in loc
        assert "error=oauth_failed" in loc


class TestManagersNotInitialized:
    """Broken deploy (managers unset) → redirect, not raw 500 JSON."""

    @pytest.mark.parametrize("callback", [google_callback, github_callback])
    async def test_redirects_failed(self, callback):
        with (
            patch.object(auth_module, "_oauth2_manager", None),
            patch.object(auth_module, "_session_manager", None),
        ):
            resp = await callback(None, code="c", state="s", error=None, error_description=None)
        assert resp.status_code == 303
        assert "error=oauth_failed" in _location(resp)


class TestFrontendUrlNormalization:
    """A trailing-slash FRONTEND_URL must not produce a double-slash redirect."""

    def test_trailing_slash_is_normalized(self):
        with patch.dict(os.environ, {"FRONTEND_URL": "https://app.example.com/"}):
            resp = _oauth_error_redirect("google", "oauth_failed")
        loc = _location(resp)
        assert loc.startswith("https://app.example.com/login?")
        assert "//login" not in loc


class TestExpiredState:
    """Expired TTL or replayed (consumed) state → oauth_expired redirect."""

    async def test_google_expired_state_redirects_expired(self):
        session_mgr = MagicMock()
        session_mgr._redis.get.return_value = None
        fake_logger = MagicMock()
        with (
            patch.object(auth_module, "_session_manager", session_mgr),
            patch.object(auth_module, "_oauth2_manager", MagicMock()),
            patch.object(auth_module, "logger", fake_logger),
        ):
            resp = await google_callback(
                None, code="c", state="raw-state-token", error=None, error_description=None
            )
        assert resp.status_code == 303
        loc = _location(resp)
        assert "error=oauth_expired" in loc
        assert "provider=google" in loc
        # The warning carries an HMAC of the state — never the raw token.
        fake_logger.warning.assert_called_once()
        _, kwargs = fake_logger.warning.call_args
        assert kwargs["state_hash"] is not None
        assert kwargs["state_hash"] != "raw-state-token"
        assert "raw-state-token" not in str(fake_logger.warning.call_args)

    async def test_github_expired_state_redirects_expired(self):
        session_mgr = MagicMock()
        session_mgr._redis.get.return_value = None
        with (
            patch.object(auth_module, "_session_manager", session_mgr),
            patch.object(auth_module, "_oauth2_manager", MagicMock()),
        ):
            resp = await github_callback(
                None, code="c", state="s", error=None, error_description=None
            )
        assert resp.status_code == 303
        assert "error=oauth_expired" in _location(resp)
        assert "provider=github" in _location(resp)


class TestExchangeFailure:
    """Token-exchange / downstream failures → oauth_failed redirect, no reflection."""

    async def test_google_exchange_failure_redirects_failed(self):
        session_mgr = MagicMock()
        session_mgr._redis.get.return_value = "pending"
        oauth_mgr = MagicMock()
        oauth_mgr.exchange_code_web.side_effect = RuntimeError("boom-internal-detail")
        with (
            patch.object(auth_module, "_session_manager", session_mgr),
            patch.object(auth_module, "_oauth2_manager", oauth_mgr),
            patch.dict(os.environ, {"GOOGLE_REDIRECT_URI": "http://localhost:8080/cb"}),
        ):
            resp = await google_callback(
                None, code="c", state="s", error=None, error_description=None
            )
        assert resp.status_code == 303
        loc = _location(resp)
        assert "error=oauth_failed" in loc
        # Internal exception text never reaches the URL.
        assert "boom-internal-detail" not in loc
        # State is still consumed (one-time use) before the exchange.
        session_mgr._redis.delete.assert_any_call("oauth2_state:s")

    async def test_github_exchange_failure_redirects_failed(self):
        session_mgr = MagicMock()
        session_mgr._redis.get.return_value = "pending"
        with (
            patch.object(auth_module, "_session_manager", session_mgr),
            patch.object(auth_module, "_oauth2_manager", MagicMock()),
            patch.object(
                auth_module,
                "_github_exchange_code",
                AsyncMock(side_effect=ValueError("GitHub OAuth error: bad_verification_code")),
            ),
        ):
            resp = await github_callback(
                None, code="c", state="s", error=None, error_description=None
            )
        assert resp.status_code == 303
        loc = _location(resp)
        assert "error=oauth_failed" in loc
        assert "bad_verification_code" not in loc


class TestDbErrorLane:
    """SQLAlchemyError no longer 503-JSONs the browser — redirect + error log."""

    async def test_google_db_error_redirects_failed_and_logs(self):
        session_mgr = MagicMock()
        session_mgr._redis.get.return_value = "pending"
        oauth_mgr = MagicMock()
        oauth_mgr.exchange_code_web.side_effect = SQLAlchemyError("db down")
        fake_logger = MagicMock()
        with (
            patch.object(auth_module, "_session_manager", session_mgr),
            patch.object(auth_module, "_oauth2_manager", oauth_mgr),
            patch.object(auth_module, "logger", fake_logger),
            patch.dict(os.environ, {"GOOGLE_REDIRECT_URI": "http://localhost:8080/cb"}),
        ):
            resp = await google_callback(
                None, code="c", state="s", error=None, error_description=None
            )
        assert resp.status_code == 303
        assert "error=oauth_failed" in _location(resp)
        # The outage stays visible to log-based alerting.
        assert any(
            call.args and call.args[0] == "oauth_callback_db_error"
            for call in fake_logger.error.call_args_list
        )
