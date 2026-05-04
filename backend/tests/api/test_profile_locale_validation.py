"""Route-level tests for PUT /api/v1/users/profile locale validation (Issue #542).

Extends the auth/me locale coverage by verifying that the profile update
endpoint rejects invalid locale codes at the schema layer (FastAPI/Pydantic
pattern validation) before they reach the DB.

Direct-handler tests (same style as ``test_auth_me_signin_method.py``) —
no TestClient or real DB required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from api.routes.users import update_user_profile
from models.schemas import UpdateUserProfileRequest


def _user_session(*, user_id: str = "u-1") -> dict:
    """Minimal SessionUser dict."""
    return {"user_id": user_id}


def _db_user(*, locale: str = "en") -> SimpleNamespace:
    """Stand-in for the ``User`` ORM row with mutable locale."""
    return SimpleNamespace(
        id=1,
        user_id="u-1",
        email="u@example.com",
        name="Test",
        picture=None,
        timezone="UTC",
        locale=locale,
        role="user",
        current_workspace_id=None,
        created_at=datetime.now(UTC),
        last_login_at=None,
        auth_method="oauth",
        auth_provider="google",
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


class TestProfileLocaleValidation:
    """PUT /api/v1/users/profile must reject locale outside {en, ja}."""

    @pytest.mark.asyncio
    async def test_valid_en_locale_accepted(self):
        """English locale is valid and persisted."""
        user = _db_user(locale="en")
        db = _mock_db(user)
        data = UpdateUserProfileRequest(locale="en")
        result = await update_user_profile(data=data, user=_user_session(), db=db)
        assert result.locale == "en"

    @pytest.mark.asyncio
    async def test_valid_ja_locale_accepted(self):
        """Japanese locale is valid and persisted."""
        user = _db_user(locale="en")
        db = _mock_db(user)
        data = UpdateUserProfileRequest(locale="ja")
        result = await update_user_profile(data=data, user=_user_session(), db=db)
        assert result.locale == "ja"

    def test_invalid_locale_rejected_at_schema(self):
        """Locale 'fr' fails Pydantic pattern validation → ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            UpdateUserProfileRequest(locale="fr")
        assert "locale" in str(exc_info.value)

    def test_empty_locale_rejected_at_schema(self):
        """Empty locale fails Pydantic pattern validation → ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            UpdateUserProfileRequest(locale="")
        assert "locale" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_null_locale_is_noop(self):
        """Null locale is omitted — existing value preserved."""
        user = _db_user(locale="ja")
        db = _mock_db(user)
        data = UpdateUserProfileRequest(locale=None)
        result = await update_user_profile(data=data, user=_user_session(), db=db)
        assert result.locale == "ja"
