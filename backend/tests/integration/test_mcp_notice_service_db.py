"""Service notices over MCP against real Postgres (Issue #2762).

Consent:
- append-only, newest row wins, appended only on change, the users row lock
  serialises parallel writers, a missing user is refused;
- a notice-consent row is never read as an announcement-email answer and an
  announcement-email row is never read as a notice answer (cross-purpose).

Writing:
- caps: 3 live or scheduled, 5 created in 24 hours (withdrawn ones count),
  the window rules; two concurrent creates at 2 active: exactly one wins;
- every create / withdraw writes one audit row with the text's sha256 and
  never the text; withdraw is idempotent;
- the list carries an aggregate ``delivered_count`` and filters by state.

Delivery:
- the candidates are live, not delivered, for people whose newest answer is
  "yes" and who have no erasure request under way, in expires / starts / id
  order;
- the claim is atomic: two concurrent claims for one person, exactly one row;
  a withdraw, an opt-out, an expiry or an erasure request between the read
  and the claim removes the notice; a claim for a deleted user raises (the
  caller drops the field); a claim behind another transaction's lock (a
  locked users row, a pending delivery row) ends at its own ``lock_timeout``
  and ``claim_notices`` returns no block in time;
- deliveries and consents go with the user.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.announcement_consent import AnnouncementEmailConsent
from models.auth import AuditLog, User
from models.erasure import (
    STATUS_CANCELLED,
    STATUS_COMPLETE,
    STATUS_COOLING_OFF,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    ErasureRequest,
)
from models.mcp_notice import McpNotice, McpNoticeConsent, McpNoticeDelivery
from services import announcement_consent_service as announcements
from services import mcp_notice_service as svc
from utils.exceptions import (
    McpNoticeActiveLimitError,
    McpNoticeDailyLimitError,
    McpNoticeNotFoundError,
    McpNoticeTextRejectedError,
    McpNoticeWindowInvalidError,
    NotFoundException,
)
from utils.hashing import sha256_hex

V = svc.MCP_NOTICES_WORDING_VERSION
ACTOR = svc.NoticeActor(
    email="operator:ops@example.com",
    user_id="operator:ops@example.com",
    ip_address="192.0.2.10",
    user_agent="console/1.0",
)


def _new_user() -> User:
    user_id = f"mn_{uuid4().hex[:12]}"
    return User(
        email=f"{user_id}@notice.invalid",
        user_id=user_id,
        name="Notice Test",
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
    )


async def _clear_notices(db: AsyncSession) -> None:
    await db.execute(delete(McpNotice))
    await db.execute(delete(AuditLog).where(AuditLog.resource.like("mcp_notice:%")))
    await db.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def make_users(db_session: AsyncSession) -> AsyncIterator:
    created: list[str] = []
    await _clear_notices(db_session)

    async def _make() -> User:
        user = _new_user()
        created.append(user.user_id)
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        return user

    yield _make
    await db_session.rollback()
    await _clear_notices(db_session)
    await db_session.execute(delete(ErasureRequest).where(ErasureRequest.user_id.in_(created)))
    await db_session.execute(delete(User).where(User.user_id.in_(created)))
    await db_session.commit()


def _maker(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)


async def _now(db: AsyncSession):
    return (await db.execute(select(func.now()))).scalar_one()


async def _opt(db: AsyncSession, user: User, value: bool) -> None:
    await svc.record_mcp_notice_consent(
        db, user_id=user.user_id, value=value, wording_version=V if value else None
    )
    await db.commit()


async def _create(db: AsyncSession, body: str = "Maintenance on Saturday.", **kw) -> svc.NoticeView:
    now = await _now(db)
    view = await svc.create_notice(
        db,
        raw_text=body,
        expires_at=kw.pop("expires_at", now + timedelta(days=2)),
        starts_at=kw.pop("starts_at", None),
        actor=ACTOR,
    )
    await db.commit()
    return view


async def _raw_notice(db: AsyncSession, *, start_offset: timedelta, end_offset: timedelta, **kw):
    """A row written straight to the table (no caps), for windows in the past."""
    now = await _now(db)
    notice = McpNotice(
        id=uuid.uuid4(),
        text=kw.get("body", "Raw notice."),
        starts_at=now + start_offset,
        expires_at=now + end_offset,
        withdrawn_at=kw.get("withdrawn_at"),
        created_at=now - timedelta(days=2),
    )
    db.add(notice)
    await db.commit()
    return notice.id


async def _raw_audit_rows(db: AsyncSession, notice_id: uuid.UUID) -> list[dict]:
    """Every column of the notice's audit rows, read in a fresh session (not
    the identity map) with ``SELECT *`` so a column the ORM does not map is
    covered too."""
    async with _maker(db)() as session:
        result = await session.execute(
            text("SELECT * FROM audit_logs WHERE resource = :resource ORDER BY id"),
            {"resource": f"mcp_notice:{notice_id}"},
        )
        return [dict(row) for row in result.mappings().all()]


async def _fresh_audits(db: AsyncSession, notice_id: uuid.UUID) -> list[AuditLog]:
    """The notice's audit rows through the ORM, read in a fresh session (so
    ``user_metadata`` is what the database holds, not the identity map)."""
    async with _maker(db)() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.resource == f"mcp_notice:{notice_id}")
            .order_by(AuditLog.id)
        )
        return list(result.scalars().all())


# Values the audit row legitimately holds next to the text's hash. A body
# window that occurred in one of them would make the window check below fail
# for a reason that is not a leak, so the check refuses such a body first.
_AUDIT_NON_TEXT_VALUES = (
    ACTOR.email,
    ACTOR.user_id,
    ACTOR.ip_address or "",
    ACTOR.user_agent or "",
    "mcp_notice.created",
    "mcp_notice.withdrawn",
)

_WINDOW = 8


def _assert_text_in_no_column(rows: list[dict], body: str) -> None:
    """No 8-character window of the body is in any stringified column, so a
    prefix, a slice or a preview of the text is caught, not only the whole."""
    assert rows
    assert len(body) >= _WINDOW
    windows = {body[i : i + _WINDOW] for i in range(len(body) - _WINDOW + 1)}
    for value in _AUDIT_NON_TEXT_VALUES:
        assert not [w for w in windows if w in value], (body, value)
    for row in rows:
        assert len(row) >= len(sa_inspect(AuditLog).column_attrs)
        for column, value in row.items():
            if column == "resource":
                assert value.startswith("mcp_notice:"), value
            leaked = sorted(w for w in windows if w in str(value))
            assert not leaked, (column, leaked)


async def _claim(db: AsyncSession, user: User, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    async with _maker(db)() as session:
        return await svc.claim_notice_deliveries(session, user_id=user.user_id, notice_ids=ids)


# --- consent ------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_consent_history_appends_only_on_change(db_session, make_users) -> None:
    alice = await make_users()
    assert await svc.current_mcp_notice_consent(db_session, alice.user_id) is None

    first = await svc.record_mcp_notice_consent(
        db_session, user_id=alice.user_id, value=True, wording_version=V
    )
    await db_session.commit()
    repeat = await svc.record_mcp_notice_consent(
        db_session, user_id=alice.user_id, value=True, wording_version=V
    )
    await db_session.commit()
    off = await svc.record_mcp_notice_consent(
        db_session, user_id=alice.user_id, value=False, wording_version=V
    )
    await db_session.commit()
    newer = await svc.record_mcp_notice_consent(
        db_session, user_id=alice.user_id, value=True, wording_version="2099-01"
    )
    await db_session.commit()

    assert (first.recorded, repeat.recorded, off.recorded, newer.recorded) == (
        True,
        False,
        True,
        True,
    )
    assert off.state.wording_version is None
    rows = (
        await db_session.execute(
            select(
                McpNoticeConsent.value, McpNoticeConsent.source, McpNoticeConsent.wording_version
            )
            .where(McpNoticeConsent.user_id == alice.user_id)
            .order_by(McpNoticeConsent.id)
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        (True, "settings", V),
        (False, "settings", None),
        (True, "settings", "2099-01"),
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_consent_missing_user_is_refused(db_session) -> None:
    with pytest.raises(NotFoundException):
        await svc.record_mcp_notice_consent(
            db_session, user_id="mn_nobody", value=False, wording_version=None
        )
    await db_session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_consent_parallel_writers_append_one_row(db_session, make_users) -> None:
    carol = await make_users()

    async def _one() -> bool:
        async with _maker(db_session)() as session:
            result = await svc.record_mcp_notice_consent(
                session, user_id=carol.user_id, value=True, wording_version=V
            )
            await session.commit()
            return result.recorded

    outcomes = await asyncio.gather(*(_one() for _ in range(6)))
    assert outcomes.count(True) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_consents_never_cross_purposes(db_session, make_users) -> None:
    """A "yes" to notices is no announcement answer, and the other way round."""
    notices_only = await make_users()
    emails_only = await make_users()
    await _opt(db_session, notices_only, True)
    await announcements.record_announcement_consent(
        db_session,
        user_id=emails_only.user_id,
        value=True,
        source="settings",
        wording_version=announcements.ANNOUNCEMENT_CONSENT_WORDING_VERSION,
    )
    await db_session.commit()

    assert (
        await announcements.current_announcement_consent(db_session, notices_only.user_id) is None
    )
    assert await svc.current_mcp_notice_consent(db_session, emails_only.user_id) is None
    email_rows = (
        await db_session.execute(
            select(func.count())
            .select_from(AnnouncementEmailConsent)
            .where(AnnouncementEmailConsent.user_id == notices_only.user_id)
        )
    ).scalar_one()
    assert email_rows == 0

    # The email opt-in does not make anyone a notice candidate.
    await _create(db_session)
    assert await svc.read_notice_candidates(db_session, emails_only.user_id) == []
    assert len(await svc.read_notice_candidates(db_session, notices_only.user_id)) == 1


# --- writing ------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_writes_one_audit_row_without_the_text(db_session, make_users) -> None:
    await make_users()
    body = "Maintenance on Saturday 02:00 UTC."
    view = await _create(db_session, body=body)

    assert view.state == "active" and view.delivered_count == 0
    assert view.text == body
    audits = await _fresh_audits(db_session, view.id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.action == "mcp_notice.created"
    assert audit.user_email == ACTOR.email and audit.user_id == ACTOR.user_id
    assert audit.old_value_hash is None
    assert audit.new_value_hash == sha256_hex(body)
    assert audit.ip_address == "192.0.2.10" and audit.user_agent == "console/1.0"
    # Exactly what the code writes: the length and the window, nothing else.
    assert audit.user_metadata == {
        "text_chars": len(body),
        "starts_at": view.starts_at.isoformat(),
        "expires_at": view.expires_at.isoformat(),
    }
    # The text is in no column of the audit row (only its hash and length are),
    # read back from the table itself, every column, stringified.
    rows = await _raw_audit_rows(db_session, view.id)
    assert len(rows) == 1
    _assert_text_in_no_column(rows, body)


@pytest.mark.asyncio(loop_scope="session")
async def test_text_is_normalised_and_refusals_name_the_reason(db_session, make_users) -> None:
    await make_users()
    view = await _create(db_session, body="  two   spaces　here ")
    assert view.text == "two spaces here"
    stored = (
        await db_session.execute(select(McpNotice.text).where(McpNotice.id == view.id))
    ).scalar_one()
    assert stored == "two spaces here"

    with pytest.raises(McpNoticeTextRejectedError) as caught:
        await _create(db_session, body="see https://example.com")
    assert caught.value.details["reason"] == "link"
    await db_session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_window_rules(db_session, make_users) -> None:
    await make_users()
    now = await _now(db_session)
    cases = {
        "ends_before_start": {
            "starts_at": now + timedelta(days=2),
            "expires_at": now + timedelta(days=1),
        },
        "already_expired": {
            "starts_at": now - timedelta(seconds=30),
            "expires_at": now - timedelta(seconds=1),
        },
        "starts_in_the_past": {
            "starts_at": now - timedelta(minutes=5),
            "expires_at": now + timedelta(days=1),
        },
        "starts_too_far_ahead": {
            "starts_at": now + timedelta(days=91),
            "expires_at": now + timedelta(days=92),
        },
        "too_long": {"starts_at": None, "expires_at": now + timedelta(days=31)},
    }
    for reason, window in cases.items():
        with pytest.raises(McpNoticeWindowInvalidError) as caught:
            await _create(db_session, **window)
        assert caught.value.details["reason"] == reason
        await db_session.rollback()

    scheduled = await _create(
        db_session, starts_at=now + timedelta(days=90), expires_at=now + timedelta(days=120)
    )
    assert scheduled.state == "scheduled"


@pytest.mark.asyncio(loop_scope="session")
async def test_at_most_three_active_and_scheduled(db_session, make_users) -> None:
    await make_users()
    now = await _now(db_session)
    await _create(db_session)
    await _create(db_session)
    await _create(db_session, starts_at=now + timedelta(days=5), expires_at=now + timedelta(days=6))
    with pytest.raises(McpNoticeActiveLimitError):
        await _create(db_session)
    await db_session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_withdrawn_notices_count_against_the_daily_cap(db_session, make_users) -> None:
    await make_users()
    for _ in range(svc.MCP_NOTICE_MAX_CREATED_PER_DAY):
        view = await _create(db_session)
        await svc.withdraw_notice(db_session, notice_id=view.id, actor=ACTOR)
        await db_session.commit()
    with pytest.raises(McpNoticeDailyLimitError):
        await _create(db_session)
    await db_session.rollback()


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("age", "refused"),
    [
        pytest.param(timedelta(hours=23), True, id="23h_ago_counts"),
        pytest.param(timedelta(hours=25), False, id="25h_ago_does_not"),
    ],
)
async def test_daily_cap_is_a_rolling_24_hour_window(
    db_session, make_users, age: timedelta, refused: bool
) -> None:
    """Owner decision 3: at most 5 CREATED per rolling 24 hours. Rows written
    straight to the table (withdrawn, so the active cap stays out of the way)
    with ``created_at`` 23 h ago fill the cap; the same rows 25 h ago do not.
    A 1-hour window passes the 25 h case but not the 23 h one; a 48-hour
    window the other way round."""
    await make_users()
    now = await _now(db_session)
    created_at = now - age
    for _ in range(svc.MCP_NOTICE_MAX_CREATED_PER_DAY):
        db_session.add(
            McpNotice(
                id=uuid.uuid4(),
                text="Earlier notice.",
                starts_at=created_at,
                expires_at=created_at + timedelta(days=1),
                withdrawn_at=created_at + timedelta(minutes=1),
                created_at=created_at,
            )
        )
    await db_session.commit()

    if refused:
        with pytest.raises(McpNoticeDailyLimitError):
            await _create(db_session)
        await db_session.rollback()
    else:
        view = await _create(db_session)
        assert view.state == "active"


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_creates_respect_the_active_cap(db_session, make_users) -> None:
    await make_users()
    await _create(db_session)
    await _create(db_session)
    now = await _now(db_session)

    async def _one() -> str:
        async with _maker(db_session)() as session:
            try:
                await svc.create_notice(
                    session,
                    raw_text="Concurrent notice.",
                    expires_at=now + timedelta(days=1),
                    starts_at=None,
                    actor=ACTOR,
                )
                # Hold the lock a moment so the other writer has to wait for it.
                await session.execute(text("SELECT pg_sleep(0.2)"))
                await session.commit()
                return "created"
            except McpNoticeActiveLimitError:
                await session.rollback()
                return "refused"

    outcomes = await asyncio.gather(_one(), _one())
    assert sorted(outcomes) == ["created", "refused"]
    active = (
        await db_session.execute(
            select(func.count()).select_from(McpNotice).where(McpNotice.withdrawn_at.is_(None))
        )
    ).scalar_one()
    assert active == 3


@pytest.mark.asyncio(loop_scope="session")
async def test_withdraw_is_idempotent_and_audited_once(db_session, make_users) -> None:
    await make_users()
    view = await _create(db_session)

    first, changed = await svc.withdraw_notice(db_session, notice_id=view.id, actor=ACTOR)
    await db_session.commit()
    again, changed_again = await svc.withdraw_notice(db_session, notice_id=view.id, actor=ACTOR)
    await db_session.commit()

    assert changed is True and changed_again is False
    assert first.state == "ended" and first.withdrawn_at is not None
    assert again.withdrawn_at == first.withdrawn_at
    actions = (
        (
            await db_session.execute(
                select(AuditLog.action)
                .where(AuditLog.resource == f"mcp_notice:{view.id}")
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert actions == ["mcp_notice.created", "mcp_notice.withdrawn"]
    withdrawn = (await _fresh_audits(db_session, view.id))[1]
    assert withdrawn.user_email == ACTOR.email and withdrawn.user_id == ACTOR.user_id
    assert withdrawn.old_value_hash is None
    assert withdrawn.new_value_hash == sha256_hex(view.text)
    assert withdrawn.ip_address == "192.0.2.10" and withdrawn.user_agent == "console/1.0"
    assert withdrawn.user_metadata == {"text_chars": len(view.text)}
    _assert_text_in_no_column(await _raw_audit_rows(db_session, view.id), view.text)

    with pytest.raises(McpNoticeNotFoundError):
        await svc.withdraw_notice(db_session, notice_id=uuid.uuid4(), actor=ACTOR)
    await db_session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_list_filters_by_state_and_counts_deliveries(db_session, make_users) -> None:
    dave = await make_users()
    erin = await make_users()
    now = await _now(db_session)
    live = await _create(db_session)
    later = await _create(
        db_session, starts_at=now + timedelta(days=3), expires_at=now + timedelta(days=4)
    )
    gone = await _raw_notice(
        db_session, start_offset=timedelta(days=-3), end_offset=timedelta(days=-1)
    )
    await _opt(db_session, dave, True)
    await _opt(db_session, erin, True)
    assert await _claim(db_session, dave, [live.id]) == {live.id}
    assert await _claim(db_session, erin, [live.id]) == {live.id}

    everything = await svc.list_notices(db_session)
    by_id = {n.id: n for n in everything}
    assert by_id[live.id].delivered_count == 2
    assert by_id[live.id].state == "active"
    assert by_id[later.id].state == "scheduled"
    assert by_id[gone].state == "ended"
    assert [n.id for n in await svc.list_notices(db_session, state="active")] == [live.id]
    assert [n.id for n in await svc.list_notices(db_session, state="scheduled")] == [later.id]
    assert [n.id for n in await svc.list_notices(db_session, state="ended")] == [gone]
    assert len(await svc.list_notices(db_session, limit=1)) == 1


# --- delivery -----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_candidates_follow_consent_window_and_delivery(db_session, make_users) -> None:
    frank = await make_users()
    never = await make_users()
    now = await _now(db_session)
    late = await _create(db_session, body="Second.", expires_at=now + timedelta(days=3))
    soon = await _create(db_session, body="First.", expires_at=now + timedelta(days=1))
    await _raw_notice(db_session, start_offset=timedelta(days=-3), end_offset=timedelta(days=-1))
    await _raw_notice(
        db_session,
        start_offset=timedelta(days=-1),
        end_offset=timedelta(days=1),
        withdrawn_at=now,
    )
    await _raw_notice(db_session, start_offset=timedelta(days=1), end_offset=timedelta(days=2))

    # Never answered, then turned on, then off.
    assert await svc.read_notice_candidates(db_session, frank.user_id) == []
    await _opt(db_session, frank, True)
    assert [c.id for c in await svc.read_notice_candidates(db_session, frank.user_id)] == [
        soon.id,
        late.id,
    ]
    assert await svc.read_notice_candidates(db_session, never.user_id) == []

    assert await _claim(db_session, frank, [soon.id]) == {soon.id}
    assert [c.id for c in await svc.read_notice_candidates(db_session, frank.user_id)] == [late.id]

    await _opt(db_session, frank, False)
    assert await svc.read_notice_candidates(db_session, frank.user_id) == []


def _erasure_request(user: User, status: str) -> ErasureRequest:
    return ErasureRequest(
        user_id=user.user_id,
        user_email_hash=sha256_hex(user.email),
        initiated_by=user.user_id,
        is_self_service=True,
        reason_code="self_service",
        status=status,
    )


@pytest.mark.parametrize("status", [STATUS_PENDING, STATUS_COOLING_OFF, STATUS_IN_PROGRESS])
@pytest.mark.asyncio(loop_scope="session")
async def test_erasure_under_way_gets_no_notice(db_session, make_users, status: str) -> None:
    """R12: every erasure status that is still under way blocks the read and the claim."""
    gina = await make_users()
    await _opt(db_session, gina, True)
    view = await _create(db_session)
    assert len(await svc.read_notice_candidates(db_session, gina.user_id)) == 1

    db_session.add(_erasure_request(gina, status))
    await db_session.commit()

    assert await svc.read_notice_candidates(db_session, gina.user_id) == []
    assert await _claim(db_session, gina, [view.id]) == set()


@pytest.mark.parametrize("status", [STATUS_COMPLETE, STATUS_FAILED, STATUS_CANCELLED])
@pytest.mark.asyncio(loop_scope="session")
async def test_ended_erasure_request_does_not_block_notices(
    db_session, make_users, status: str
) -> None:
    """Control for R12: a finished request (the account is still here) does not block."""
    gina = await make_users()
    await _opt(db_session, gina, True)
    view = await _create(db_session)

    db_session.add(_erasure_request(gina, status))
    await db_session.commit()

    assert [c.id for c in await svc.read_notice_candidates(db_session, gina.user_id)] == [view.id]
    assert await _claim(db_session, gina, [view.id]) == {view.id}


@pytest.mark.asyncio(loop_scope="session")
async def test_claim_is_once_per_user_under_concurrency(db_session, make_users) -> None:
    hank = await make_users()
    await _opt(db_session, hank, True)
    view = await _create(db_session)

    outcomes = await asyncio.gather(*(_claim(db_session, hank, [view.id]) for _ in range(6)))

    assert sum(len(o) for o in outcomes) == 1
    rows = (
        await db_session.execute(
            select(func.count())
            .select_from(McpNoticeDelivery)
            .where(McpNoticeDelivery.user_id == hank.user_id)
        )
    ).scalar_one()
    assert rows == 1
    assert await _claim(db_session, hank, [view.id]) == set()


@pytest.mark.asyncio(loop_scope="session")
async def test_claim_rechecks_withdraw_opt_out_and_expiry(db_session, make_users) -> None:
    ivan = await make_users()
    jane = await make_users()
    await _opt(db_session, ivan, True)
    await _opt(db_session, jane, True)
    withdrawn = await _create(db_session)
    kept = await _create(db_session)
    expired = await _raw_notice(
        db_session, start_offset=timedelta(days=-3), end_offset=timedelta(seconds=-1)
    )

    await svc.withdraw_notice(db_session, notice_id=withdrawn.id, actor=ACTOR)
    await db_session.commit()
    assert await _claim(db_session, ivan, [withdrawn.id, kept.id, expired]) == {kept.id}

    await _opt(db_session, jane, False)
    assert await _claim(db_session, jane, [kept.id]) == set()


@pytest.mark.asyncio(loop_scope="session")
async def test_claim_for_a_deleted_user_raises_and_writes_nothing(db_session, make_users) -> None:
    kim = await make_users()
    await _opt(db_session, kim, True)
    view = await _create(db_session)
    candidates = await svc.read_notice_candidates(db_session, kim.user_id)
    assert [c.id for c in candidates] == [view.id]

    # The user goes between the read and the claim: no consent row is left,
    # so the claim selects nothing.
    await db_session.execute(delete(User).where(User.user_id == kim.user_id))
    await db_session.commit()
    assert await _claim(db_session, kim, [view.id]) == set()

    # A delivery row for a user that does not exist is refused by the FK.
    async with _maker(db_session)() as session:
        with pytest.raises(IntegrityError):
            session.add(McpNoticeDelivery(user_id=kim.user_id, notice_id=view.id))
            await session.commit()


# A claim blocked by a lock another transaction holds: the users row an
# erasure has locked (the delivery row's FK check needs FOR KEY SHARE on it),
# or the same delivery row inserted by a concurrent session start that has not
# committed yet (ON CONFLICT waits for it). The claim's own ``lock_timeout``
# ends the wait; without it the session start would hang for as long as the
# other transaction runs. The outer ``wait_for`` only keeps a regression from
# hanging the suite.
_LOCK_NOT_AVAILABLE = "55P03"


async def _hold_conflicting_lock(holder: AsyncSession, how: str, user: User, notice_id) -> None:
    if how == "users_row_locked":
        await holder.execute(
            select(User.user_id).where(User.user_id == user.user_id).with_for_update()
        )
    else:
        holder.add(McpNoticeDelivery(user_id=user.user_id, notice_id=notice_id))
        await holder.flush()


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None)
        if state:
            return state
    return None


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("how", ["users_row_locked", "delivery_row_pending"])
async def test_a_claim_behind_a_lock_gives_up_at_the_lock_timeout(
    db_session, make_users, how
) -> None:
    from sqlalchemy.exc import DBAPIError

    mia = await make_users()
    await _opt(db_session, mia, True)
    view = await _create(db_session)

    async with _maker(db_session)() as holder:
        await _hold_conflicting_lock(holder, how, mia, view.id)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(DBAPIError) as caught:
            await asyncio.wait_for(_claim(db_session, mia, [view.id]), timeout=10)
        elapsed = loop.time() - started
        await holder.rollback()

    # lock_timeout (1 s), not statement_timeout (2 s), ended it.
    assert _sqlstate(caught.value) == _LOCK_NOT_AVAILABLE
    assert elapsed < 1.9
    rows = (
        await db_session.execute(
            select(func.count())
            .select_from(McpNoticeDelivery)
            .where(McpNoticeDelivery.user_id == mia.user_id)
        )
    ).scalar_one()
    assert rows == 0


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("how", ["users_row_locked", "delivery_row_pending"])
async def test_claim_notices_behind_a_lock_returns_no_key_in_time(
    db_session, make_users, how
) -> None:
    """The bootstrap step on a real database: the lock timeout is raised by
    the INSERT that reached the server; ``claim_notices`` closes its session,
    returns no block and the next session start can still claim."""
    from mcp_server.tools._notices import NOTICES_PROVENANCE, claim_notices, notice_item

    nina = await make_users()
    await _opt(db_session, nina, True)
    view = await _create(db_session)
    (candidate,) = await svc.read_notice_candidates(db_session, nina.user_id)
    block = {"notices": {"provenance": NOTICES_PROVENANCE, "items": [notice_item(candidate)]}}

    async with _maker(db_session)() as holder:
        await _hold_conflicting_lock(holder, how, nina, view.id)
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await asyncio.wait_for(claim_notices(db_session, nina.user_id, block), timeout=10)
        elapsed = loop.time() - started
        await holder.rollback()

    assert result == {}
    assert elapsed < 3
    # Not used up: the lock is gone, so the next claim delivers it.
    assert await claim_notices(db_session, nina.user_id, block) == block


@pytest.mark.asyncio(loop_scope="session")
async def test_deliveries_and_consents_go_with_the_user(db_session, make_users) -> None:
    lena = await make_users()
    await _opt(db_session, lena, True)
    view = await _create(db_session)
    assert await _claim(db_session, lena, [view.id]) == {view.id}

    await db_session.execute(delete(User).where(User.user_id == lena.user_id))
    await db_session.commit()

    for model in (McpNoticeConsent, McpNoticeDelivery):
        left = (
            await db_session.execute(
                select(func.count()).select_from(model).where(model.user_id == lena.user_id)
            )
        ).scalar_one()
        assert left == 0
    still_there = (
        await db_session.execute(
            select(func.count()).select_from(McpNotice).where(McpNotice.id == view.id)
        )
    ).scalar_one()
    assert still_there == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_account_erasure_removes_consents_and_deliveries(
    db_session, make_users, connector_world
) -> None:
    """The erasure deletes the users row last; the FK cascade takes the
    person's notice answers and delivery rows with it, and the notice stays."""
    target = await connector_world.user("noticed")
    await db_session.commit()
    user = (await db_session.execute(select(User).where(User.user_id == target))).scalar_one()
    await _opt(db_session, user, True)
    view = await _create(db_session)
    assert await _claim(db_session, user, [view.id]) == {view.id}

    from unittest.mock import AsyncMock, patch

    with (
        patch("services.account_erasure_service.delete_user_points", AsyncMock(return_value=0)),
        patch("db.qdrant.list_memory_collections", AsyncMock(return_value=["kagura_memories"])),
        patch("db.qdrant.delete_context_points_everywhere", AsyncMock(return_value={})),
    ):
        await connector_world.erase(target)

    for model in (McpNoticeConsent, McpNoticeDelivery):
        left = (
            await db_session.execute(
                select(func.count()).select_from(model).where(model.user_id == target)
            )
        ).scalar_one()
        assert left == 0
    assert [n.id for n in await svc.list_notices(db_session)] == [view.id]
