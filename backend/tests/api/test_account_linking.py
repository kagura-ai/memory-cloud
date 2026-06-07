"""Service-level tests for AccountLinkingService (#517 Task 4).

Covers the link/unlink/list logic plus the gate1 security edge cases:

- link unbound identity -> INSERT + ``oauth_provider_linked`` audit row
- link already-mine identity -> idempotent ``last_used_at`` touch (no dup/error)
- link identity owned by another user -> ``oauth_provider_link_failed`` audit
  row IS written, then ConflictError (edge case 6: failures are audited)
- unlink that would leave zero sign-in methods -> ConflictError
- unlink the legacy "primary" provider repoints ``User.auth_provider``
  (edge case 7)
- list_providers returns the user's provider rows

These run against the real PostgreSQL test DB (``conftest.async_engine`` skips
when unreachable). The ``db_session`` fixture is SESSION-SCOPED, so every test
seeds uuid-suffixed identifiers to avoid colliding on the unique columns.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import me_account
from api.routes.me_account import (
    LinkProviderRequest,
    LinkProviderResponse,
    ProvidersListResponse,
    UnlinkProviderRequest,
    link_provider,
    list_providers,
    unlink_provider,
)
from models.auth import AuditLog, User, UserOAuthProvider
from services.account_linking_service import AccountLinkingService
from utils.exceptions import ConflictError, NotFoundException


async def _make_user(
    db: AsyncSession,
    *,
    suffix: str,
    auth_method: str = "oauth",
    auth_provider: str | None = "google",
    password_hash: str | None = None,
) -> User:
    user = User(
        email=f"link-{suffix}@example.com",
        user_id=f"u-{suffix}",
        name="Link Tester",
        role="user",
        auth_method=auth_method,
        auth_provider=auth_provider,
        password_hash=password_hash,
    )
    db.add(user)
    await db.commit()
    return user


async def _audit_rows(db: AsyncSession, user_id: str, action: str) -> list[AuditLog]:
    return list(
        (await db.execute(select(AuditLog).filter_by(user_id=user_id, action=action)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_link_unbound_inserts_row_and_audit(db_session: AsyncSession):
    suffix = uuid4().hex[:8]
    user = await _make_user(db_session, suffix=suffix)
    sub = f"gh-{suffix}"
    svc = AccountLinkingService(db_session)

    await svc.link(
        user_id=user.user_id,
        provider="github",
        oauth_sub=sub,
        email=user.email,
        ip_address="1.2.3.4",
        user_agent="pytest",
    )

    row = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="github", oauth_sub=sub)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.user_id == user.user_id
    assert row.last_used_at is not None

    audits = await _audit_rows(db_session, user.user_id, "oauth_provider_linked")
    assert len(audits) == 1
    assert audits[0].resource == "oauth_provider:github"


@pytest.mark.asyncio
async def test_link_already_mine_is_idempotent_touch(db_session: AsyncSession):
    suffix = uuid4().hex[:8]
    user = await _make_user(db_session, suffix=suffix)
    sub = f"gh-{suffix}"
    svc = AccountLinkingService(db_session)

    await svc.link(user_id=user.user_id, provider="github", oauth_sub=sub, email=user.email)
    first = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="github", oauth_sub=sub)
        )
    ).scalar_one()
    first_used = first.last_used_at

    # Re-link the same identity to the same user: no error, no duplicate row.
    await svc.link(user_id=user.user_id, provider="github", oauth_sub=sub, email=user.email)

    rows = list(
        (
            await db_session.execute(
                select(UserOAuthProvider).filter_by(provider="github", oauth_sub=sub)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].last_used_at is not None
    assert rows[0].last_used_at >= first_used

    # Idempotent touch must NOT emit a second linked audit.
    audits = await _audit_rows(db_session, user.user_id, "oauth_provider_linked")
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_link_owned_by_other_audits_failure_then_conflict(db_session: AsyncSession):
    suffix = uuid4().hex[:8]
    owner = await _make_user(db_session, suffix=f"owner-{suffix}")
    intruder = await _make_user(db_session, suffix=f"intruder-{suffix}")
    sub = f"gh-{suffix}"
    svc = AccountLinkingService(db_session)

    # Owner links the identity first.
    await svc.link(user_id=owner.user_id, provider="github", oauth_sub=sub, email=owner.email)

    # Intruder attempts to link the SAME identity -> ConflictError + failed audit.
    with pytest.raises(ConflictError):
        await svc.link(
            user_id=intruder.user_id,
            provider="github",
            oauth_sub=sub,
            email=intruder.email,
        )

    # Edge case 6: the failed attempt IS audited.
    failed = await _audit_rows(db_session, intruder.user_id, "oauth_provider_link_failed")
    assert len(failed) == 1
    assert failed[0].resource == "oauth_provider:github"

    # The identity still belongs to the owner (no hijack).
    row = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="github", oauth_sub=sub)
        )
    ).scalar_one()
    assert row.user_id == owner.user_id


@pytest.mark.asyncio
async def test_unlink_last_method_raises_conflict(db_session: AsyncSession):
    suffix = uuid4().hex[:8]
    # oauth-only user with a single linked provider and no usable password.
    user = await _make_user(db_session, suffix=suffix, auth_provider="google")
    sub = f"g-{suffix}"
    svc = AccountLinkingService(db_session)
    await svc.link(user_id=user.user_id, provider="google", oauth_sub=sub, email=user.email)

    with pytest.raises(ConflictError):
        await svc.unlink(user_id=user.user_id, provider="google")

    # Provider row must still exist — nothing was deleted.
    row = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="google", oauth_sub=sub)
        )
    ).scalar_one_or_none()
    assert row is not None


@pytest.mark.asyncio
async def test_unlink_not_linked_raises_not_found(db_session: AsyncSession):
    suffix = uuid4().hex[:8]
    # Password user with one linked provider; unlinking a DIFFERENT provider
    # they never linked must 404, and must not delete the existing row.
    user = await _make_user(
        db_session,
        suffix=suffix,
        auth_method="password",
        auth_provider=None,
        password_hash="hashed",
    )
    sub = f"g-{suffix}"
    svc = AccountLinkingService(db_session)
    await svc.link(user_id=user.user_id, provider="google", oauth_sub=sub, email=user.email)

    with pytest.raises(NotFoundException):
        await svc.unlink(user_id=user.user_id, provider="github")

    # The provider the user actually linked must still exist — nothing deleted.
    row = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="google", oauth_sub=sub)
        )
    ).scalar_one_or_none()
    assert row is not None


@pytest.mark.asyncio
async def test_unlink_secondary_keeps_primary_succeeds(db_session: AsyncSession):
    """Unlinking a secondary provider succeeds while the primary remains.

    Post-#938 (legacy ``users.user_id``-as-sub fallback + self-heal removed,
    backfill saturated) every usable OAuth method has a ``user_oauth_providers``
    row, so ``remaining_methods`` is computed purely from the rows + password.
    A user with both a google and a github row unlinks github → remaining_methods
    = (2-1)+0 = 1 → succeeds, the google row stays, and ``auth_provider`` (the
    denormalized primary pointer) is untouched because we unlinked the secondary.

    (This replaces the old ``test_unlink_with_legacy_fallback_provider_succeeds``,
    which pinned the removed legacy_provider_usable term. Its scenario — a user
    with ``auth_provider='google'`` but NO google row — can no longer occur:
    the e37_517 backfill covered all such users and the new-user path always
    writes a provider row for known providers.)"""
    suffix = uuid4().hex[:8]
    user = await _make_user(db_session, suffix=suffix, auth_provider="google")
    svc = AccountLinkingService(db_session)
    # Backfill model: the primary (google) has a real provider row, as does github.
    await svc.link(
        user_id=user.user_id, provider="google", oauth_sub=f"g-{suffix}", email=user.email
    )
    await svc.link(
        user_id=user.user_id, provider="github", oauth_sub=f"gh-{suffix}", email=user.email
    )

    # Unlink the secondary: (2-1)+0 = 1 remaining → succeeds.
    await svc.unlink(user_id=user.user_id, provider="github")

    remaining = await svc.list_providers(user.user_id)
    assert {r.provider for r in remaining} == {"google"}

    # auth_provider untouched (we unlinked github, not the primary).
    refreshed = (
        await db_session.execute(select(User).filter_by(user_id=user.user_id))
    ).scalar_one()
    assert refreshed.auth_provider == "google"


@pytest.mark.asyncio
async def test_unlink_primary_repoints_auth_provider(db_session: AsyncSession):
    suffix = uuid4().hex[:8]
    user = await _make_user(db_session, suffix=suffix, auth_provider="google")
    g_sub = f"g-{suffix}"
    gh_sub = f"gh-{suffix}"
    svc = AccountLinkingService(db_session)
    await svc.link(user_id=user.user_id, provider="google", oauth_sub=g_sub, email=user.email)
    await svc.link(user_id=user.user_id, provider="github", oauth_sub=gh_sub, email=user.email)

    # Unlink the legacy "primary" (google); auth_provider must repoint to github.
    await svc.unlink(user_id=user.user_id, provider="google")

    refreshed = (
        await db_session.execute(select(User).filter_by(user_id=user.user_id))
    ).scalar_one()
    assert refreshed.auth_provider == "github"

    remaining = await svc.list_providers(user.user_id)
    assert {r.provider for r in remaining} == {"github"}

    audits = await _audit_rows(db_session, user.user_id, "oauth_provider_unlinked")
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_list_providers_returns_user_rows(db_session: AsyncSession):
    suffix = uuid4().hex[:8]
    user = await _make_user(db_session, suffix=suffix)
    svc = AccountLinkingService(db_session)
    await svc.link(
        user_id=user.user_id,
        provider="google",
        oauth_sub=f"g-{suffix}",
        email=user.email,
    )
    await svc.link(
        user_id=user.user_id,
        provider="github",
        oauth_sub=f"gh-{suffix}",
        email=user.email,
    )

    rows = await svc.list_providers(user.user_id)
    assert {r.provider for r in rows} == {"google", "github"}
    assert all(r.user_id == user.user_id for r in rows)


# ---------------------------------------------------------------------------
# Endpoint-level tests (#517 Task 5)
#
# These call the route handlers directly (the repo's established pattern for
# me_account / me_oauth — see test_me_account.py / test_me_refresh_oauth.py),
# mocking the session user via a plain dict, the Redis handle via
# ``auth_module._session_manager._redis``, and the linking service where the
# DB-backed logic is already covered by the service tests above.
# ---------------------------------------------------------------------------


def _session(*, user_id: str = "u-link-1") -> dict:
    """Minimal SessionUser dict — handlers only read ``user_id``."""
    return {"user_id": user_id}


def _request() -> SimpleNamespace:
    """Minimal Request stand-in for ``.client.host`` / ``.headers.get``."""
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"user-agent": "pytest"},
    )


def _mock_managers():
    """Patch the OAuth manager singletons ``link_provider`` reads.

    Returns the redis mock so tests can assert the four state keys the
    link-mode callback later consumes.
    """
    redis = MagicMock()
    session_manager = MagicMock()
    session_manager._redis = redis

    oauth2_manager = MagicMock()
    oauth2_manager.get_authorization_url_web.return_value = (
        "https://accounts.google.com/o/oauth2/auth?state=...&..."
    )
    return session_manager, oauth2_manager, redis


class TestLinkProviderEndpoint:
    @pytest.mark.asyncio
    async def test_link_provider_initiates_oauth_without_password(self, monkeypatch):
        """edge case 1: a session user can start a github link with NO
        password prompt — the OAuth round-trip is the fresh re-auth.

        Asserts 200-shape (authorization_url + state) and that all four
        link-mode Redis state keys are written, with intent == "link" and
        the originating user pinned.
        """
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client")
        monkeypatch.setenv(
            "GITHUB_REDIRECT_URI", "http://localhost:8080/api/v1/auth/github/callback"
        )

        with (
            patch.object(me_account.auth_module, "_session_manager", session_manager),
            patch.object(me_account.auth_module, "_oauth2_manager", oauth2_manager),
        ):
            result = await link_provider(
                body=LinkProviderRequest(provider="github"),
                user=_session(user_id="u-gh"),
            )

        assert isinstance(result, LinkProviderResponse)
        assert result.authorization_url.startswith("http")
        assert "github.com/login/oauth/authorize" in result.authorization_url
        assert result.state  # non-empty CSRF token

        keys_written = {call.args[0] for call in redis.setex.call_args_list}
        assert f"oauth2_state:{result.state}" in keys_written
        assert f"oauth2_state_intent:{result.state}" in keys_written
        assert f"oauth2_state_user:{result.state}" in keys_written
        assert f"oauth2_return_to:{result.state}" in keys_written

        intent_call = next(
            c
            for c in redis.setex.call_args_list
            if c.args[0] == f"oauth2_state_intent:{result.state}"
        )
        assert intent_call.args[2] == "link"
        user_call = next(
            c
            for c in redis.setex.call_args_list
            if c.args[0] == f"oauth2_state_user:{result.state}"
        )
        assert user_call.args[2] == "u-gh"
        return_call = next(
            c for c in redis.setex.call_args_list if c.args[0] == f"oauth2_return_to:{result.state}"
        )
        assert return_call.args[2] == "/profile?linked=1"

    @pytest.mark.asyncio
    async def test_link_provider_google(self, monkeypatch):
        """Google link → manager-built authorization URL, intent == link."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        with (
            patch.object(me_account.auth_module, "_session_manager", session_manager),
            patch.object(me_account.auth_module, "_oauth2_manager", oauth2_manager),
        ):
            result = await link_provider(
                body=LinkProviderRequest(provider="google"),
                user=_session(),
            )
        assert result.authorization_url.startswith("https://accounts.google.com/")

    @pytest.mark.asyncio
    async def test_link_provider_missing_env_does_not_pollute_redis(self, monkeypatch):
        """Missing GITHUB_CLIENT_ID → 500 BEFORE any Redis write (config is
        resolved before state keys are persisted, so none are orphaned)."""
        session_manager, oauth2_manager, redis = _mock_managers()
        monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)

        with (
            patch.object(me_account.auth_module, "_session_manager", session_manager),
            patch.object(me_account.auth_module, "_oauth2_manager", oauth2_manager),
        ):
            with pytest.raises(Exception) as exc_info:
                await link_provider(
                    body=LinkProviderRequest(provider="github"),
                    user=_session(),
                )
            assert exc_info.value.status_code == 500  # type: ignore[attr-defined]
        redis.setex.assert_not_called()


class TestProvidersListEndpoint:
    @pytest.mark.asyncio
    async def test_get_providers_lists_linked(self):
        """GET providers → 200 listing the session user's linked providers,
        with datetimes serialized to ISO-8601 Z strings."""
        linked = datetime(2026, 1, 2, 3, 4, 5)  # naive UTC, as stored
        rows = [
            SimpleNamespace(provider="google", linked_at=linked, last_used_at=linked),
            SimpleNamespace(provider="github", linked_at=linked, last_used_at=None),
        ]
        with patch.object(me_account, "AccountLinkingService") as mock_cls:
            mock_cls.return_value.list_providers = AsyncMock(return_value=rows)
            result = await list_providers(user=_session(), db=AsyncMock())

        assert isinstance(result, ProvidersListResponse)
        assert {p.provider for p in result.providers} == {"google", "github"}
        google = next(p for p in result.providers if p.provider == "google")
        assert google.linked_at == "2026-01-02T03:04:05Z"
        assert google.last_used_at == "2026-01-02T03:04:05Z"
        github = next(p for p in result.providers if p.provider == "github")
        assert github.last_used_at is None


class TestUnlinkProviderEndpoint:
    @pytest.mark.asyncio
    async def test_unlink_provider_endpoint(self):
        """Unlinking one of two providers → 200 {"status": "ok"}; the handler
        forwards ip/user-agent to the service for the audit row."""
        with patch.object(me_account, "AccountLinkingService") as mock_cls:
            mock_cls.return_value.unlink = AsyncMock(return_value=None)
            result = await unlink_provider(
                body=UnlinkProviderRequest(provider="github"),
                request=_request(),
                user=_session(),
                db=AsyncMock(),
            )

        assert result.status == "ok"
        _, kwargs = mock_cls.return_value.unlink.call_args
        assert kwargs["provider"] == "github"
        assert kwargs["ip_address"] == "127.0.0.1"
        assert kwargs["user_agent"] == "pytest"

    @pytest.mark.asyncio
    async def test_unlink_last_method_propagates_conflict(self):
        """Last-method unlink → service raises ConflictError (→ 409 via the
        global exception handler); the handler does not swallow it."""
        with patch.object(me_account, "AccountLinkingService") as mock_cls:
            mock_cls.return_value.unlink = AsyncMock(
                side_effect=ConflictError("Cannot unlink the only remaining sign-in method")
            )
            with pytest.raises(ConflictError) as exc_info:
                await unlink_provider(
                    body=UnlinkProviderRequest(provider="google"),
                    request=_request(),
                    user=_session(),
                    db=AsyncMock(),
                )
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_unlink_not_linked_propagates_not_found(self):
        """Unlinking a provider that isn't linked → NotFoundException (404)."""
        with patch.object(me_account, "AccountLinkingService") as mock_cls:
            mock_cls.return_value.unlink = AsyncMock(
                side_effect=NotFoundException("OAuth provider", resource_id="github")
            )
            with pytest.raises(NotFoundException) as exc_info:
                await unlink_provider(
                    body=UnlinkProviderRequest(provider="github"),
                    request=_request(),
                    user=_session(),
                    db=AsyncMock(),
                )
        assert exc_info.value.status_code == 404
