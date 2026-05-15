"""Unit tests for utils.google_user (Issue #655).

Mirrors the structure of ``test_github_user.py``. ``resolve_google_sub_by_email``
hits only the local ``users`` table (unlike GitHub's helper, which calls the
public GitHub API), so the tests use an in-memory result mock rather than an
HTTP-layer mock.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.google_user import GoogleUserNotFound, resolve_google_sub_by_email


def _db_with_first(row):
    """Helper: build an AsyncSession mock that returns ``row`` from .first()."""
    result = MagicMock()
    result.first = MagicMock(return_value=row)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestResolveGoogleSubByEmail:
    @pytest.mark.asyncio
    async def test_returns_sub_for_existing_google_user(self):
        db = _db_with_first(("108276939729829363", "alice@example.com"))

        sub, email = await resolve_google_sub_by_email("alice@example.com", db)

        assert sub == "108276939729829363"
        assert email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_case_insensitive_email_match(self):
        """Google's userinfo sometimes capitalizes locally; the lookup must
        not be defeated by a single character of case drift."""
        db = _db_with_first(("108276939729829363", "Alice@example.com"))

        sub, email = await resolve_google_sub_by_email("alice@example.com", db)

        assert sub == "108276939729829363"
        # The canonical email from the users row (preserves stored case).
        assert email == "Alice@example.com"

    @pytest.mark.asyncio
    async def test_raises_when_no_google_user_with_this_email(self):
        db = _db_with_first(None)

        with pytest.raises(GoogleUserNotFound) as exc_info:
            await resolve_google_sub_by_email("ghost@example.com", db)

        # The exception message carries the bootstrap-UX hint admins need
        # ("the user must OAuth once before they can be allowlisted").
        assert "OAuth" in str(exc_info.value)
        assert "ghost@example.com" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_filters_to_google_auth_provider(self):
        """The query MUST scope to auth_provider='google' so a GitHub user
        with the same email doesn't accidentally satisfy the lookup."""
        captured = {}

        async def fake_execute(stmt):
            captured["sql"] = str(stmt.compile())
            result = MagicMock()
            result.first = MagicMock(return_value=None)
            return result

        db = MagicMock()
        db.execute = fake_execute

        with pytest.raises(GoogleUserNotFound):
            await resolve_google_sub_by_email("a@b.com", db)

        # WHERE clause includes the provider scope.
        sql = captured["sql"]
        assert "auth_provider" in sql
        # Case-insensitive email match. SQLAlchemy compiles ``.ilike(...)``
        # to either ``ILIKE`` (PostgreSQL dialect) or ``LOWER(...) LIKE
        # LOWER(...)`` (default dialect — what the in-memory mock sees).
        # Both produce the same runtime semantics, so accept either shape.
        upper = sql.upper()
        assert "ILIKE" in upper or "LOWER" in upper
