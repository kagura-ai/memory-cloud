"""Integration tests for RoleManager.ensure_user provider resolution (#517, #938).

``ensure_user`` resolves the owning user by ``(provider, oauth_sub)`` against
``user_oauth_providers`` (the NEW path from #517).

#938 removed the legacy ``users.user_id == oauth_sub`` fallback read and its
self-heal insert, after the e37_517 backfill saturated (a prod probe confirmed
0 un-migrated google/github users). The second test below now pins the
post-removal behavior: a user that somehow lacks a provider row still resolves
to the same account via the new-user path's IntegrityError(user_id) retry —
without spawning a duplicate User and without a self-heal provider row.

These run against a real PostgreSQL test DB (``conftest.async_engine`` skips
when unreachable). Each test seeds uuid-suffixed identifiers so parallel /
repeated runs against the session-scoped ``db_session`` don't collide on the
unique columns.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.roles import Role, RoleManager
from models.auth import User, UserOAuthProvider


def _patch_get_db_with_fresh_session(async_engine):
    """Patch ``db.base.get_db`` to yield a NEW session per call.

    ``ensure_user`` opens its own session via ``async for db in get_db():`` and
    commits inside. Reusing the test's outer ``db_session`` for that path can
    poison the outer session on a failed commit; yielding a freshly built
    AsyncSession keeps ensure_user's transaction isolated so the outer session
    can still verify committed state. Mirrors the helper in
    ``tests/integration/test_role_manager_email_sync.py``.
    """
    sessionmaker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _scope():
        async with sessionmaker() as s:
            yield s

    async def _fake():
        async with _scope() as s:
            yield s

    return patch("db.base.get_db", new=_fake)


@pytest.mark.asyncio
async def test_login_via_linked_secondary_provider_resolves_owner(db_session: AsyncSession):
    """A github login whose sub is linked to a google-registered user resolves
    that SAME user via the (provider, oauth_sub) path — no second User row."""
    suffix = uuid4().hex[:8]
    google_sub = f"g-dr-1-{suffix}"
    github_sub = f"gh-dr-2-{suffix}"
    email = f"dual-read-owner-{suffix}@example.com"

    owner = User(
        email=email,
        user_id=google_sub,
        name="Owner",
        role="user",
        auth_method="oauth",
        auth_provider="google",
    )
    db_session.add(owner)
    await db_session.flush()
    # Link a github identity to the same owner.
    db_session.add(
        UserOAuthProvider(user_id=owner.user_id, provider="github", oauth_sub=github_sub)
    )
    await db_session.commit()

    users_before = (await db_session.execute(select(func.count()).select_from(User))).scalar()

    rm = RoleManager(use_postgres=True)
    with _patch_get_db_with_fresh_session(db_session.bind):
        role = await rm.ensure_user(
            email=email,
            user_id=github_sub,
            name="Owner",
            auth_provider="github",
            email_verified=True,
        )

    assert role == Role.USER

    # No new User row was created — resolution mapped github_sub → owner.
    users_after = (await db_session.execute(select(func.count()).select_from(User))).scalar()
    assert users_after == users_before

    # The github sub must NOT have spawned its own user row.
    other = (
        await db_session.execute(select(User).filter_by(user_id=github_sub))
    ).scalar_one_or_none()
    assert other is None

    # last_used_at on the github link was touched by the login.
    link = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="github", oauth_sub=github_sub)
        )
    ).scalar_one_or_none()
    assert link is not None
    assert link.user_id == owner.user_id
    assert link.last_used_at is not None


@pytest.mark.asyncio
async def test_user_without_provider_row_still_resolves_without_selfheal(
    db_session: AsyncSession,
):
    """#938: the legacy user_id-as-sub fallback + self-heal are removed.

    A user that lacks a ``user_oauth_providers`` row (post-backfill this should
    never happen — prod probe = 0 — but the path must degrade safely) still
    resolves to the SAME account via the new-user path's IntegrityError(user_id)
    retry: no duplicate User is created. And no self-heal provider row is added
    (that behavior was intentionally removed)."""
    suffix = uuid4().hex[:8]
    legacy_sub = f"g-dr-3-{suffix}"
    email = f"dual-read-legacy-{suffix}@example.com"

    legacy = User(
        email=email,
        user_id=legacy_sub,
        name="Legacy",
        role="user",
        auth_method="oauth",
        auth_provider="google",
    )
    db_session.add(legacy)
    await db_session.commit()

    # Precondition: no provider row exists for this sub yet.
    pre = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="google", oauth_sub=legacy_sub)
        )
    ).scalar_one_or_none()
    assert pre is None

    users_before = (await db_session.execute(select(func.count()).select_from(User))).scalar()

    rm = RoleManager(use_postgres=True)
    with _patch_get_db_with_fresh_session(db_session.bind):
        role = await rm.ensure_user(
            email=email,
            user_id=legacy_sub,
            name="Legacy",
            auth_provider="google",
            email_verified=True,
        )

    assert role == Role.USER

    # Resolved to the SAME user — no duplicate row spawned by the collision retry.
    users_after = (await db_session.execute(select(func.count()).select_from(User))).scalar()
    assert users_after == users_before

    # No self-heal: the missing provider row is NOT created (removed in #938).
    healed = (
        await db_session.execute(
            select(UserOAuthProvider).filter_by(provider="google", oauth_sub=legacy_sub)
        )
    ).scalar_one_or_none()
    assert healed is None
