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

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
