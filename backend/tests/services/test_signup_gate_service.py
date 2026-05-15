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

from services.signup_gate_service import (
    SignupGateService,
    _legacy_user_id_for_non_github,
)
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
    async def test_enabled_google_now_gated(self):
        """#655: Google no longer passes through; the allowlist is consulted.

        Before #655 the gate trusted Google's Consent Screen test-user list
        to filter signups. That assumption only holds for sensitive scopes —
        for openid/email/profile the test-user list is advisory. The gate
        now applies uniformly to both providers; Google goes through the
        same existing-user / first-user / allowlist branches.
        """
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)
        svc._is_allowlisted = AsyncMock(return_value=True)

        result = await svc.check_access(
            provider="google",
            oauth_sub="108276939729829363",
            email="a@b.com",
            username="a@b.com",
        )

        assert result is None
        # Allowlist IS consulted for Google now (with provider="google"
        # and subject_id=oauth_sub as the match key).
        svc._is_allowlisted.assert_awaited_once_with("google", "108276939729829363", "manual")

    @pytest.mark.asyncio
    async def test_enabled_google_blocked_when_not_allowlisted(self):
        """#655: a Google user outside the allowlist is blocked and logged."""
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)
        svc._is_allowlisted = AsyncMock(return_value=False)
        # Phase 2 (#655 follow-up): check_access now also consults
        # _promote_pending_google_entry before falling through to block.
        # Stub it to "not promoted" so this test still covers the block
        # path; the promotion path has its own coverage below.
        svc._promote_pending_google_entry = AsyncMock(return_value=False)
        # Stub the audit-write so the test doesn't try to talk to a real
        # session — _record_blocked_signup is exercised on its own below.
        svc._record_blocked_signup = AsyncMock()

        result = await svc.check_access(
            provider="google",
            oauth_sub="108276939729829363",
            email="stranger@example.com",
            username="stranger@example.com",
            ip_address="203.0.113.7",
            user_agent="Mozilla/5.0",
        )

        assert isinstance(result, RedirectResponse)
        assert "/signup-blocked" in result.headers["location"]
        # Reason hints surfaced via query params (provider + first 8 chars
        # of the sub) so the frontend can render an admin-contact prompt.
        assert "provider=google" in result.headers["location"]
        assert "sub=10827693" in result.headers["location"]
        svc._record_blocked_signup.assert_awaited_once()
        kwargs = svc._record_blocked_signup.await_args.kwargs
        assert kwargs["provider"] == "google"
        assert kwargs["oauth_sub"] == "108276939729829363"
        assert kwargs["email"] == "stranger@example.com"
        assert kwargs["ip_address"] == "203.0.113.7"
        assert kwargs["user_agent"] == "Mozilla/5.0"

    @pytest.mark.asyncio
    async def test_promote_pending_google_entry_updates_row_and_returns_true(self):
        """Direct unit cover for ``_promote_pending_google_entry`` — the
        method that the broader ``test_google_pending_entry_promoted_on_first_oauth``
        only exercises through a mock. This test pins the actual
        query / mutation / commit path so a refactor that breaks the
        sentinel lookup or the legacy-column rewrite surfaces here
        before reaching production.
        """
        svc = _svc()
        # Mock a SignupAllowlistEntry instance the query returns.
        pending = MagicMock()
        pending.id = uuid4()
        pending.subject_id = "pending:newcomer@example.com"
        pending.github_user_id = "google:pending:newcomer@example.com"
        pending.provider = "google"
        pending.state = "active"
        pending.source = "manual"

        # First db.execute call (the SELECT inside the method) returns a
        # result whose scalar_one_or_none yields the pending row.
        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=pending)
        svc.db.execute = AsyncMock(return_value=execute_result)
        svc.db.commit = AsyncMock()
        svc.db.rollback = AsyncMock()

        result = await svc._promote_pending_google_entry(
            email="Newcomer@Example.com",  # mixed case — method lower-cases
            oauth_sub="108276939729829363",
        )

        assert result is True
        # subject_id rewritten to the real sub.
        assert pending.subject_id == "108276939729829363"
        # Legacy column also updated, with provider prefix.
        assert pending.github_user_id == "google:108276939729829363"
        svc.db.commit.assert_awaited_once()
        svc.db.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_promote_pending_google_entry_missing_returns_false(self):
        """No pending row → return False without committing.

        Ensures the gate falls through to the regular block path instead
        of silently committing on every Google sign-in attempt.
        """
        svc = _svc()
        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=None)
        svc.db.execute = AsyncMock(return_value=execute_result)
        svc.db.commit = AsyncMock()

        result = await svc._promote_pending_google_entry(
            email="absent@example.com", oauth_sub="999999999999999"
        )

        assert result is False
        svc.db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_promote_pending_google_entry_integrity_error_no_race_winner_returns_false(self):
        """IntegrityError + post-rollback re-check finds no
        ``(google, oauth_sub, active, manual)`` row → method returns
        False so the caller falls through to the regular block path.

        Covers the stale/inconsistent case: a duplicate-key error came
        from a row outside the active+manual scope (e.g. a soft-deleted
        or sponsor-source row), so the user is NOT effectively
        allowlisted under the manual gate.
        """
        from sqlalchemy.exc import IntegrityError

        svc = _svc()
        pending = MagicMock()
        pending.id = uuid4()
        pending.subject_id = "pending:racy@example.com"

        # First execute() = initial SELECT returns pending. Second
        # execute() = post-rollback re-check returns no row.
        initial_result = MagicMock()
        initial_result.scalar_one_or_none = MagicMock(return_value=pending)
        recheck_result = MagicMock()
        recheck_result.first = MagicMock(return_value=None)
        svc.db.execute = AsyncMock(side_effect=[initial_result, recheck_result])
        svc.db.commit = AsyncMock(side_effect=IntegrityError("INSERT", {}, BaseException("dup")))
        svc.db.rollback = AsyncMock()

        result = await svc._promote_pending_google_entry(
            email="racy@example.com", oauth_sub="2222222222"
        )

        assert result is False
        svc.db.rollback.assert_awaited_once()
        # Two execute() calls = initial SELECT + post-rollback re-check.
        assert svc.db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_promote_pending_google_entry_integrity_error_with_race_winner_returns_true(self):
        """IntegrityError caused by a concurrent admin add (or another
        promotion) that just inserted ``(google, oauth_sub, manual)`` —
        re-check finds the winning row → method returns True so the
        gate treats the user as allowlisted rather than blocking them
        despite the row actually being present (PR #673 Copilot
        review #3 finding E).
        """
        from sqlalchemy.exc import IntegrityError

        svc = _svc()
        pending = MagicMock()
        pending.id = uuid4()
        pending.subject_id = "pending:racy@example.com"

        # First execute() = initial SELECT returns pending. Second
        # execute() = post-rollback re-check finds the race-winning row.
        initial_result = MagicMock()
        initial_result.scalar_one_or_none = MagicMock(return_value=pending)
        recheck_result = MagicMock()
        # ``first()`` returning anything non-None signals "row present".
        recheck_result.first = MagicMock(return_value=(uuid4(),))
        svc.db.execute = AsyncMock(side_effect=[initial_result, recheck_result])
        svc.db.commit = AsyncMock(side_effect=IntegrityError("INSERT", {}, BaseException("dup")))
        svc.db.rollback = AsyncMock()

        result = await svc._promote_pending_google_entry(
            email="racy@example.com", oauth_sub="2222222222"
        )

        assert result is True
        svc.db.rollback.assert_awaited_once()
        assert svc.db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_promote_pending_google_entry_rollback_does_not_touch_orm_instance(self):
        """The rollback / success-log paths must reference a pre-commit
        ID snapshot, not ``pending.id`` on an instance that may have
        been expired by ``db.rollback()`` (which in async sessions
        triggers an implicit lazy load → ``MissingGreenlet``).

        Simulate the failure mode by making ``pending.id`` raise on
        access after rollback, and verify the method still completes
        without hitting that attribute (PR #673 Copilot review #3
        finding D).
        """
        from sqlalchemy.exc import IntegrityError

        svc = _svc()

        # Use a class with a property that raises after a flag is set,
        # so the snapshot at the top of the method captures a usable
        # ID but any post-commit/post-rollback access blows up.
        class _ExpiringInstance:
            def __init__(self):
                self._id = uuid4()
                self._expired = False
                self.subject_id = "pending:racy@example.com"
                self.github_user_id = "google:pending:racy@example.com"

            @property
            def id(self):
                if self._expired:
                    raise RuntimeError("MissingGreenlet-like lazy load on expired instance")
                return self._id

        pending = _ExpiringInstance()
        initial_result = MagicMock()
        initial_result.scalar_one_or_none = MagicMock(return_value=pending)
        recheck_result = MagicMock()
        recheck_result.first = MagicMock(return_value=None)
        svc.db.execute = AsyncMock(side_effect=[initial_result, recheck_result])

        async def _rollback_then_expire():
            pending._expired = True

        svc.db.commit = AsyncMock(side_effect=IntegrityError("INSERT", {}, BaseException("dup")))
        svc.db.rollback = AsyncMock(side_effect=_rollback_then_expire)

        # Must NOT raise — the method captured pending_id before commit.
        result = await svc._promote_pending_google_entry(
            email="racy@example.com", oauth_sub="2222222222"
        )

        assert result is False
        svc.db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_google_pending_entry_promoted_on_first_oauth(self):
        """Phase 2 (#655 follow-up): a pending pre-OAuth row is promoted
        to the real OIDC sub on first sign-in, and the gate returns None
        (signup allowed) without writing a blocked-signup audit row.
        """
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)
        # Regular allowlist check misses (no real-sub row yet).
        svc._is_allowlisted = AsyncMock(return_value=False)
        # Promotion succeeds for this email.
        svc._promote_pending_google_entry = AsyncMock(return_value=True)
        svc._record_blocked_signup = AsyncMock()

        result = await svc.check_access(
            provider="google",
            oauth_sub="108276939729829363",
            email="newcomer@example.com",
            username="newcomer@example.com",
        )

        assert result is None
        svc._promote_pending_google_entry.assert_awaited_once_with(
            email="newcomer@example.com",
            oauth_sub="108276939729829363",
        )
        # Promotion path must not write a blocked-signup audit row — the
        # signup is actually being ALLOWED via the pending → real-sub flip.
        svc._record_blocked_signup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_github_does_not_attempt_pending_promotion(self):
        """Phase 2 (#655 follow-up): the pending-promotion path is
        Google-only. GitHub usernames are resolvable to a numeric ID via
        the GitHub API at add-time, so a "pending sub" state cannot
        exist; the check_access path must NOT invoke the promotion
        helper when provider=github (it would otherwise scan for sentinel
        rows that can't be created via the github path).
        """
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="manual"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)
        svc._is_allowlisted = AsyncMock(return_value=False)
        # If this were called, the assertion below would fail.
        svc._promote_pending_google_entry = AsyncMock(return_value=False)
        svc._record_blocked_signup = AsyncMock()

        result = await svc.check_access(
            provider="github",
            oauth_sub="1234",
            email="stranger@example.com",
            username="stranger",
        )

        assert isinstance(result, RedirectResponse)
        svc._promote_pending_google_entry.assert_not_awaited()

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
        # #655: blocked-signup also writes an audit_logs row; stub the
        # helper so the test stays pure (the audit-write itself is
        # exercised in TestRecordBlockedSignup).
        svc._record_blocked_signup = AsyncMock()

        result = await svc.check_access(
            provider="github", oauth_sub="1234", email="a@b.com", username="octocat"
        )

        assert isinstance(result, RedirectResponse)
        assert "/signup-blocked" in result.headers["location"]
        assert "provider=github" in result.headers["location"]
        assert "sub=1234" in result.headers["location"]
        assert result.status_code == 303
        svc._record_blocked_signup.assert_awaited_once()

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
    async def test_google_sponsors_mode_falls_back_to_manual(self):
        """#655: provider=google + sponsors mode → manual semantics.

        Sponsorship is GitHub-specific (no equivalent Google concept), so
        when an admin has the gate set to ``github_sponsors`` or ``both``
        the Google path falls back to the manual allowlist rather than
        raising NotImplementedError. Avoids surfacing a 500 for Google
        users when the GitHub Sponsors integration is in flight.
        """
        svc = _svc()
        svc._load_config = AsyncMock(return_value=_config(enabled=True, mode="github_sponsors"))
        svc._is_existing_user = AsyncMock(return_value=False)
        svc._is_first_user = AsyncMock(return_value=False)
        svc._is_allowlisted = AsyncMock(return_value=True)

        result = await svc.check_access(
            provider="google",
            oauth_sub="9999",
            email="a@b.com",
            username="a@b.com",
        )

        assert result is None
        # The fallback evaluates ``manual`` semantics, NOT github_sponsors —
        # _is_allowlisted is called with mode='manual'.
        svc._is_allowlisted.assert_awaited_once_with("google", "9999", "manual")

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

        # #655: _is_allowlisted now takes (provider, subject_id, mode).
        allowed = await svc._is_allowlisted("github", "1234", mode)
        assert allowed is False  # no matching row → not allowlisted

        # SQL contains "source = " clause iff mode != 'both'
        has_source_filter = "signup_allowlist.source =" in captured["sql"]
        if mode == "both":
            assert not has_source_filter, f"mode=both must not filter by source: {captured['sql']}"
        else:
            assert has_source_filter, f"mode={mode} must filter by source: {captured['sql']}"
        # All modes must include the provider + subject_id filters.
        assert "signup_allowlist.provider" in captured["sql"]
        assert "signup_allowlist.subject_id" in captured["sql"]


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


class TestAddToAllowlistEntry:
    """#655: provider-aware add. Covers both the Google path (no GitHub API
    call) and the GitHub legacy-column write side-effect."""

    @pytest.mark.asyncio
    async def test_adds_google_entry_with_sentinel_legacy_columns(self):
        """Google entries must populate the deprecated github_user_id / github_username
        columns with sentinel values since both stay NOT NULL during the migration."""
        svc = _svc()
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none = MagicMock(return_value=None)
        svc.db.execute = AsyncMock(return_value=scalar_result)
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock()
        svc.db.refresh = AsyncMock()

        entry = await svc.add_to_allowlist_entry(
            provider="google",
            subject_id="108276939729829363",
            subject_label="a@b.com",
            added_by_user_id="admin1",
        )

        assert entry.provider == "google"
        assert entry.subject_id == "108276939729829363"
        assert entry.subject_label == "a@b.com"
        # Sentinel writes to the deprecated columns (the migration window
        # constraint requires NOT NULL on these until a future drop).
        assert entry.github_user_id == "google:108276939729829363"
        assert entry.github_username == "a@b.com"
        assert entry.source == "manual"
        assert entry.state == "active"
        svc.db.add.assert_called_once()
        svc.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_duplicate_google_entry(self):
        svc = _svc()
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none = MagicMock(return_value=MagicMock())
        svc.db.execute = AsyncMock(return_value=scalar_result)
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock()

        with pytest.raises(ValueError, match="already on the manual allowlist"):
            await svc.add_to_allowlist_entry(
                provider="google",
                subject_id="9999",
                subject_label="dup@b.com",
                added_by_user_id="admin1",
            )

        svc.db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_path_requires_legacy_columns(self):
        """provider='github' callers MUST supply the legacy github_user_id /
        github_username — the entry-point is the migration-window contract."""
        svc = _svc()
        svc.db.execute = AsyncMock()

        with pytest.raises(ValueError, match="legacy columns are still NOT NULL"):
            await svc.add_to_allowlist_entry(
                provider="github",
                subject_id="583231",
                subject_label="octocat",
                added_by_user_id="admin1",
            )

    @pytest.mark.asyncio
    async def test_long_pending_sentinel_uses_hashed_legacy_value(self):
        """PR #673 Copilot review #4 finding F: a long pending sentinel
        for the Phase 2 path
        (``subject_id='pending:<email>'`` with an email near the 247-char
        cap) would overflow the legacy ``github_user_id String(64)``.
        Hash-based fallback keeps the value ≤64 chars AND
        collision-resistant under the downgrade migration's restored
        unique on ``(github_user_id, source)``.
        """
        svc = _svc()
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none = MagicMock(return_value=None)
        svc.db.execute = AsyncMock(return_value=scalar_result)
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock()
        svc.db.refresh = AsyncMock()

        # Email at the 247-char cap → sentinel is "pending:" + 247 = 255.
        long_email = ("a" * 235) + "@example.com"
        assert len(long_email) == 247
        long_sentinel = f"pending:{long_email}"

        entry = await svc.add_to_allowlist_entry(
            provider="google",
            subject_id=long_sentinel,
            subject_label=long_email,
            added_by_user_id="admin1",
        )

        assert entry.subject_id == long_sentinel  # full sentinel preserved
        # Legacy column fits the 64-char limit AND keeps the provider
        # prefix readable so spot-queries during the migration window
        # still identify the row's origin.
        assert len(entry.github_user_id) <= 64
        assert entry.github_user_id.startswith("google:")
        # Raw truncation would have produced this colliding value:
        raw_truncated = f"google:{long_sentinel}"[:64]
        assert entry.github_user_id != raw_truncated


class TestLegacyUserIdForNonGithub:
    """PR #673 Copilot review #4 finding F: legacy ``github_user_id`` write
    must be downgrade-safe. The helper switches from readable
    ``<provider>:<subject_id>`` to a sha256-based sentinel when the
    readable form would overflow ``String(64)``, so two distinct
    subject_ids never collide on the legacy column (which the e14
    downgrade migration re-uniques on)."""

    def test_short_subject_returns_readable_form(self):
        # Real OIDC sub (~21 chars) + "google:" = 28 chars — fits 64.
        result = _legacy_user_id_for_non_github("google", "108276939729829363")
        assert result == "google:108276939729829363"

    def test_long_subject_returns_hashed_form_fitting_column(self):
        long_subject = "pending:" + ("a" * 235) + "@example.com"
        result = _legacy_user_id_for_non_github("google", long_subject)
        assert len(result) == 64  # exactly the column width
        assert result.startswith("google:")
        # Hash budget = 64 - len("google:") = 57 hex chars.
        digest_part = result[len("google:") :]
        assert len(digest_part) == 57
        assert all(c in "0123456789abcdef" for c in digest_part)

    def test_long_subject_helper_is_deterministic(self):
        """Same subject_id → same legacy value (idempotent across
        re-writes; e.g. _promote_pending_google_entry retries)."""
        s = "pending:" + ("a" * 235) + "@example.com"
        assert _legacy_user_id_for_non_github("google", s) == _legacy_user_id_for_non_github(
            "google", s
        )

    def test_long_subject_helper_collision_resistant_on_shared_prefix(self):
        """Two distinct long subject_ids that share a 49+ char prefix
        (raw truncation would collide) must map to distinct legacy
        values via the hash branch — this is the corner case the
        downgrade migration's unique constraint relies on.
        """
        s1 = "pending:" + ("a" * 235) + "@example.com"
        s2 = "pending:" + ("a" * 235) + "@example.org"
        v1 = _legacy_user_id_for_non_github("google", s1)
        v2 = _legacy_user_id_for_non_github("google", s2)
        # Pre-fix raw truncation would have collided here:
        assert f"google:{s1}"[:64] == f"google:{s2}"[:64]
        # Hashed form does not:
        assert v1 != v2


class TestRecordBlockedSignup:
    """#655: blocked signups now write an audit_logs row in addition to
    the structlog event (CSO gate1 D2)."""

    @pytest.mark.asyncio
    async def test_writes_audit_log_with_hmac_email(self):
        """The audit row must HMAC the email — never log plaintext PII."""
        svc = _svc()
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock()

        with patch(
            "services.signup_gate_service.get_settings",
            return_value=SimpleNamespace(audit_hmac_key="test-key"),
        ):
            await svc._record_blocked_signup(
                provider="google",
                oauth_sub="108276939729829363",
                email="stranger@example.com",
                username="stranger@example.com",
                ip_address="203.0.113.7",
                user_agent="Mozilla/5.0",
            )

        svc.db.add.assert_called_once()
        audit = svc.db.add.call_args.args[0]
        assert audit.action == "signup_blocked"
        assert audit.resource == "signup_gate:google"
        assert audit.user_id == "108276939729829363"
        # Plaintext email must not leak into the row.
        assert audit.new_value_hash is not None
        assert audit.new_value_hash != "stranger@example.com"
        # 64-char hex (SHA256 hex) shape.
        assert len(audit.new_value_hash) == 64
        # IP / UA are captured for triage.
        assert audit.ip_address == "203.0.113.7"
        assert audit.user_agent == "Mozilla/5.0"
        # Metadata stores provider type only — never the email or any
        # other PII. The HMAC in new_value_hash is the canonical email
        # reference (matches the auth/roles.py precedent; PR #657 Copilot
        # review / CSO finding #1).
        assert audit.user_metadata == {"provider": "google"}
        # Defense in depth: the plaintext email MUST NOT appear anywhere
        # on the row (not in user_metadata, not in any other column).
        assert "stranger@example.com" not in str(audit.user_metadata)
        assert audit.user_email == "oauth-callback"  # the actor sentinel, not the subject
        svc.db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_audit_failure_is_swallowed(self):
        """A DB hiccup during the audit-write must NOT escalate into a
        callback 500 for the user being blocked."""
        svc = _svc()
        svc.db.add = MagicMock()
        svc.db.commit = AsyncMock(side_effect=RuntimeError("db down"))
        svc.db.rollback = AsyncMock()

        with patch(
            "services.signup_gate_service.get_settings",
            return_value=SimpleNamespace(audit_hmac_key="test-key"),
        ):
            # Must NOT raise.
            await svc._record_blocked_signup(
                provider="github",
                oauth_sub="1234",
                email="a@b.com",
                username="octocat",
                ip_address=None,
                user_agent=None,
            )

        svc.db.rollback.assert_awaited_once()


class TestBlockedResponse:
    """The blocked redirect must include reason hints (#655 D1)."""

    def test_includes_provider_and_sub_head8(self):
        svc = _svc()
        response = svc._blocked_response(provider="google", oauth_sub="108276939729829363")

        location = response.headers["location"]
        assert "/signup-blocked" in location
        assert "provider=google" in location
        # First 8 chars only (Git short-SHA convention); the rest must
        # not leak into the URL.
        assert "sub=10827693" in location
        assert "108276939729829363" not in location

    def test_no_params_when_context_absent(self):
        """Defensive call without context should still produce a valid URL."""
        svc = _svc()
        response = svc._blocked_response()

        location = response.headers["location"]
        assert "/signup-blocked" in location
        assert "?" not in location
