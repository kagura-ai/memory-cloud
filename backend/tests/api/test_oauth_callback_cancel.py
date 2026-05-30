"""Tests for the cancelled-IdP short-circuit in the OAuth callbacks (Issue #727).

When a user cancels at the IdP, the callback arrives as
``?error=access_denied&state=...`` with **no** ``code``. Before #727 both
``google_callback`` and ``github_callback`` declared ``code`` as a required
query param, so FastAPI rejected the request with a raw pydantic 422 JSON that
the end user saw in production.

The fix makes ``code``/``state`` optional, adds an ``error`` param, and
short-circuits to a friendly ``/login?cancelled=1`` redirect **before** any
CSRF / OAuth-manager / DB / Redis work. That makes the cancel and malformed
paths self-contained — they can be exercised by calling the handlers (and the
shared ``_oauth_cancel_redirect`` helper) directly, without the heavy Authlib /
Redis / DB fixture stack the success path needs.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from api.routes import auth as auth_module
from api.routes.auth import (
    _oauth_cancel_redirect,
    github_callback,
    google_callback,
)


def _location(resp: RedirectResponse) -> str:
    return resp.headers["location"]


class TestCallbackCancelShortCircuit:
    """The ``error`` param triggers a 303 redirect before other validation."""

    async def test_google_cancel_redirects_to_login_cancelled(self):
        resp = await google_callback(
            None,  # request — unused on the cancel path
            code=None,
            state="state-token",
            error="access_denied",
            error_description="The user denied the request",
        )
        assert isinstance(resp, RedirectResponse)
        assert resp.status_code == 303
        loc = _location(resp)
        assert "/login?cancelled=1" in loc
        assert "provider=google" in loc
        assert "reason=access_denied" in loc
        # error_description must NOT be reflected into the redirect.
        assert "denied%20the%20request" not in loc
        assert "denied the request" not in loc

    async def test_github_cancel_redirects_to_login_cancelled(self):
        resp = await github_callback(
            None,
            code=None,
            state="state-token",
            error="access_denied",
            error_description=None,
        )
        assert isinstance(resp, RedirectResponse)
        assert resp.status_code == 303
        assert "provider=github" in _location(resp)

    async def test_cancel_redirect_uses_frontend_origin(self):
        """The redirect base is the fixed FRONTEND_URL origin (not the API
        origin, not a relative path) — works cross-origin in dev and is not an
        open redirect."""
        with patch.dict(os.environ, {"FRONTEND_URL": "https://app.example.com"}):
            resp = await google_callback(
                None,
                code=None,
                state="s",
                error="access_denied",
                error_description=None,
            )
        assert _location(resp).startswith("https://app.example.com/login?cancelled=1")


class TestCallbackMalformed:
    """No ``error`` and no ``code``/``state`` → clean 400, not a pydantic 422."""

    async def test_google_malformed_returns_400(self):
        with pytest.raises(HTTPException) as exc:
            await google_callback(None, code=None, state=None, error=None, error_description=None)
        assert exc.value.status_code == 400

    async def test_github_malformed_returns_400(self):
        with pytest.raises(HTTPException) as exc:
            await github_callback(None, code=None, state=None, error=None, error_description=None)
        assert exc.value.status_code == 400


class TestCancelRedirectHelper:
    """``_oauth_cancel_redirect`` sanitization + PII-free audit."""

    def test_reason_sanitized_to_safe_charset(self):
        # Injection attempt: query-string metachars + tag chars must be stripped.
        resp = _oauth_cancel_redirect("google", "access_denied&foo=bar#frag<script>", "s")
        loc = _location(resp)
        assert "reason=access_deniedfoobarfragscript" in loc
        # No raw metachar leaks past the single intended separators.
        assert "#" not in loc
        assert "<" not in loc and ">" not in loc

    def test_empty_or_unsafe_error_becomes_unknown(self):
        assert "reason=unknown" in _location(_oauth_cancel_redirect("google", "", "s"))
        assert "reason=unknown" in _location(_oauth_cancel_redirect("google", "<<<>>>", "s"))
        assert "reason=unknown" in _location(_oauth_cancel_redirect("google", None, "s"))

    def test_reason_truncated_to_64_chars(self):
        long_error = "a" * 200
        loc = _location(_oauth_cancel_redirect("google", long_error, "s"))
        # reason=<64 a's>
        assert "reason=" + "a" * 64 in loc
        assert "a" * 65 not in loc

    def test_audit_log_hashes_state_no_pii(self):
        fake_logger = MagicMock()
        with patch.object(auth_module, "logger", fake_logger):
            _oauth_cancel_redirect("github", "access_denied", "raw-state-token")
        fake_logger.info.assert_called_once()
        event, kwargs = fake_logger.info.call_args
        assert event[0] == "oauth_login_cancelled"
        assert kwargs["provider"] == "github"
        assert kwargs["reason"] == "access_denied"
        # State is HMAC-hashed, never logged raw.
        assert kwargs["state_hash"] is not None
        assert kwargs["state_hash"] != "raw-state-token"
        assert "raw-state-token" not in str(kwargs)

    def test_audit_log_null_state_hash_when_state_absent(self):
        fake_logger = MagicMock()
        with patch.object(auth_module, "logger", fake_logger):
            _oauth_cancel_redirect("google", "access_denied", None)
        _, kwargs = fake_logger.info.call_args
        assert kwargs["state_hash"] is None
