"""The email sign-up writes its announcement opt-in with the account, on Postgres.

#1734 + #2761: the box on ``/signup/verify`` is answered by the person who
opened the emailed link; ``EmailSignupService.complete`` stages the history row
after the ``users`` row is flushed, inside the transaction that creates the
account. The answer is resolved as the route resolves it
(``resolve_signup_consent``), so:

- no answer -> no row; ``false`` -> a ``false`` row; ``true`` with the current
  wording version -> a ``true`` row; a stale version -> no row;
- an address that has an account by now (409) -> no row anywhere;
- a failure after the admission -> nothing at all (no account, no row).

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.email_signup_service as svc_module
from config.settings import get_settings
from models.announcement_consent import AnnouncementEmailConsent
from models.auth import User
from models.signup_gate import SignupGateConfig
from services.announcement_consent_service import (
    ANNOUNCEMENT_CONSENT_WORDING_VERSION as V,
)
from services.announcement_consent_service import resolve_signup_consent
from services.email_signup_service import EmailSignupService, SignupConsents
from services.signup_gate_service import SignupGateService
from utils.datetime import utcnow
from utils.exceptions import SignupAccountExistsError

PASSWORD = "Correct-Horse-Battery-42"
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _addr(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}@example.test"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def open_signup(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession):
    """Open registration, gate off, announcements offered; not the first account."""
    seed = User(
        email=_addr("seed"),
        user_id=f"seed-{uuid4().hex[:10]}",
        role="user",
        auth_method="oauth",
        auth_provider="google",
    )
    db_session.add(seed)
    await db_session.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_email_signup", True)
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "terms_version", "")
    monkeypatch.setattr(settings, "announcement_emails_enabled", True)
    monkeypatch.setattr(svc_module, "_claim_notice_slot", AsyncMock(return_value=True))
    monkeypatch.setattr(svc_module, "_over_daily_invite_limit", AsyncMock(return_value=False))
    gate = SignupGateService(db_session)
    config = await gate.get_config()
    previous = (config.enabled, config.mode)
    await gate.update_config(enabled=False, mode="manual")
    yield settings
    await db_session.rollback()
    await db_session.execute(
        update(SignupGateConfig)
        .where(SignupGateConfig.id == 1)
        .values(enabled=previous[0], mode=previous[1])
    )
    await db_session.commit()


async def _token(maker, email: str) -> str:
    async with maker() as s:
        pending = await EmailSignupService(s).request(
            email=email, beta_invite_token_hash=None, locale="en", timezone="UTC"
        )
    return pending.verify_url.split("token=", 1)[1]


async def _complete(maker, token: str, value: object, version: object):
    async with maker() as s:
        return await EmailSignupService(s).complete(
            raw_token=token,
            new_password=PASSWORD,
            name=None,
            consents=SignupConsents(
                terms_version=None, announcement=resolve_signup_consent(value, version)
            ),
        )


async def _rows(db: AsyncSession, user_id: str) -> list[tuple[bool, str, str | None]]:
    db.expire_all()
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


async def _consent_rows_for_address(db: AsyncSession, email: str) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count())
            .select_from(AnnouncementEmailConsent)
            .join(User, User.user_id == AnnouncementEmailConsent.user_id)
            .where(func.lower(User.email) == email.lower())
        )
        or 0
    )


@pytest.mark.parametrize(
    ("value", "version", "expected"),
    [
        (None, None, []),
        (False, V, [(False, "signup", V)]),
        (True, V, [(True, "signup", V)]),
        (True, "2000-01", []),
    ],
    ids=["absent", "false", "true-current", "stale-version"],
)
async def test_new_account_records_the_answer(
    open_signup, maker, db_session, value, version, expected
) -> None:
    email = _addr("ann")
    created = await _complete(maker, await _token(maker, email), value, version)
    assert await _rows(db_session, created.user_id) == expected


async def test_existing_address_writes_no_row(open_signup, maker, db_session) -> None:
    email = _addr("ann-exists")
    token = await _token(maker, email)
    holder = User(
        email=email.upper(),
        user_id=f"g-{uuid4().hex[:10]}",
        role="user",
        auth_method="oauth",
        auth_provider="google",
        email_verified_at=utcnow(),
    )
    db_session.add(holder)
    await db_session.commit()

    with pytest.raises(SignupAccountExistsError):
        await _complete(maker, token, True, V)

    assert await _consent_rows_for_address(db_session, email) == 0


async def test_failure_after_admission_writes_nothing(
    open_signup, maker, db_session, monkeypatch
) -> None:
    email = _addr("ann-fail")
    token = await _token(maker, email)
    staged: list[str] = []
    real_stage = svc_module.stage_signup_consents

    async def _stage(db, *, user_id, consents):
        await real_stage(db, user_id=user_id, consents=consents)
        staged.append(user_id)

    monkeypatch.setattr(svc_module, "stage_signup_consents", _stage)
    # The audit row is built after the consent row was staged.
    monkeypatch.setattr(svc_module, "AuditLog", MagicMock(side_effect=RuntimeError("injected")))

    with pytest.raises(RuntimeError):
        await _complete(maker, token, True, V)

    assert len(staged) == 1
    db_session.expire_all()
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(func.lower(User.email) == email)
        )
    ) == 0
    assert await _rows(db_session, staged[0]) == []
