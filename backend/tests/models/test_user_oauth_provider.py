"""Tests for UserOAuthProvider model (#517 — multi-provider OAuth account linking).

Verifies:
- Multiple providers can be linked to a single user.
- The UNIQUE(provider, oauth_sub) constraint prevents one OAuth identity from
  being bound to two different users.

Each test seeds uuid-suffixed identifiers so parallel / repeated runs against
the session-scoped ``db_session`` don't collide on the unique columns.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models.auth import User, UserOAuthProvider


@pytest.mark.asyncio
async def test_link_two_providers_to_one_user(db_session):
    suffix = uuid4().hex[:8]
    user_id = f"google-sub-{suffix}"
    email = f"link-two-{suffix}@example.com"
    github_sub = f"github-sub-{suffix}"

    user = User(email=email, user_id=user_id, auth_provider="google", role="user")
    db_session.add(user)
    await db_session.flush()
    db_session.add_all(
        [
            UserOAuthProvider(user_id=user_id, provider="google", oauth_sub=user_id),
            UserOAuthProvider(user_id=user_id, provider="github", oauth_sub=github_sub),
        ]
    )
    await db_session.commit()
    rows = (
        (
            await db_session.execute(
                select(UserOAuthProvider).where(UserOAuthProvider.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    assert {r.provider for r in rows} == {"google", "github"}


@pytest.mark.asyncio
async def test_same_provider_sub_cannot_bind_two_users(db_session):
    suffix = uuid4().hex[:8]
    user_id_1 = f"u1-{suffix}"
    user_id_2 = f"u2-{suffix}"
    # Both users try to claim the SAME oauth_sub — constraint must fire.
    shared_oauth_sub = f"gh-shared-{suffix}"

    db_session.add_all(
        [
            User(email=f"a-{suffix}@x.com", user_id=user_id_1, auth_provider="google", role="user"),
            User(email=f"b-{suffix}@x.com", user_id=user_id_2, auth_provider="google", role="user"),
        ]
    )
    await db_session.flush()
    db_session.add(
        UserOAuthProvider(user_id=user_id_1, provider="github", oauth_sub=shared_oauth_sub)
    )
    await db_session.commit()
    db_session.add(
        UserOAuthProvider(user_id=user_id_2, provider="github", oauth_sub=shared_oauth_sub)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_same_user_cannot_link_same_provider_twice(db_session):
    suffix = uuid4().hex[:8]
    user_id = f"u3-{suffix}"
    oauth_sub_1 = f"gh-{suffix}-a"
    oauth_sub_2 = f"gh-{suffix}-b"

    db_session.add(
        User(email=f"c-{suffix}@x.com", user_id=user_id, auth_provider="google", role="user")
    )
    await db_session.flush()
    db_session.add(UserOAuthProvider(user_id=user_id, provider="github", oauth_sub=oauth_sub_1))
    await db_session.commit()
    db_session.add(UserOAuthProvider(user_id=user_id, provider="github", oauth_sub=oauth_sub_2))
    with pytest.raises(IntegrityError):
        await db_session.commit()
