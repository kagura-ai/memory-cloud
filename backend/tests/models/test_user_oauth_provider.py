"""Tests for UserOAuthProvider model (#517 — multi-provider OAuth account linking).

Verifies:
- Multiple providers can be linked to a single user.
- The UNIQUE(provider, oauth_sub) constraint prevents one OAuth identity from
  being bound to two different users.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models.auth import User, UserOAuthProvider


@pytest.mark.asyncio
async def test_link_two_providers_to_one_user(db_session):
    user = User(email="a@example.com", user_id="google-sub-1", auth_provider="google", role="user")
    db_session.add(user)
    await db_session.flush()
    db_session.add_all(
        [
            UserOAuthProvider(user_id="google-sub-1", provider="google", oauth_sub="google-sub-1"),
            UserOAuthProvider(user_id="google-sub-1", provider="github", oauth_sub="github-sub-9"),
        ]
    )
    await db_session.commit()
    rows = (
        (
            await db_session.execute(
                select(UserOAuthProvider).where(UserOAuthProvider.user_id == "google-sub-1")
            )
        )
        .scalars()
        .all()
    )
    assert {r.provider for r in rows} == {"google", "github"}


@pytest.mark.asyncio
async def test_same_provider_sub_cannot_bind_two_users(db_session):
    db_session.add_all(
        [
            User(email="a@x.com", user_id="u1", auth_provider="google", role="user"),
            User(email="b@x.com", user_id="u2", auth_provider="google", role="user"),
        ]
    )
    await db_session.flush()
    db_session.add(UserOAuthProvider(user_id="u1", provider="github", oauth_sub="gh-1"))
    await db_session.commit()
    db_session.add(UserOAuthProvider(user_id="u2", provider="github", oauth_sub="gh-1"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
