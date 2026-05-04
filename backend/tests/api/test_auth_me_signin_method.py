"""Route-level tests for /api/v1/auth/me sign-in-method exposure (Issue #514).

The handler builds a hand-crafted dict (not a Pydantic schema) for
backwards compatibility with the frontend ``{user: {...}}`` envelope.
This test pins the new fields ``auth_method`` and ``auth_provider`` to
the response shape so a future regression that drops or renames either
column-derived field gets caught at unit-test time, not at frontend
runtime where it would silently degrade the profile UI to the "Other"
fallback.

Service-layer behaviour around how ``auth_method`` and ``auth_provider``
are set at signup / OAuth callback is covered separately in
``tests/auth/`` and ``tests/api/test_auth_providers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.auth import get_current_user_info


def _session(*, user_id: str = "u-1", email: str = "u@example.com") -> dict:
    """Minimal SessionUser dict — only the keys the handler reads."""
    return {
        "user_id": user_id,
        "email": email,
        "name": "Test User",
        "picture": None,
        "role": "user",
        "current_workspace_id": None,
    }


def _db_user(*, auth_method: str, auth_provider: str | None) -> SimpleNamespace:
    """Stand-in for the ``User`` ORM row.

    The handler reads ``timezone``, ``auth_method``, ``auth_provider``
    from the row; the rest of the response is sourced from the session.
    """
    return SimpleNamespace(
        timezone="UTC",
        locale="en",
        auth_method=auth_method,
        auth_provider=auth_provider,
    )


def _mock_db(db_user: SimpleNamespace | None) -> AsyncMock:
    """Build an ``AsyncSession`` mock whose ``execute()`` returns the
    given row (or None for the "user missing" branch).
    """
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = db_user
    db.execute.return_value = result
    return db


class TestAuthMeSignInMethod:
    """GET /api/v1/auth/me must expose ``auth_method`` and ``auth_provider``
    so the frontend can render the profile sign-in-method line (Issue #514)."""

    @pytest.mark.asyncio
    async def test_oauth_google_user_exposes_provider(self):
        """Google OAuth user → auth_method='oauth', auth_provider='google'."""
        db = _mock_db(_db_user(auth_method="oauth", auth_provider="google"))
        result = await get_current_user_info(user=_session(), db=db)

        assert result["user"]["auth_method"] == "oauth"
        assert result["user"]["auth_provider"] == "google"
        assert result["user"]["locale"] == "en"

    @pytest.mark.asyncio
    async def test_oauth_github_user_exposes_provider(self):
        """GitHub OAuth user → auth_method='oauth', auth_provider='github'."""
        db = _mock_db(_db_user(auth_method="oauth", auth_provider="github"))
        result = await get_current_user_info(user=_session(), db=db)

        assert result["user"]["auth_method"] == "oauth"
        assert result["user"]["auth_provider"] == "github"
        assert result["user"]["locale"] == "en"

    @pytest.mark.asyncio
    async def test_password_user_provider_is_null(self):
        """Password user → auth_method='password', auth_provider=None.

        The frontend falls back to the "Email + Password" label whenever
        auth_method='password', regardless of auth_provider — but we still
        pin auth_provider to ``None`` here to catch a regression that
        accidentally fills it in for password users.
        """
        db = _mock_db(_db_user(auth_method="password", auth_provider=None))
        result = await get_current_user_info(user=_session(), db=db)

        assert result["user"]["auth_method"] == "password"
        assert result["user"]["auth_provider"] is None
        assert result["user"]["locale"] == "en"

    @pytest.mark.asyncio
    async def test_legacy_oauth_user_with_null_provider(self):
        """Pre-#361 OAuth user → auth_method='oauth', auth_provider=None.

        Users created before Issue #361 introduced ``auth_provider`` may
        still have ``auth_provider=NULL`` even though they signed in via
        OAuth. The handler must surface this honestly so the frontend
        can render the "Other" fallback rather than guessing a provider.
        """
        db = _mock_db(_db_user(auth_method="oauth", auth_provider=None))
        result = await get_current_user_info(user=_session(), db=db)

        assert result["user"]["auth_method"] == "oauth"
        assert result["user"]["auth_provider"] is None
        assert result["user"]["locale"] == "en"

    @pytest.mark.asyncio
    async def test_db_user_missing_falls_back_to_oauth_default(self):
        """If the DB row is missing, the handler defaults auth_method to
        ``'oauth'`` and auth_provider to ``None``. This matches the
        legacy timezone fallback behaviour (UTC) — the response stays
        well-formed rather than 5xx-ing on a phantom session.
        """
        db = _mock_db(None)
        result = await get_current_user_info(user=_session(), db=db)

        assert result["user"]["auth_method"] == "oauth"
        assert result["user"]["auth_provider"] is None
        assert result["user"]["locale"] == "en"

    @pytest.mark.asyncio
    async def test_oauth_user_with_ja_locale(self):
        """Japanese locale user → locale='ja' exposed in response (Issue #542)."""
        db = _mock_db(
            SimpleNamespace(
                timezone="Asia/Tokyo",
                locale="ja",
                auth_method="oauth",
                auth_provider="google",
            )
        )
        result = await get_current_user_info(user=_session(), db=db)

        assert result["user"]["locale"] == "ja"
