"""The sign-up opt-in is written with the new account, on Postgres (Issue #2761).

``RoleManager.ensure_user(..., announcement_consent=...)`` adds the consent row
in the same unit of work as the new ``users`` row (and its provider link), so
the row exists exactly when the account does:

- a NEW account gets one ``signup`` row with the answer and the wording
  version, committed with the user (the FK insert order works without a
  relationship);
- no answer → no row ("never answered");
- an EXISTING account (a returning sign-in) gets no row and keeps its answer.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.roles import RoleManager
from models.announcement_consent import AnnouncementEmailConsent
from models.auth import User
from services.announcement_consent_service import (
    ANNOUNCEMENT_CONSENT_WORDING_VERSION as V,
)
from services.announcement_consent_service import SignupAnnouncementConsent


def _fresh_sessions(async_engine):
    """``ensure_user`` opens its own session through ``db.base.get_db``."""
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _scope():
        async with maker() as s:
            yield s

    async def _fake():
        async with _scope() as s:
            yield s

    return patch("db.base.get_db", new=_fake)


async def _rows(db: AsyncSession, user_id: str) -> list[tuple[bool, str, str | None]]:
    result = await db.execute(
        select(
            AnnouncementEmailConsent.value,
            AnnouncementEmailConsent.source,
            AnnouncementEmailConsent.wording_version,
        )
        .where(AnnouncementEmailConsent.user_id == user_id)
        .order_by(AnnouncementEmailConsent.id)
    )
    return [tuple(r) for r in result.all()]


async def _sign_in(db_session: AsyncSession, sub: str, consent) -> None:
    rm = RoleManager(use_postgres=True)
    with _fresh_sessions(db_session.bind):
        await rm.ensure_user(
            email=f"{sub}@consent.invalid",
            user_id=sub,
            name="Consent",
            auth_provider="google",
            email_verified=True,
            announcement_consent=consent,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_new_account_gets_the_row_in_the_same_commit(
    db_session: AsyncSession, value: bool
) -> None:
    sub = f"g-consent-{uuid4().hex[:10]}"

    await _sign_in(db_session, sub, SignupAnnouncementConsent(value=value, wording_version=V))

    user = (await db_session.execute(select(User).where(User.user_id == sub))).scalar_one()
    assert user.email == f"{sub}@consent.invalid"
    assert await _rows(db_session, sub) == [(value, "signup", V)]


@pytest.mark.asyncio
async def test_no_answer_writes_no_row(db_session: AsyncSession) -> None:
    sub = f"g-consent-{uuid4().hex[:10]}"

    await _sign_in(db_session, sub, None)

    assert (
        await db_session.execute(select(User.id).where(User.user_id == sub))
    ).scalar_one_or_none() is not None
    assert await _rows(db_session, sub) == []


@pytest.mark.asyncio
async def test_existing_account_keeps_its_answer(db_session: AsyncSession) -> None:
    sub = f"g-consent-{uuid4().hex[:10]}"
    await _sign_in(db_session, sub, SignupAnnouncementConsent(value=False, wording_version=V))

    # A later sign-in carrying "yes" (e.g. the box ticked on /login) changes nothing.
    await _sign_in(db_session, sub, SignupAnnouncementConsent(value=True, wording_version=V))

    assert await _rows(db_session, sub) == [(False, "signup", V)]
