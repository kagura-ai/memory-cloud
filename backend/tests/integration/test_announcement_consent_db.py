"""Announcement-email consent against real Postgres (Issue #2761).

- the history is append-only; the newest row (highest id) is the answer, also
  for two rows written in one transaction;
- ``record_announcement_consent`` appends only on change, refuses a missing
  user and serializes parallel writers on the users row;
- ``apply_unsubscribe`` turns the setting off for the account row the token
  names (id AND user_id) and touches nothing else;
- ``select_announcement_recipients`` returns only users whose newest row is on,
  verified, not ``@local``, with no active erasure request; nobody while the
  feature is off; keyset-paged; each recipient carries working opt-out links;
- the rows go with the user (FK CASCADE).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.announcement_unsubscribe import (
    UnsubscribeClaim,
    issue_unsubscribe_token,
    read_unsubscribe_token,
)
from config.settings import get_settings
from models.announcement_consent import AnnouncementEmailConsent
from models.auth import User
from models.erasure import ErasureRequest
from services import announcement_consent_service as svc
from utils.datetime import utcnow
from utils.exceptions import NotFoundException
from utils.hashing import sha256_hex

V = svc.ANNOUNCEMENT_CONSENT_WORDING_VERSION


def _new_user(*, verified: bool = True, domain: str = "consent.invalid") -> User:
    user_id = f"ac_{uuid4().hex[:12]}"
    return User(
        email=f"{user_id}@{domain}",
        user_id=user_id,
        name="Consent Test",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
        email_verified_at=utcnow() if verified else None,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def make_users(db_session: AsyncSession) -> AsyncIterator:
    created: list[str] = []

    async def _make(**kwargs) -> User:
        user = _new_user(**kwargs)
        created.append(user.user_id)
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        return user

    yield _make
    await db_session.rollback()
    await db_session.execute(delete(ErasureRequest).where(ErasureRequest.user_id.in_(created)))
    await db_session.execute(delete(User).where(User.user_id.in_(created)))
    await db_session.commit()


@pytest.fixture
def enabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "announcement_emails_enabled", True)


async def _history(db: AsyncSession, user_id: str) -> list[tuple[bool, str, str | None]]:
    rows = (
        await db.execute(
            select(
                AnnouncementEmailConsent.value,
                AnnouncementEmailConsent.source,
                AnnouncementEmailConsent.wording_version,
            )
            .where(AnnouncementEmailConsent.user_id == user_id)
            .order_by(AnnouncementEmailConsent.id)
        )
    ).all()
    return [tuple(r) for r in rows]


async def _set(db: AsyncSession, user: User, value: bool, source: str = "settings") -> None:
    await svc.record_announcement_consent(
        db,
        user_id=user.user_id,
        value=value,
        source=source,  # type: ignore[arg-type]
        wording_version=V if value else None,
    )
    await db.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_history_appends_only_on_change(db_session: AsyncSession, make_users) -> None:
    alice = await make_users()
    assert await svc.current_announcement_consent(db_session, alice.user_id) is None

    first = await svc.record_announcement_consent(
        db_session, user_id=alice.user_id, value=True, source="settings", wording_version=V
    )
    await db_session.commit()
    assert first.recorded is True
    assert first.state.value is True and first.state.recorded_at.tzinfo is not None

    repeat = await svc.record_announcement_consent(
        db_session, user_id=alice.user_id, value=True, source="settings", wording_version=V
    )
    await db_session.commit()
    assert repeat.recorded is False

    off = await svc.record_announcement_consent(
        db_session, user_id=alice.user_id, value=False, source="settings", wording_version=V
    )
    await db_session.commit()
    assert off.recorded is True
    assert off.state.wording_version is None  # a withdrawal stores no version

    # A "yes" to newer copy is recorded again.
    await _set(db_session, alice, True)
    newer = await svc.record_announcement_consent(
        db_session, user_id=alice.user_id, value=True, source="settings", wording_version="2099-01"
    )
    await db_session.commit()
    assert newer.recorded is True

    assert await _history(db_session, alice.user_id) == [
        (True, "settings", V),
        (False, "settings", None),
        (True, "settings", V),
        (True, "settings", "2099-01"),
    ]
    state = await svc.current_announcement_consent(db_session, alice.user_id)
    assert state is not None and state.wording_version == "2099-01"


@pytest.mark.asyncio(loop_scope="session")
async def test_newest_row_wins_inside_one_transaction(db_session: AsyncSession, make_users) -> None:
    """Two rows in one transaction share ``recorded_at`` (now()); id decides."""
    bob = await make_users()
    db_session.add(
        svc.new_consent_row(user_id=bob.user_id, value=True, source="signup", wording_version=V)
    )
    db_session.add(
        svc.new_consent_row(
            user_id=bob.user_id, value=False, source="settings", wording_version=None
        )
    )
    await db_session.commit()

    times = (
        await db_session.execute(
            select(func.count(func.distinct(AnnouncementEmailConsent.recorded_at))).where(
                AnnouncementEmailConsent.user_id == bob.user_id
            )
        )
    ).scalar_one()
    assert times == 1
    state = await svc.current_announcement_consent(db_session, bob.user_id)
    assert state is not None and state.value is False


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_user_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundException):
        await svc.record_announcement_consent(
            db_session, user_id="ac_nobody", value=False, source="settings", wording_version=None
        )
    await db_session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_parallel_writers_append_one_row(db_session: AsyncSession, make_users) -> None:
    carol = await make_users()
    user_id = carol.user_id
    maker = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    async def _one() -> bool:
        async with maker() as session:
            result = await svc.record_announcement_consent(
                session, user_id=user_id, value=True, source="settings", wording_version=V
            )
            await session.commit()
            return result.recorded

    outcomes = await asyncio.gather(*(_one() for _ in range(8)))

    assert outcomes.count(True) == 1
    assert await _history(db_session, user_id) == [(True, "settings", V)]


@pytest.mark.asyncio(loop_scope="session")
async def test_unsubscribe_turns_off_only_the_named_account(
    db_session: AsyncSession, make_users
) -> None:
    dave = await make_users()
    erin = await make_users()
    await _set(db_session, dave, True)
    await _set(db_session, erin, True)

    # The row id and the user_id must both match.
    assert (
        await svc.apply_unsubscribe(
            db_session, UnsubscribeClaim(account_id=erin.id, user_id=dave.user_id)
        )
        is False
    )
    await db_session.commit()
    assert await svc.apply_unsubscribe(
        db_session, UnsubscribeClaim(account_id=dave.id, user_id=dave.user_id)
    )
    await db_session.commit()
    # Idempotent.
    assert (
        await svc.apply_unsubscribe(
            db_session, UnsubscribeClaim(account_id=dave.id, user_id=dave.user_id)
        )
        is False
    )
    await db_session.commit()

    assert await _history(db_session, dave.user_id) == [
        (True, "settings", V),
        (False, "unsubscribe_link", None),
    ]
    assert await _history(db_session, erin.user_id) == [(True, "settings", V)]
    # Nothing else on the account changed.
    refreshed = (
        await db_session.execute(select(User).where(User.user_id == dave.user_id))
    ).scalar_one()
    assert refreshed.email == dave.email and refreshed.role == "user"


@pytest.mark.asyncio(loop_scope="session")
async def test_link_of_an_erased_account_does_not_touch_a_recreated_one(
    db_session: AsyncSession,
) -> None:
    """The same OAuth subject signs up again after erasure: a new users row with
    the same user_id. A link minted for the old row does nothing to the new one."""
    old = _new_user()
    db_session.add(old)
    await db_session.commit()
    await db_session.refresh(old)
    old_id, user_id, email = old.id, old.user_id, old.email
    claim = read_unsubscribe_token(issue_unsubscribe_token(account_id=old_id, user_id=user_id))
    assert claim == UnsubscribeClaim(old_id, user_id)

    await db_session.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})
    await db_session.commit()
    db_session.expunge_all()
    again = _new_user()
    again.user_id = user_id
    again.email = email
    db_session.add(again)
    await db_session.commit()
    await db_session.refresh(again)
    try:
        assert again.id != old_id
        await _set(db_session, again, True)

        assert await svc.apply_unsubscribe(db_session, claim) is False
        await db_session.commit()

        assert await _history(db_session, user_id) == [(True, "settings", V)]
    finally:
        await db_session.rollback()
        await db_session.execute(delete(User).where(User.user_id == user_id))
        await db_session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_unsubscribe_for_an_unknown_account_does_nothing(db_session: AsyncSession) -> None:
    assert (
        await svc.apply_unsubscribe(
            db_session, UnsubscribeClaim(account_id=2_000_000_000, user_id="ac_gone")
        )
        is False
    )
    await db_session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_recipients(db_session: AsyncSession, make_users, enabled, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "frontend_url", "https://app.example.com")
    on = await make_users()
    never = await make_users()
    off_again = await make_users()
    unverified = await make_users(verified=False)
    local = await make_users(domain="local")
    leaving = await make_users()
    erased_before = await make_users()

    for user in (on, off_again, unverified, local, leaving, erased_before):
        await _set(db_session, user, True)
    await _set(db_session, off_again, False)
    db_session.add_all(
        [
            ErasureRequest(
                user_id=leaving.user_id,
                user_email_hash=sha256_hex(leaving.email),
                initiated_by=leaving.user_id,
                is_self_service=True,
                reason_code="self_service",
                status="cooling_off",
                scheduled_for=utcnow() + timedelta(days=7),
            ),
            # A cancelled request does not exclude.
            ErasureRequest(
                user_id=erased_before.user_id,
                user_email_hash=sha256_hex(erased_before.email),
                initiated_by=erased_before.user_id,
                is_self_service=True,
                reason_code="self_service",
                status="cancelled",
                scheduled_for=utcnow() - timedelta(days=7),
            ),
        ]
    )
    await db_session.commit()

    ours = {on.user_id, erased_before.user_id}
    everyone = []
    after = None
    while True:
        page = await svc.select_announcement_recipients(db_session, after_user_id=after, limit=1)
        if not page:
            break
        assert len(page) == 1
        everyone.extend(page)
        after = page[-1].user_id
    picked = [r for r in everyone if r.user_id.startswith("ac_")]

    assert {r.user_id for r in picked} >= ours
    for excluded in (never, off_again, unverified, local, leaving):
        assert excluded.user_id not in {r.user_id for r in picked}
    assert [r.user_id for r in everyone] == sorted(r.user_id for r in everyone)

    mine = next(r for r in picked if r.user_id == on.user_id)
    assert mine.email == on.email and mine.locale == "en"
    header = mine.list_unsubscribe_headers["List-Unsubscribe"]
    token = header.rsplit("/", 1)[1].rstrip(">")
    assert read_unsubscribe_token(token) == UnsubscribeClaim(on.id, on.user_id)
    assert mine.unsubscribe_page_url.startswith("https://app.example.com/email/unsubscribe#")

    # The feature off selects nobody.
    monkeypatch.setattr(get_settings(), "announcement_emails_enabled", False)
    assert await svc.select_announcement_recipients(db_session) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_rows_go_with_the_user(db_session: AsyncSession) -> None:
    user = _new_user()
    db_session.add(user)
    await db_session.commit()
    await _set(db_session, user, True)

    await db_session.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user.user_id})
    await db_session.commit()

    assert await _history(db_session, user.user_id) == []
