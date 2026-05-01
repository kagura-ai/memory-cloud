"""Unit tests for SignupGateService (Issue #358 Phase 1).

Tests the decision logic in ``check_access`` and mutations in the admin CRUD
methods. Internal lookups (``_is_existing_user``, ``_is_first_user``,
``_is_allowlisted``, ``_legacy_check``, ``_load_config``) are patched so these
remain pure unit tests with no DB.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.responses import RedirectResponse

from services.signup_gate_service import SignupGateService
from utils.github_user import GitHubUserNotFound


def _svc() -> SignupGateService:
    """Build a service with a MagicMock DB (no real session)."""
    db = MagicMock()
    return SignupGateService(db)


def _config(*, enabled: bool, mode: str) -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled, mode=mode)


class TestCheckAccess:
    @pytest.mark.asyncio
    async def test_disabled_delegates_to_legacy(self):
        """enabled=false → legacy env-based gate owns the decision (OSS path)."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=False, mode="manual"))
        svc._legacy_check = AsyncMock(return_value=None)  # allowed

        result = await svc.check_access(
            provider="github", oauth_sub="1234", email="a@b.com", username="octocat"
        )

        assert result is None
        svc._legacy_check.assert_awaited_once_with("a@b.com", "1234")

    @pytest.mark.asyncio
    async def test_disabled_delegates_and_blocks(self):
        """enabled=false + legacy says 'blocked' → that block propagates."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=False, mode="manual"))
        legacy_redirect = RedirectResponse("/login?error=x", status_code=303)
        svc._legacy_check = AsyncMock(return_value=legacy_redirect)

        result = await svc.check_access(
            provider="github", oauth_sub="1234", email="a@b.com", username="octocat"
        )

        assert result is legacy_redirect

    @pytest.mark.asyncio
    async def test_enabled_google_passes_through(self):
        """provider=google never invokes the allowlist (Google-side controls signup)."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock()
        svc._is_allowlisted = AsyncMock()

        result = await svc.check_access(
            provider="google", oauth_sub="5678", email="a@b.com", username=None
        )

        assert result is None
        svc._is_existing_user.assert_not_awaited()
        svc._is_allowlisted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_user_always_allowed(self):
        """Existing user → login not signup, skip allowlist check entirely."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock(return_value=True)
        svc._is_allowlisted = AsyncMock()

        result = await svc.check_access(
            provider="github", oauth_sub="1234", email="a@b.com", username="octocat"
        )

        assert result is None
        svc._is_allowlisted.assert_not_awaited()
        # Ensure both email and oauth_sub are forwarded so a user whose email
        # changed at the IdP is still found by user_id.
        svc._is_existing_user.assert_awaited_once_with("a@b.com", "1234")

    @pytest.mark.asyncio
    async def test_existing_user_found_by_user_id_not_email(self):
        """User whose IdP email changed is still recognised as existing (login, not signup)."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        # Simulate: same user_id, different email (e.g. after IdP email rename).
        svc._is_existing_user = AsyncMock(return_value=True)
        svc._is_allowlisted = AsyncMock()

        result = await svc.check_access(
            provider="github",
            oauth_sub="1234",
            email="new@example.com",
            username="octocat",
        )

        assert result is None
        svc._is_allowlisted.assert_not_awaited()
        # Both the new email and user_id must be passed down.
        svc._is_existing_user.assert_awaited_once_with("new@example.com", "1234")

    @pytest.mark.asyncio
    async def test_first_user_bootstrap_allowed(self):
        """No users in DB → allow bootstrap admin regardless of allowlist state."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=True)
        svc._is_allowlisted = AsyncMock()

        result = await svc.check_access(
            provider="github", oauth_sub="1234", email="a@b.com", username="octocat"
        )

        assert result is None
        svc._is_allowlisted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_manual_mode_allows_allowlisted_user(self):
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)
        svc._is_allowlisted = AsyncMock(return_value=True)

        result = await svc.check_access(
            provider="github", oauth_sub="1234", email="a@b.com", username="octocat"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_manual_mode_blocks_non_allowlisted_user(self):
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)
        svc._is_allowlisted = AsyncMock(return_value=False)

        result = await svc.check_access(
            provider="github", oauth_sub="1234", email="a@b.com", username="octocat"
        )

        assert isinstance(result, RedirectResponse)
        assert "/signup-blocked" in result.headers["location"]
        assert result.status_code == 303

    @pytest.mark.asyncio
    async def test_sponsors_mode_raises_not_implemented_in_phase1(self):
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="github_sponsors"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)

        with pytest.raises(NotImplementedError):
            await svc.check_access(
                provider="github",
                oauth_sub="1234",
                email="a@b.com",
                username="octocat",
            )

    @pytest.mark.asyncio
    async def test_both_mode_raises_not_implemented_in_phase1(self):
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="both"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)

        with pytest.raises(NotImplementedError):
            await svc.check_access(
                provider="github",
                oauth_sub="1234",
                email="a@b.com",
                username="octocat",
            )


class TestAddToAllowlist:
    @pytest.mark.asyncio
    async def test_resolves_and_persists_new_entry(self):
        svc = _svc()
        # No existing row for this (user_id, source='manual') pair.
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none = MagicMock(return_value=None)
        svc.db.execute = AsyncMock(return_value=scalar_result)
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock()
        svc.db.refresh = AsyncMock()

        with patch(
            "services.signup_gate_service.resolve_github_user_id",
            new=AsyncMock(return_value=("583231", "octocat")),
        ):
            entry = await svc.add_to_allowlist(github_username="OctoCat", added_by_user_id="admin1")

        assert entry.github_user_id == "583231"
        assert entry.github_username == "octocat"  # canonical form
        assert entry.source == "manual"
        assert entry.state == "active"
        assert entry.added_by_user_id == "admin1"
        svc.db.add.assert_called_once()
        svc.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_duplicate_entry(self):
        svc = _svc()
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none = MagicMock(return_value=MagicMock())
        svc.db.execute = AsyncMock(return_value=scalar_result)
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock()

        with patch(
            "services.signup_gate_service.resolve_github_user_id",
            new=AsyncMock(return_value=("583231", "octocat")),
        ):
            with pytest.raises(ValueError, match="already on the manual allowlist"):
                await svc.add_to_allowlist(github_username="octocat", added_by_user_id="admin1")

        svc.db.add.assert_not_called()
        svc.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_propagates_github_user_not_found(self):
        svc = _svc()
        svc.db.execute = AsyncMock()

        with patch(
            "services.signup_gate_service.resolve_github_user_id",
            new=AsyncMock(side_effect=GitHubUserNotFound("ghost")),
        ):
            with pytest.raises(GitHubUserNotFound):
                await svc.add_to_allowlist(github_username="ghost", added_by_user_id="admin1")

    @pytest.mark.asyncio
    async def test_integrity_error_on_commit_becomes_value_error(self):
        """Concurrent add: SELECT check passes, COMMIT hits the unique index.

        The pre-check SELECT can return 'no existing row' while a parallel
        request is still mid-flight; when our COMMIT then violates the
        uq_allowlist_user_source index, surface the same ValueError the
        pre-check raises so the API returns a consistent 409 instead of
        leaking a 500.
        """
        from sqlalchemy.exc import IntegrityError

        svc = _svc()
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none = MagicMock(return_value=None)
        svc.db.execute = AsyncMock(return_value=scalar_result)
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock(
            side_effect=IntegrityError("uq violation", params=None, orig=Exception())
        )
        svc.db.rollback = AsyncMock()

        with patch(
            "services.signup_gate_service.resolve_github_user_id",
            new=AsyncMock(return_value=("583231", "octocat")),
        ):
            with pytest.raises(ValueError, match="already on the manual allowlist"):
                await svc.add_to_allowlist(github_username="octocat", added_by_user_id="admin1")

        svc.db.rollback.assert_awaited_once()


class TestRemoveFromAllowlist:
    @pytest.mark.asyncio
    async def test_deletes_existing_entry(self):
        svc = _svc()
        entry = MagicMock(github_username="octocat")
        svc.db.get = AsyncMock(return_value=entry)
        svc.db.delete = AsyncMock()
        svc.db.commit = AsyncMock()

        await svc.remove_from_allowlist(uuid4())

        svc.db.delete.assert_awaited_once_with(entry)
        svc.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_when_entry_missing(self):
        svc = _svc()
        svc.db.get = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="not found"):
            await svc.remove_from_allowlist(uuid4())


class TestUpdateConfig:
    @pytest.mark.asyncio
    async def test_updates_enabled_and_mode(self):
        svc = _svc()
        config = SimpleNamespace(enabled=False, mode="manual")
        svc._load_config = AsyncMock(return_value=config)
        svc.db.commit = AsyncMock()
        svc.db.refresh = AsyncMock()

        result = await svc.update_config(enabled=True, mode="manual")

        assert result.enabled is True
        assert result.mode == "manual"
        svc.db.commit.assert_awaited_once()


class TestLoadConfigSelfHeal:
    @pytest.mark.asyncio
    async def test_concurrent_self_heal_race_resolves(self):
        """Two callers both see config is None; one INSERT wins, the other
        catches IntegrityError, rolls back, and re-SELECTs the winning row.
        """
        from sqlalchemy.exc import IntegrityError

        winning_row = SimpleNamespace(id=1, enabled=False, mode="manual")

        # First execute: SELECT finds no row (concurrent caller hasn't committed yet).
        first_select = MagicMock()
        first_select.scalar_one_or_none = MagicMock(return_value=None)
        # Second execute (after IntegrityError): SELECT finds the row the
        # winning caller just committed.
        second_select = MagicMock()
        second_select.scalar_one = MagicMock(return_value=winning_row)

        svc = _svc()
        svc.db.execute = AsyncMock(side_effect=[first_select, second_select])
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock(
            side_effect=IntegrityError("uq violation", params=None, orig=Exception())
        )
        svc.db.rollback = AsyncMock()

        config = await svc._load_config()

        assert config is winning_row
        svc.db.rollback.assert_awaited_once()
        # execute called twice: initial SELECT, then re-SELECT after rollback
        assert svc.db.execute.await_count == 2


class TestIsAllowlistedSourceFiltering:
    """Verify mode→source filter mapping so manual/sponsors/both stay distinct."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["manual", "github_sponsors", "both"])
    async def test_mode_controls_source_filter(self, mode):
        """mode='both' MUST omit the source filter so either row type allows signup."""
        svc = _svc()
        captured = {}

        async def fake_execute(stmt):
            # Introspect the compiled WHERE clause to count filters.
            captured["sql"] = str(stmt.compile())
            result = MagicMock()
            result.first = MagicMock(return_value=None)
            return result

        svc.db.execute = fake_execute

        allowed = await svc._is_allowlisted("1234", mode)
        assert allowed is False  # no matching row → not allowlisted

        # SQL contains "source = " clause iff mode != 'both'
        has_source_filter = "signup_allowlist.source =" in captured["sql"]
        if mode == "both":
            assert not has_source_filter, f"mode=both must not filter by source: {captured['sql']}"
        else:
            assert has_source_filter, f"mode={mode} must filter by source: {captured['sql']}"


class TestIsExistingUser:
    """Verify _is_existing_user uses an OR condition on email + user_id and
    tolerates the OR predicate matching two different rows (which legitimately
    happens when GitHub primary email drifts onto an existing other-provider
    user's email — see _is_existing_user docstring)."""

    @pytest.mark.asyncio
    async def test_query_includes_user_id_or_email(self):
        """Generated SQL must include both email and user_id so a user whose
        IdP email changed is still found by their stable OAuth sub. The query
        also LIMITs to 1 row so the OR predicate hitting two different
        legitimate rows doesn't raise MultipleResultsFound."""
        svc = _svc()
        captured = {}

        async def fake_execute(stmt):
            captured["sql"] = str(stmt.compile())
            result = MagicMock()
            result.first = MagicMock(return_value=None)
            return result

        svc.db.execute = fake_execute

        found = await svc._is_existing_user("old@example.com", "gh-sub-42")

        assert found is False
        sql = captured["sql"]
        assert "users.email" in sql
        assert "users.user_id" in sql
        # OR semantics must be present (SQLAlchemy renders "OR" in uppercase)
        assert " OR " in sql.upper()
        # LIMIT 1 must be present so the multi-row case doesn't blow up
        assert "LIMIT" in sql.upper()

    @pytest.mark.asyncio
    async def test_returns_true_when_row_exists(self):
        svc = _svc()
        result_mock = MagicMock()
        result_mock.first = MagicMock(return_value=MagicMock())
        svc.db.execute = AsyncMock(return_value=result_mock)

        assert await svc._is_existing_user("a@b.com", "sub-1") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_row(self):
        svc = _svc()
        result_mock = MagicMock()
        result_mock.first = MagicMock(return_value=None)
        svc.db.execute = AsyncMock(return_value=result_mock)

        assert await svc._is_existing_user("unknown@b.com", "sub-99") is False

    @pytest.mark.asyncio
    async def test_multi_row_match_does_not_raise_and_returns_true(self):
        """Regression: the OR predicate can match two distinct rows when a
        user has rows under two providers and one IdP's primary email
        drifts onto the other row's email. ``.first()`` + ``LIMIT 1`` keep
        this case sane — the cross-row email collision is later detected
        and surfaced as ConflictError → /login?error=email_in_use by
        RoleManager._sync_existing_user when the email UPDATE attempts the
        move. We just need _is_existing_user to NOT raise here.
        """
        svc = _svc()
        result_mock = MagicMock()
        # ``.first()`` returns the first row even when the underlying query
        # would have hit multiple — that's the contract we're pinning.
        result_mock.first = MagicMock(return_value=(1,))
        svc.db.execute = AsyncMock(return_value=result_mock)

        assert await svc._is_existing_user("collision@b.com", "sub-2") is True


class TestLegacyCheckPassesUserIdThrough:
    """_legacy_check must forward user_id to _check_registration_allowed."""

    @pytest.mark.asyncio
    async def test_disabled_gate_passes_user_id_to_legacy(self):
        """When gate is disabled the legacy path is called with both email and user_id."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=False, mode="manual"))
        svc._legacy_check = AsyncMock(return_value=None)

        await svc.check_access(
            provider="github",
            oauth_sub="gh-sub-99",
            email="user@example.com",
            username="octocat",
        )

        svc._legacy_check.assert_awaited_once_with("user@example.com", "gh-sub-99")
