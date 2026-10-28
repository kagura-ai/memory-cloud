"""#1969: the capacity purge — clock arithmetic and the countdown notices (mocked DB)."""

import uuid
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.capacity_lock import CapacityLock
from services.capacity_purge import (
    MIN_WARNING_DAYS,
    NOTICE_DAYS,
    NOTICE_KEY_TTL_SECONDS,
    PURGE_AFTER,
    PURGE_MAX_WORKSPACES_PER_RUN,
    SWEEP_HOUR_UTC,
    NoticeCounts,
    PurgeCounts,
    StampCounts,
    SweepCounts,
    mail_provider_configured,
    notice_key,
    notice_stage,
    purge_at,
    purge_due,
    purge_expired_workspaces,
    run_capacity_purge_sweep,
    send_due_purge_notices,
)
from services.context_service import CAPACITY_PURGE_ACTOR, WorkspacePurgeReport


def _on_time(since: datetime, now: datetime | None = None) -> datetime | None:
    """``purge_at`` for a lock whose day-0 mail went out in the stamping run."""
    return purge_at(since, first_notice_at=since, final_notice_at=None, now=now or since)


def test_purge_at_is_thirty_days_after_lock_start() -> None:
    since = datetime(2026, 10, 1, 4, 0)
    assert _on_time(since) == datetime(2026, 10, 31, 4, 0)
    assert PURGE_AFTER == timedelta(days=30)


@pytest.mark.parametrize(
    "since",
    [
        datetime(2026, 10, 1, 4, 0, 5),  # a few seconds into the 04:00 run
        datetime(2026, 10, 1, 0, 30),  # a manual run before the day's sweep
        datetime(2026, 10, 1, 23, 59, 59),  # a manual run late in the day
    ],
)
def test_purge_at_is_the_sweep_that_purges(since) -> None:
    """The date the owner is shown is the 04:00 UTC run on calendar day 30 —
    the run ``purge_due`` first says yes to — not stamp + 30 x 24 hours."""
    deadline = _on_time(since)
    assert deadline == datetime(2026, 10, 31, SWEEP_HOUR_UTC, 0)
    final = deadline - timedelta(days=1)  # the day-29 "1 day left" mail
    assert purge_due(since, deadline, final_notice_at=final, first_notice_at=since)
    assert not purge_due(
        since, deadline - timedelta(days=1), final_notice_at=final, first_notice_at=since
    )
    # Recorded on schedule, the final notice does not move it.
    assert purge_at(since, first_notice_at=since, final_notice_at=final, now=final) == deadline


@pytest.mark.parametrize(
    ("first_age", "purge_day"),
    [
        (0, 30),  # on time
        (23, 30),  # first mail on day 23: exactly a week before day 30
        (24, 31),  # one day later: the week moves the purge by a day
        (25, 32),
        (29, 36),  # the first mail is the day-29 one
        (35, 42),  # past day 30: a week after it, never a past date
    ],
)
def test_purge_at_waits_a_week_after_a_late_first_notice(first_age, purge_day) -> None:
    """#1971: the first notice must precede the purge by ``MIN_WARNING_DAYS``,
    so a stamp more than 23 days old at its first mail is purged later than
    stamp + 30 days — and that later date is the one shown."""
    since = datetime(2026, 10, 1, 4, 0, 5)
    first = since + timedelta(days=first_age)
    final = first if first_age >= 29 else None
    deadline = purge_at(since, first_notice_at=first, final_notice_at=final, now=first)
    assert deadline == datetime(2026, 10, 1, SWEEP_HOUR_UTC) + timedelta(days=purge_day)


def test_purge_at_projects_unsent_notices_onto_the_next_run() -> None:
    """No notice yet: the first goes out on the next run (today's if it has not
    run yet), the final one on the day-29 run or the next one if that is later."""
    since = datetime(2026, 9, 1, 4, 0, 5)  # 40 days old, never notified
    before_run = datetime(2026, 10, 11, 3, 0)
    after_run = datetime(2026, 10, 11, 12, 0)
    kwargs = {"first_notice_at": None, "final_notice_at": None}
    assert purge_at(since, now=before_run, **kwargs) == datetime(2026, 10, 18, 4, 0)
    assert purge_at(since, now=after_run, **kwargs) == datetime(2026, 10, 19, 4, 0)


@pytest.mark.parametrize(
    ("now", "shown"),
    [
        (datetime(2026, 10, 31, 4, 0, 20), datetime(2026, 10, 31, 4, 0)),  # run in progress
        (datetime(2026, 10, 31, 4, 59), datetime(2026, 10, 31, 4, 0)),
        (datetime(2026, 10, 31, 5, 0), datetime(2026, 11, 1, 4, 0)),  # run over, not purged
    ],
)
def test_purge_at_counts_the_run_in_progress(now, shown) -> None:
    """A read during the day-30 run names that run, not tomorrow's."""
    since = datetime(2026, 10, 1, 4, 0, 5)
    final = datetime(2026, 10, 30, 4, 0, 9)
    assert purge_at(since, first_notice_at=since, final_notice_at=final, now=now) == shown


def test_purge_at_is_never_before_the_next_run() -> None:
    """Warned and expired, but not purged yet (the per-run cap): the next run."""
    since = datetime(2026, 9, 1, 4, 0, 5)
    deadline = purge_at(
        since,
        first_notice_at=since,
        final_notice_at=datetime(2026, 9, 30, 4, 0, 5),
        now=datetime(2026, 10, 11, 12, 0),
    )
    assert deadline == datetime(2026, 10, 12, 4, 0)


def _simulate(start_day: int, failures: frozenset[int] = frozenset()):
    """Daily runs of the notice pass then the purge decision, as in
    ``run_capacity_purge_sweep``: the notices are on from ``start_day`` and the
    mails of the ``failures`` days fail (claim released, nothing recorded).
    Yields ``(since, day, (first, final) before the run, the same after it, due)``."""
    since = datetime(2026, 9, 1, 4, 0, 5)
    first = final = None
    claimed: set[int] = set()
    for day in range(0, 80):
        run = datetime(2026, 9, 1, 4, 0, 40) + timedelta(days=day)
        before = (first, final)
        stage = notice_stage(
            day, days_since_first_notice=(run.date() - first.date()).days if first else 0
        )
        if day >= start_day and stage is not None and stage not in claimed:
            if day not in failures:
                claimed.add(stage)
                first = first or run
                if stage == NOTICE_DAYS[-1]:
                    final = run
        due = purge_due(since, run, final_notice_at=final, first_notice_at=first)
        yield since, day, before, (first, final), due
        if due:
            return


@pytest.mark.parametrize("start_day", [0, 1, 7, 20, 23, 24, 26, 28, 29, 30, 31, 45])
def test_purge_at_is_the_first_run_purge_due_says_yes_to(start_day) -> None:
    """Property (#1971): once the notices are on, ``purge_at`` asked at any time
    names the very run on which ``purge_due`` first says yes — no earlier
    run is due, and that one is."""
    runs = list(_simulate(start_day))
    since, purge_day, *_, due = runs[-1]
    assert due
    expected = datetime.combine(since.date() + timedelta(days=purge_day), time(SWEEP_HOUR_UTC))
    for _, day, before, after, _ in runs:
        if day < start_day:
            continue
        first, final = before
        for hour, minute in ((3, 0), (4, 0), (4, 30)):  # before or during the day's run
            asked = datetime(2026, 9, 1, hour, minute) + timedelta(days=day)
            assert (
                purge_at(since, first_notice_at=first, final_notice_at=final, now=asked) == expected
            ), (day, hour, minute)
        if day < purge_day:
            asked_after = datetime(2026, 9, 1, 12, 0) + timedelta(days=day)
            first, final = after
            assert (
                purge_at(since, first_notice_at=first, final_notice_at=final, now=asked_after)
                == expected
            ), day


@pytest.mark.parametrize("failures", [frozenset({0}), frozenset({29}), frozenset({29, 30, 31})])
def test_purge_at_is_a_lower_bound_when_a_mail_fails(failures) -> None:
    """A failed mail moves the purge later; the date shown before the failure
    is never after the run that really purges."""
    runs = list(_simulate(0, failures))
    since, purge_day, *_ = runs[-1]
    real = datetime.combine(since.date() + timedelta(days=purge_day), time(SWEEP_HOUR_UTC))
    for _, day, before, _, _ in runs:
        first, final = before
        shown = purge_at(
            since,
            first_notice_at=first,
            final_notice_at=final,
            now=datetime(2026, 9, 1, 3, 0) + timedelta(days=day),
        )
        assert shown is not None and shown <= real


@pytest.mark.parametrize(
    ("final_notice_at", "due"),
    [
        (None, False),  # never warned: never deleted
        (datetime(2026, 10, 31, 4, 0, 3), False),  # warned this very run
        (datetime(2026, 10, 30, 23, 59), True),  # warned the previous calendar day
    ],
)
def test_purge_needs_the_final_notice_a_day_earlier(final_notice_at, due) -> None:
    since = datetime(2026, 10, 1, 4, 0, 5)
    assert (
        purge_due(
            since,
            datetime(2026, 10, 31, 4, 0),
            final_notice_at=final_notice_at,
            first_notice_at=since,
        )
        is due
    )


@pytest.mark.parametrize(
    ("first_notice_at", "due"),
    [
        (None, False),  # no first notice recorded: never deleted
        (datetime(2026, 10, 28, 4, 0, 5), False),  # notices started 3 days ago
        (datetime(2026, 10, 25, 4, 0, 5), False),  # 6 days: one short
        (datetime(2026, 10, 24, 4, 0, 5), True),  # 7 calendar days
    ],
)
def test_purge_needs_the_first_notice_a_week_earlier(first_notice_at, due) -> None:
    """A late-enabled notice flag must not turn into a one-day warning."""
    assert MIN_WARNING_DAYS == 7
    since = datetime(2026, 9, 1, 4, 0, 5)
    now = datetime(2026, 10, 31, 4, 0)
    final = datetime(2026, 10, 30, 4, 0, 5)
    assert purge_due(since, now, final_notice_at=final, first_notice_at=first_notice_at) is due


def test_purge_at_none_when_not_locked() -> None:
    assert (
        purge_at(None, first_notice_at=None, final_notice_at=None, now=datetime(2026, 10, 1))
        is None
    )


def test_notice_days_are_the_policy_series() -> None:
    assert NOTICE_DAYS == (0, 7, 14, 23, 27, 29)


# ---------------------------------------------------------------------------
# Countdown notices (stubbed DB rows, fake Redis, fake e-mail)
# ---------------------------------------------------------------------------

_LOCK = CapacityLock(
    memory_count=12,
    memory_limit=10,
    over_memories=2,
    used_bytes=0,
    storage_limit_bytes=100,
    over_bytes=0,
    cleanup_url="https://app.example.test/workspace/settings/plan",
)


class _Redis:
    def __init__(self) -> None:
        self.keys: dict[str, int | None] = {}
        self.deleted: list[str] = []

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = ex
        return True

    async def delete(self, key):
        self.keys.pop(key, None)
        self.deleted.append(key)


def _ws(since: datetime | None, *, plan: str = "free"):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "Research"
    ws.owner_user_id = "owner-1"
    ws.plan_name = plan
    ws.entitlement_source = "external_billing"
    ws.capacity_locked_since = since
    ws.capacity_purge_first_notice_at = None
    ws.capacity_purge_final_notice_at = None
    return ws


def _db(workspaces, *, owner=("owner@example.test", "en")):
    """First ``execute`` → the stamped workspaces (``.scalars().all()``); each
    later one → the owner's ``(email, locale)`` row (``.one_or_none()``)."""
    db = MagicMock()
    ws_result = MagicMock()
    ws_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=workspaces)))
    owner_result = MagicMock()
    owner_result.one_or_none = MagicMock(return_value=owner)
    db.execute = AsyncMock(side_effect=[ws_result] + [owner_result] * len(workspaces))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _email(**kwargs):
    return MagicMock(send_capacity_purge_notice=AsyncMock(**kwargs))


def _locked(lock=_LOCK):
    return patch("services.capacity_purge.capacity_lock_state", AsyncMock(return_value=lock))


@pytest.mark.parametrize(
    ("elapsed", "stage"),
    [
        (-1, None),
        (0, 0),
        (3, 0),
        (7, 7),
        (13, 7),
        (14, 14),
        (23, 23),
        (26, 23),
        (27, 27),
        (28, 27),
        (29, 29),
        (45, 29),
    ],
)
def test_notice_stage_is_latest_due(elapsed, stage) -> None:
    assert notice_stage(elapsed) == stage


def test_notice_key_includes_lock_start_and_stage() -> None:
    ws = uuid.uuid4()
    since = datetime(2026, 10, 1, 4, 0, 0)
    assert notice_key(ws, since, 7) == f"capacity_purge_notice:{ws}:2026-10-01T04:00:00:7"


@pytest.mark.asyncio
async def test_sends_day_zero_notice_once() -> None:
    now = datetime(2026, 10, 1, 4, 0)
    ws = _ws(now)
    redis, email = _Redis(), _email(return_value=True)
    with _locked():
        counts = await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
    assert (counts.considered, counts.sent, counts.already_sent) == (1, 1, 0)
    kwargs = email.send_capacity_purge_notice.call_args.kwargs
    assert kwargs["days_left"] == 30 and kwargs["locale"] == "en"
    assert kwargs["to_email"] == "owner@example.test"
    assert kwargs["purge_at"] == now + timedelta(days=30)
    assert kwargs["purge_at"] == _on_time(ws.capacity_locked_since)
    assert kwargs["over_memories"] == 2 and kwargs["over_bytes"] == 0
    assert kwargs["contexts_url"].endswith("/workspace/contexts")
    assert redis.keys[notice_key(ws.id, now, 0)] == NOTICE_KEY_TTL_SECONDS

    with _locked():
        counts = await send_due_purge_notices(
            _db([ws]), now=now + timedelta(hours=1), redis=redis, email=email
        )
    assert (counts.sent, counts.already_sent) == (0, 1)
    assert email.send_capacity_purge_notice.await_count == 1


@pytest.mark.asyncio
async def test_owner_locale_is_passed_through() -> None:
    now = datetime(2026, 10, 1, 4, 0)
    ws = _ws(now)
    email = _email(return_value=True)
    with _locked():
        await send_due_purge_notices(
            _db([ws], owner=("owner@example.test", "ja")), now=now, redis=_Redis(), email=email
        )
    assert email.send_capacity_purge_notice.call_args.kwargs["locale"] == "ja"


@pytest.mark.asyncio
async def test_stage_counts_calendar_days_not_elapsed_time() -> None:
    """The stamp landed a few seconds into the 04:00 run; the day-7 run starts at
    04:00:00 sharp. That is still day 7 with 23 days left — not day 6 (a day late)."""
    since = datetime(2026, 10, 1, 4, 0, 30)
    ws = _ws(since)
    redis, email = _Redis(), _email(return_value=True)
    redis.keys[notice_key(ws.id, since, 0)] = 1
    with _locked():
        counts = await send_due_purge_notices(
            _db([ws]), now=datetime(2026, 10, 8, 4, 0, 0), redis=redis, email=email
        )
    assert counts.sent == 1
    assert email.send_capacity_purge_notice.call_args.kwargs["days_left"] == 23
    # The mail names the run that purges: 04:00 sharp, not 04:00:30.
    assert email.send_capacity_purge_notice.call_args.kwargs["purge_at"] == datetime(
        2026, 10, 31, 4, 0, 0
    )
    assert notice_key(ws.id, since, 7) in redis.keys


@pytest.mark.asyncio
async def test_last_notice_says_one_day_left() -> None:
    since = datetime(2026, 10, 1, 4, 0, 30)
    ws = _ws(since)
    ws.capacity_purge_first_notice_at = since  # the day-0 mail
    email = _email(return_value=True)
    with _locked():
        await send_due_purge_notices(
            _db([ws]), now=datetime(2026, 10, 30, 4, 0, 0), redis=_Redis(), email=email
        )
    assert email.send_capacity_purge_notice.call_args.kwargs["days_left"] == 1


@pytest.mark.parametrize(
    ("age", "days_left"),
    [
        (25, 7),  # stage 23 mail on day 25: purge on day 32
        (29, 7),  # first mail is the day-29 one
        (35, 7),  # stamp past day 30: a week from now, not a past date with 0 left
    ],
)
@pytest.mark.asyncio
async def test_late_first_notice_names_the_real_purge_date(age, days_left) -> None:
    """#1971: notices switched on when the stamp is already old — the mail names
    the run that will purge (a week after this first mail), not stamp + 30."""
    since = datetime(2026, 9, 1, 4, 0, 30)
    now = datetime(2026, 9, 1, 4, 0, 50) + timedelta(days=age)
    ws = _ws(since)
    email = _email(return_value=True)
    with _locked():
        await send_due_purge_notices(_db([ws]), now=now, redis=_Redis(), email=email)
    kwargs = email.send_capacity_purge_notice.call_args.kwargs
    assert kwargs["days_left"] == days_left
    assert kwargs["purge_at"] == datetime.combine(now.date() + timedelta(days=7), time(4))
    assert kwargs["purge_at"] == purge_at(
        since,
        first_notice_at=ws.capacity_purge_first_notice_at,
        final_notice_at=ws.capacity_purge_final_notice_at,
        now=now,
    )


@pytest.mark.parametrize("start_day", [24, 25, 29, 35, 60])
@pytest.mark.asyncio
async def test_late_start_sends_seven_three_and_one_day_notices(start_day) -> None:
    """#2321: the final mail must actually warn of tomorrow's deletion."""
    since = datetime(2026, 9, 1, 4, 0, 5)
    ws, redis = _ws(since), _Redis()
    email = _email(return_value=True)
    notices = []
    for day in range(start_day, start_day + 8):
        now = since + timedelta(days=day)
        email.send_capacity_purge_notice.reset_mock()
        with _locked():
            await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
        if email.send_capacity_purge_notice.await_count:
            args = email.send_capacity_purge_notice.call_args.kwargs
            notices.append((day, args["days_left"]))
            assert args["purge_at"] == datetime.combine(
                since.date() + timedelta(days=start_day + 7), time(4)
            )
        assert purge_due(
            since,
            now,
            first_notice_at=ws.capacity_purge_first_notice_at,
            final_notice_at=ws.capacity_purge_final_notice_at,
        ) is (day == start_day + 7)
    assert notices == [(start_day, 7), (start_day + 4, 3), (start_day + 6, 1)]


@pytest.mark.asyncio
async def test_legacy_early_final_notice_is_replaced_before_purge() -> None:
    """An old stage-29 claim must not suppress the corrected final warning."""
    since = datetime(2026, 9, 1, 4)
    first = since + timedelta(days=35)
    ws, redis = _ws(since), _Redis()
    ws.capacity_purge_first_notice_at = ws.capacity_purge_final_notice_at = first
    redis.keys[notice_key(ws.id, since, 29)] = 1
    now = first + timedelta(days=7)
    assert not purge_due(since, now, first_notice_at=first, final_notice_at=first)
    assert purge_at(since, now=now, first_notice_at=first, final_notice_at=first) == (
        now + timedelta(days=1)
    )
    email = _email(return_value=True)
    with _locked():
        await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
        await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
    email.send_capacity_purge_notice.assert_awaited_once()
    assert email.send_capacity_purge_notice.call_args.kwargs["days_left"] == 1
    assert ws.capacity_purge_final_notice_at == now
    assert not purge_due(since, now, first_notice_at=first, final_notice_at=now)
    assert purge_due(since, now + timedelta(days=1), first_notice_at=first, final_notice_at=now)


@pytest.mark.parametrize(
    ("send", "stamped"),
    [
        (AsyncMock(return_value=True), True),
        (AsyncMock(side_effect=TimeoutError()), True),  # may have been delivered
        (AsyncMock(return_value=False), False),
        (AsyncMock(side_effect=RuntimeError("smtp")), False),
    ],
)
@pytest.mark.asyncio
async def test_final_notice_is_recorded_on_the_workspace(send, stamped) -> None:
    """The day-29 mail is what lets the purge run: sent (or possibly sent)
    stamps ``capacity_purge_final_notice_at`` with the run's clock and commits."""
    since = datetime(2026, 10, 1, 4, 0, 30)
    now = datetime(2026, 10, 30, 4, 0, 0)
    ws = _ws(since)
    ws.capacity_purge_first_notice_at = since
    db = _db([ws])
    with _locked():
        await send_due_purge_notices(
            db, now=now, redis=_Redis(), email=MagicMock(send_capacity_purge_notice=send)
        )
    assert ws.capacity_purge_final_notice_at == (now if stamped else None)
    # Failed delivery never records a notice that could authorize deletion.
    assert db.commit.await_count == (1 if stamped else 0)


@pytest.mark.asyncio
async def test_earlier_notices_do_not_record_the_final_notice() -> None:
    since = datetime(2026, 10, 1, 4, 0, 30)
    ws = _ws(since)
    ws.capacity_purge_first_notice_at = since  # the day-0 mail
    db = _db([ws])
    email = _email(return_value=True)
    with _locked():
        counts = await send_due_purge_notices(
            db, now=datetime(2026, 10, 28, 4, 0, 0), redis=_Redis(), email=email
        )
    assert counts.sent == 1
    assert email.send_capacity_purge_notice.call_args.kwargs["days_left"] == 3  # stage 27
    assert ws.capacity_purge_final_notice_at is None
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("send", "recorded"),
    [
        (AsyncMock(return_value=True), True),
        (AsyncMock(side_effect=TimeoutError()), True),  # may have been delivered
        (AsyncMock(return_value=False), False),
    ],
)
@pytest.mark.asyncio
async def test_first_notice_is_recorded_once_per_lock(send, recorded) -> None:
    """The first mail of a lock (sent or possibly sent) records
    ``capacity_purge_first_notice_at``; a later mail never moves it."""
    since = datetime(2026, 10, 1, 4, 0, 30)
    day0 = datetime(2026, 10, 1, 4, 0, 31)
    ws = _ws(since)
    redis = _Redis()
    db = _db([ws])
    with _locked():
        await send_due_purge_notices(
            db, now=day0, redis=redis, email=MagicMock(send_capacity_purge_notice=send)
        )
    assert ws.capacity_purge_first_notice_at == (day0 if recorded else None)
    assert db.commit.await_count == (1 if recorded else 0)
    if not recorded:
        return
    db = _db([ws])
    with _locked():
        counts = await send_due_purge_notices(
            db, now=datetime(2026, 10, 8, 4, 0), redis=redis, email=_email(return_value=True)
        )
    assert counts.sent == 1
    assert ws.capacity_purge_first_notice_at == day0  # unchanged by the day-7 mail
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_notice_is_committed_only_after_delivery() -> None:
    """A delivery attempt cannot prematurely authorize deletion."""
    since = datetime(2026, 10, 1, 4, 0, 30)
    now = datetime(2026, 10, 30, 4, 0, 0)
    ws = _ws(since)
    ws.capacity_purge_first_notice_at = since
    db = _db([ws])
    seen: list[tuple] = []

    async def send(**_kwargs):
        seen.append(
            (
                ws.capacity_purge_first_notice_at,
                ws.capacity_purge_final_notice_at,
                db.commit.await_count,
            )
        )
        return True

    with _locked():
        counts = await send_due_purge_notices(
            db, now=now, redis=_Redis(), email=MagicMock(send_capacity_purge_notice=send)
        )
    assert counts.sent == 1
    assert seen == [(since, None, 0)]
    assert ws.capacity_purge_final_notice_at == now
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_notice_persist_failure_releases_claim_and_moves_on() -> None:
    """A delivered mail's record fails: release its claim for retry and move on."""
    import structlog

    since = datetime(2026, 10, 1, 4, 0, 30)
    now = datetime(2026, 10, 30, 4, 0, 0)
    first, second = _ws(since), _ws(since)
    first.capacity_purge_first_notice_at = second.capacity_purge_first_notice_at = since
    db = _db([first, second])
    db.commit = AsyncMock(side_effect=[RuntimeError("db gone"), None])
    redis = _Redis()
    email = _email(return_value=True)
    with _locked(), structlog.testing.capture_logs() as logs:
        counts = await send_due_purge_notices(db, now=now, redis=redis, email=email)
    assert (counts.sent, counts.failed) == (1, 1)
    assert email.send_capacity_purge_notice.await_count == 2
    db.rollback.assert_awaited_once()
    assert notice_key(first.id, since, 29) not in redis.keys  # released: retried tomorrow
    assert notice_key(first.id, since, 29) in redis.deleted
    assert notice_key(second.id, since, 29) in redis.keys
    db.refresh.assert_awaited_once_with(second)  # the rollback expired it
    failed = [e for e in logs if e["event"] == "capacity_purge_notice_persist_failed"]
    assert [e["workspace_id"] for e in failed] == [str(first.id)]


@pytest.mark.parametrize(
    "send", [AsyncMock(return_value=False), AsyncMock(side_effect=RuntimeError("smtp"))]
)
@pytest.mark.asyncio
async def test_failed_delivery_never_records_the_notice(send) -> None:
    """A hard failure leaves both columns unchanged without needing a DB revert."""
    since = datetime(2026, 10, 1, 4, 0, 30)
    now = datetime(2026, 10, 30, 4, 0, 0)
    ws = _ws(since)
    ws.capacity_purge_first_notice_at = since
    db = _db([ws])
    redis = _Redis()
    with _locked():
        counts = await send_due_purge_notices(
            db, now=now, redis=redis, email=MagicMock(send_capacity_purge_notice=send)
        )
    assert counts.failed == 1
    assert ws.capacity_purge_first_notice_at == since
    assert ws.capacity_purge_final_notice_at is None
    db.commit.assert_not_awaited()
    assert notice_key(ws.id, since, 29) not in redis.keys


@pytest.mark.asyncio
async def test_catch_up_sends_only_latest_stage() -> None:
    """The sweep was down from day 5 to day 15: one mail (stage 14), not 7 and 14."""
    since = datetime(2026, 10, 1, 4, 0)
    ws = _ws(since)
    redis, email = _Redis(), _email(return_value=True)
    redis.keys[notice_key(ws.id, since, 0)] = 1  # day 0 already went out
    with _locked():
        counts = await send_due_purge_notices(
            _db([ws]), now=since + timedelta(days=15), redis=redis, email=email
        )
    assert counts.sent == 1
    assert email.send_capacity_purge_notice.await_count == 1
    assert email.send_capacity_purge_notice.call_args.kwargs["days_left"] == 15
    assert notice_key(ws.id, since, 7) not in redis.keys
    assert notice_key(ws.id, since, 14) in redis.keys


@pytest.mark.asyncio
async def test_notice_keys_change_with_lock_start() -> None:
    """Cleared on day 29 and stamped again: a fresh clock, day-0 mail, no '1 day left'."""
    first = datetime(2026, 10, 1, 4, 0)
    second = datetime(2026, 11, 2, 4, 0)
    ws = _ws(second)
    redis, email = _Redis(), _email(return_value=True)
    redis.keys[notice_key(ws.id, first, 29)] = 1
    with _locked():
        counts = await send_due_purge_notices(_db([ws]), now=second, redis=redis, email=email)
    assert counts.sent == 1
    assert email.send_capacity_purge_notice.call_args.kwargs["days_left"] == 30
    assert notice_key(ws.id, second, 0) in redis.keys


@pytest.mark.asyncio
async def test_failed_delivery_releases_claim() -> None:
    now = datetime(2026, 10, 1, 4, 0)
    ws = _ws(now)
    redis, email = _Redis(), _email(return_value=False)
    with _locked():
        counts = await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
    assert (counts.sent, counts.failed) == (0, 1)
    assert notice_key(ws.id, now, 0) not in redis.keys


@pytest.mark.asyncio
async def test_raising_implementation_releases_claim() -> None:
    now = datetime(2026, 10, 1, 4, 0)
    ws = _ws(now)
    redis, email = _Redis(), _email(side_effect=RuntimeError("boom"))
    with _locked():
        counts = await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
    assert counts.failed == 1
    assert notice_key(ws.id, now, 0) not in redis.keys


@pytest.mark.asyncio
async def test_uncertain_delivery_keeps_claim() -> None:
    now = datetime(2026, 10, 1, 4, 0)
    ws = _ws(now)
    redis, email = _Redis(), _email(side_effect=TimeoutError())
    with _locked():
        counts = await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
    assert counts.sent == 1
    assert notice_key(ws.id, now, 0) in redis.keys


@pytest.mark.asyncio
async def test_dedup_unavailable_fails_closed() -> None:
    now = datetime(2026, 10, 1, 4, 0)
    ws = _ws(now)
    redis, email = _Redis(), _email(return_value=True)
    redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
    with _locked():
        counts = await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
    assert (counts.sent, counts.failed) == (0, 1)
    email.send_capacity_purge_notice.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_workspace_that_is_within_capacity_now() -> None:
    """Stamped but already under the cap (cleaned up since the stamp run): no mail."""
    now = datetime(2026, 10, 8, 4, 0)
    ws = _ws(now - timedelta(days=7))
    redis, email = _Redis(), _email(return_value=True)
    with _locked(None):
        counts = await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
    assert (counts.sent, counts.skipped) == (0, 1)
    email.send_capacity_purge_notice.assert_not_awaited()
    assert redis.keys == {}


@pytest.mark.asyncio
async def test_skips_workspace_that_is_no_longer_a_candidate() -> None:
    """Re-subscribed since the stamp run: no lock evaluation, no mail."""
    now = datetime(2026, 10, 8, 4, 0)
    ws = _ws(now - timedelta(days=7), plan="pro")
    redis, email = _Redis(), _email(return_value=True)
    state = AsyncMock(return_value=_LOCK)
    with patch("services.capacity_purge.capacity_lock_state", state):
        counts = await send_due_purge_notices(_db([ws]), now=now, redis=redis, email=email)
    assert (counts.sent, counts.skipped) == (0, 1)
    state.assert_not_awaited()
    email.send_capacity_purge_notice.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_workspace_without_owner_email() -> None:
    now = datetime(2026, 10, 1, 4, 0)
    ws = _ws(now)
    redis, email = _Redis(), _email(return_value=True)
    with _locked():
        counts = await send_due_purge_notices(
            _db([ws], owner=None), now=now, redis=redis, email=email
        )
    assert (counts.sent, counts.skipped) == (0, 1)
    assert redis.keys == {}


# ---------------------------------------------------------------------------
# Purge (stubbed DB, ContextService and lock state)
# ---------------------------------------------------------------------------

_REPORT = WorkspacePurgeReport(
    contexts_deleted=2,
    contexts_emptied=1,
    memories_deleted=12,
    files_deleted=3,
    bytes_released=300,
)


def _purge_ws(
    since: datetime | None,
    *,
    plan: str = "free",
    final_notice_at: datetime | None = datetime(2026, 10, 30, 4, 0, 2),
    first_notice_at: datetime | None = datetime(2026, 10, 1, 4, 0, 2),
):
    """A stamped workspace; by default warned by the day-0 run and by the
    day-29 run before ``_NOW``."""
    ws = _ws(since, plan=plan)
    ws.deleted_at = None
    ws.capacity_purge_first_notice_at = first_notice_at
    ws.capacity_purge_final_notice_at = final_notice_at
    return ws


def _purge_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


def _purge_patches(workspaces, *, lock=_LOCK, purge=None, contact=("owner@example.test", "ja")):
    service = MagicMock()
    service.purge_workspace_data = purge or AsyncMock(return_value=_REPORT)
    return (
        patch(
            "services.capacity_purge._stamped_workspaces", AsyncMock(return_value=list(workspaces))
        ),
        patch("services.capacity_purge.capacity_lock_state", AsyncMock(return_value=lock)),
        patch("services.capacity_purge._owner_contact", AsyncMock(return_value=contact)),
        patch("services.capacity_purge.ContextService", MagicMock(return_value=service)),
        service,
    )


async def _run_purge(workspaces, *, now, email=None, db=None, cleaned_up=False, **kwargs):
    stamped, locked, owner, ctx_service, service = _purge_patches(workspaces, **kwargs)
    email = email or MagicMock(send_capacity_purge_complete=AsyncMock(return_value=True))
    db = db or _purge_db()
    if not isinstance(cleaned_up, AsyncMock):
        cleaned_up = AsyncMock(return_value=cleaned_up)
    cleanup = patch("services.capacity_purge._owner_cleaned_up", cleaned_up)
    with stamped, locked, owner, ctx_service, cleanup:
        counts = await purge_expired_workspaces(db, now=now, email=email)
    return counts, service, email, db


_NOW = datetime(2026, 10, 31, 4, 0, 0)


@pytest.mark.asyncio
async def test_purge_is_due_on_calendar_day_thirty() -> None:
    """Stamped a few seconds into the day-0 run; the day-30 run starts at 04:00:00
    sharp — 29 days 23:59:55 of elapsed time, but calendar day 30: due."""
    ws = _purge_ws(datetime(2026, 10, 1, 4, 0, 5))
    counts, service, _, _ = await _run_purge([ws], now=_NOW)
    assert (counts.due, counts.purged) == (1, 1)
    service.purge_workspace_data.assert_awaited_once()
    assert service.purge_workspace_data.call_args.args == (ws.id,)
    assert service.purge_workspace_data.call_args.kwargs["actor"] == CAPACITY_PURGE_ACTOR


@pytest.mark.parametrize(
    "final_notice_at",
    [None, datetime(2026, 10, 31, 4, 0, 1)],  # never warned / warned this run
)
@pytest.mark.asyncio
async def test_purge_waits_for_the_final_notice(final_notice_at) -> None:
    """Day 30 or later but no day-29 mail a calendar day earlier: nothing is
    deleted, the workspace stays stamped and is counted as awaiting notice."""
    import structlog

    ws = _purge_ws(datetime(2026, 9, 1, 4, 0), final_notice_at=final_notice_at)
    with structlog.testing.capture_logs() as logs:
        counts, service, email, _ = await _run_purge([ws], now=_NOW)
    assert (counts.due, counts.awaiting_notice, counts.purged) == (1, 1, 0)
    service.purge_workspace_data.assert_not_awaited()
    email.send_capacity_purge_complete.assert_not_awaited()
    assert ws.capacity_locked_since is not None
    waiting = [e for e in logs if e["event"] == "capacity_purge_awaiting_final_notice"]
    assert [e["workspace_id"] for e in waiting] == [str(ws.id)]


@pytest.mark.parametrize(("first_notice_days_ago", "purged"), [(3, False), (7, True)])
@pytest.mark.asyncio
async def test_purge_needs_a_first_notice_a_week_before(first_notice_days_ago, purged) -> None:
    """Day 30 and the final notice went out the previous day, but the notices
    started only ``first_notice_days_ago`` days ago (flag turned on late):
    deleted only once the first warning is a week old."""
    ws = _purge_ws(
        datetime(2026, 9, 25, 4, 0),
        first_notice_at=_NOW - timedelta(days=first_notice_days_ago),
    )
    counts, service, _, _ = await _run_purge([ws], now=_NOW)
    assert counts.purged == int(purged)
    assert counts.awaiting_notice == int(not purged)
    assert service.purge_workspace_data.await_count == int(purged)
    assert (ws.capacity_locked_since is None) is purged


@pytest.mark.asyncio
async def test_awaiting_notice_does_not_take_a_purge_slot() -> None:
    """Workspaces still waiting for their final notice do not crowd warned ones
    out of the per-run cap."""
    waiting = [
        _purge_ws(datetime(2026, 8, 1 + i, 4, 0), final_notice_at=None)
        for i in range(PURGE_MAX_WORKSPACES_PER_RUN)
    ]
    warned = _purge_ws(datetime(2026, 9, 1, 4, 0))
    counts, service, _, _ = await _run_purge([*waiting, warned], now=_NOW)
    assert (counts.awaiting_notice, counts.purged) == (PURGE_MAX_WORKSPACES_PER_RUN, 1)
    assert service.purge_workspace_data.call_args.args == (warned.id,)


@pytest.mark.asyncio
async def test_purge_clears_the_final_notice_with_the_stamp() -> None:
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    counts, _, _, _ = await _run_purge([ws], now=_NOW)
    assert counts.purged == 1
    assert (ws.capacity_locked_since, ws.capacity_purge_final_notice_at) == (None, None)
    assert ws.capacity_purge_first_notice_at is None


@pytest.mark.asyncio
async def test_purge_skip_clears_the_final_notice_with_the_stamp() -> None:
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    counts, _, _, _ = await _run_purge([ws], now=_NOW, lock=None)
    assert counts.skipped_within == 1
    assert (ws.capacity_locked_since, ws.capacity_purge_final_notice_at) == (None, None)
    assert ws.capacity_purge_first_notice_at is None


@pytest.mark.asyncio
async def test_purge_is_not_due_on_calendar_day_twenty_nine() -> None:
    """29 days and 23 hours of elapsed time, but calendar day 29: not due."""
    ws = _purge_ws(datetime(2026, 10, 2, 0, 30, 0))
    counts, service, email, _ = await _run_purge([ws], now=datetime(2026, 10, 31, 23, 30, 0))
    assert counts == PurgeCounts(due=0, purged=0, skipped_within=0, failed=0, aborted=0)
    service.purge_workspace_data.assert_not_awaited()
    email.send_capacity_purge_complete.assert_not_awaited()
    assert ws.capacity_locked_since is not None


@pytest.mark.asyncio
async def test_purge_caps_each_run_oldest_stamp_first() -> None:
    """7 due: the 5 oldest go this run, the 2 newest stay stamped; ``due`` says 7."""
    stamps = [datetime(2026, 9, 1 + i, 4, 0) for i in range(7)]
    workspaces = [_purge_ws(s) for s in reversed(stamps)]  # listed newest first
    by_id = {ws.id: ws for ws in workspaces}
    counts, service, _, _ = await _run_purge(workspaces, now=_NOW)
    assert PURGE_MAX_WORKSPACES_PER_RUN == 5
    assert (counts.due, counts.purged) == (7, 5)
    purged = [by_id[c.args[0]] for c in service.purge_workspace_data.call_args_list]
    assert [w for w in workspaces if w not in purged] == workspaces[:2]  # the two newest
    assert all(w.capacity_locked_since is None for w in purged)
    assert all(w.capacity_locked_since is not None for w in workspaces[:2])


@pytest.mark.asyncio
async def test_completion_mail_and_audit_row() -> None:
    since = datetime(2026, 9, 30, 4, 0)
    ws = _purge_ws(since)
    counts, _, email, db = await _run_purge([ws], now=_NOW)
    assert counts.purged == 1
    assert ws.capacity_locked_since is None
    kwargs = email.send_capacity_purge_complete.call_args.kwargs
    assert kwargs == {
        "to_email": "owner@example.test",
        "workspace_name": "Research",
        "locale": "ja",
        "purged_at": _NOW,
        "contexts_deleted": 2,
        "memories_deleted": 12,
        "files_deleted": 3,
        "cleanup_url": _LOCK.cleanup_url,
    }
    audit = db.add.call_args.args[0]
    assert audit.action == "capacity_purge"
    assert audit.user_id == CAPACITY_PURGE_ACTOR
    assert audit.resource == f"workspace:{ws.id}"
    assert audit.user_metadata == {
        "locked_since": since.isoformat(),
        "over_memories": 2,
        "over_bytes": 0,
        "contexts_deleted": 2,
        "contexts_emptied": 1,
        "memories_deleted": 12,
        "files_deleted": 3,
        "bytes_released": 300,
        "aborted": False,
        "aborted_reason": None,
    }


@pytest.mark.parametrize(("after", "logged"), [(_LOCK, True), (None, False)])
@pytest.mark.asyncio
async def test_purge_re_measures_the_lock_once(after, logged) -> None:
    """After the purge the lock is measured again: still over (data the purge
    cannot reach) is logged with its counts — once, no second purge."""
    import structlog

    ws = _purge_ws(datetime(2026, 9, 30, 4, 0))
    stamped, _, owner, ctx_service, service = _purge_patches([ws])
    state = AsyncMock(side_effect=[_LOCK, after])
    with (
        stamped,
        owner,
        ctx_service,
        patch("services.capacity_purge.capacity_lock_state", state),
        patch("services.capacity_purge._owner_cleaned_up", AsyncMock(return_value=False)),
        structlog.testing.capture_logs() as logs,
    ):
        counts = await purge_expired_workspaces(
            _purge_db(),
            now=_NOW,
            email=MagicMock(send_capacity_purge_complete=AsyncMock(return_value=True)),
        )
    assert counts.purged == 1
    assert state.await_count == 2
    service.purge_workspace_data.assert_awaited_once()
    over = [e for e in logs if e["event"] == "capacity_purge_still_over"]
    if logged:
        assert len(over) == 1 and over[0]["log_level"] == "warning"
        assert over[0]["workspace_id"] == str(ws.id)
        assert (over[0]["over_memories"], over[0]["over_bytes"]) == (2, 0)
    else:
        assert over == []


@pytest.mark.asyncio
async def test_purge_skips_workspace_within_capacity_now() -> None:
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    counts, service, email, _ = await _run_purge([ws], now=_NOW, lock=None)
    assert (counts.due, counts.skipped_within, counts.purged) == (1, 1, 0)
    assert ws.capacity_locked_since is None
    service.purge_workspace_data.assert_not_awaited()
    email.send_capacity_purge_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_skips_workspace_resubscribed_before_the_run() -> None:
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0), plan="pro")
    counts, service, _, _ = await _run_purge([ws], now=_NOW)
    assert (counts.skipped_within, counts.purged) == (1, 0)
    service.purge_workspace_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_failure_keeps_the_stamp_and_moves_on() -> None:
    first = _purge_ws(datetime(2026, 9, 1, 4, 0))
    second = _purge_ws(datetime(2026, 9, 2, 4, 0))
    purge = AsyncMock(side_effect=[RuntimeError("db gone"), _REPORT])
    counts, _, email, db = await _run_purge([first, second], now=_NOW, purge=purge)
    assert (counts.failed, counts.purged) == (1, 1)
    db.rollback.assert_awaited()
    assert first.capacity_locked_since is not None  # retried tomorrow
    assert second.capacity_locked_since is None
    assert email.send_capacity_purge_complete.await_count == 1


@pytest.mark.asyncio
async def test_audit_commit_failure_counts_failed_and_moves_on() -> None:
    """The purge succeeded but the audit-row commit raised: logged, rolled back,
    counted as failed, no completion mail — and the next workspace proceeds."""
    first = _purge_ws(datetime(2026, 9, 1, 4, 0))
    second = _purge_ws(datetime(2026, 9, 2, 4, 0))
    db = _purge_db()
    # intent (first), result (first: raises), intent (second), result (second)
    db.commit = AsyncMock(side_effect=[None, RuntimeError("db gone"), None, None])
    counts, service, email, db = await _run_purge([first, second], now=_NOW, db=db)
    assert (counts.failed, counts.purged) == (1, 1)
    assert service.purge_workspace_data.await_count == 2
    db.rollback.assert_awaited_once()
    assert email.send_capacity_purge_complete.await_count == 1
    assert second.capacity_locked_since is None


@pytest.mark.asyncio
async def test_intent_row_is_committed_before_the_purge() -> None:
    """A ``capacity_purge_started`` row is added and committed before any
    deletion, so a purge whose result row is lost still left a trace."""
    since = datetime(2026, 9, 30, 4, 0)
    ws = _purge_ws(since)
    db = _purge_db()
    seen: list[tuple] = []

    async def purge(_ws_id, *, actor, should_continue):
        seen.append((db.add.call_count, db.commit.await_count))
        return _REPORT

    counts, _, _, db = await _run_purge([ws], now=_NOW, db=db, purge=AsyncMock(side_effect=purge))
    assert counts.purged == 1
    assert seen == [(1, 1)]
    intent = db.add.call_args_list[0].args[0]
    assert intent.action == "capacity_purge_started"
    assert intent.user_id == CAPACITY_PURGE_ACTOR
    assert intent.user_email == "owner@example.test"
    assert intent.resource == f"workspace:{ws.id}"
    assert intent.user_metadata["locked_since"] == since.isoformat()
    assert (intent.user_metadata["over_memories"], intent.user_metadata["over_bytes"]) == (2, 0)
    assert isinstance(intent.user_metadata["purge_started_at"], str)
    assert db.add.call_args_list[1].args[0].action == "capacity_purge"


@pytest.mark.asyncio
async def test_intent_commit_failure_deletes_nothing_and_moves_on() -> None:
    first = _purge_ws(datetime(2026, 9, 1, 4, 0))
    second = _purge_ws(datetime(2026, 9, 2, 4, 0))
    db = _purge_db()
    db.commit = AsyncMock(side_effect=[RuntimeError("db gone"), None, None])
    counts, service, email, db = await _run_purge([first, second], now=_NOW, db=db)
    assert (counts.failed, counts.purged) == (1, 1)
    assert service.purge_workspace_data.await_count == 1
    assert service.purge_workspace_data.call_args.args[0] == second.id
    db.rollback.assert_awaited_once()
    assert first.capacity_locked_since is not None  # retried tomorrow
    assert email.send_capacity_purge_complete.await_count == 1


@pytest.mark.asyncio
async def test_nothing_to_delete_clears_stamp_audits_and_sends_no_mail() -> None:
    """A due workspace whose purge removed nothing (e.g. the retry after a lost
    audit commit) is un-stamped and audited, but gets no zero-count mail."""
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    empty = WorkspacePurgeReport(
        contexts_deleted=0,
        contexts_emptied=1,
        memories_deleted=0,
        files_deleted=0,
        bytes_released=0,
    )
    counts, _, email, db = await _run_purge([ws], now=_NOW, purge=AsyncMock(return_value=empty))
    assert (counts.nothing_to_delete, counts.purged, counts.failed) == (1, 0, 0)
    assert ws.capacity_locked_since is None
    email.send_capacity_purge_complete.assert_not_awaited()
    audit = db.add.call_args.args[0]
    assert audit.user_metadata["memories_deleted"] == 0
    assert audit.user_metadata["aborted"] is False


@pytest.mark.asyncio
async def test_aborted_purge_clears_stamp_audits_and_sends_no_mail() -> None:
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    aborted = WorkspacePurgeReport(
        contexts_deleted=1,
        contexts_emptied=0,
        memories_deleted=4,
        files_deleted=0,
        bytes_released=0,
        aborted=True,
    )
    counts, _, email, db = await _run_purge([ws], now=_NOW, purge=AsyncMock(return_value=aborted))
    assert (counts.aborted, counts.purged) == (1, 0)
    assert ws.capacity_locked_since is None
    email.send_capacity_purge_complete.assert_not_awaited()
    assert db.add.call_args.args[0].user_metadata["aborted"] is True


@pytest.mark.asyncio
async def test_should_continue_follows_the_workspace_row() -> None:
    """The callback handed to the purge re-reads the row: re-subscribed or
    soft-deleted (erasure) mid-run means stop."""
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    answers: list[bool] = []

    async def purge(_ws_id, *, actor, should_continue):
        answers.append(await should_continue())
        ws.plan_name = "pro"
        answers.append(await should_continue())
        ws.plan_name = "free"
        ws.deleted_at = _NOW
        answers.append(await should_continue())
        return _REPORT

    await _run_purge([ws], now=_NOW, purge=AsyncMock(side_effect=purge))
    assert answers == [True, False, False]


@pytest.mark.parametrize(
    ("change", "reason"),
    [("plan", "resubscribed"), ("deleted", "erased"), ("cleanup", "owner_cleanup")],
)
@pytest.mark.asyncio
async def test_aborted_purge_records_why(change, reason) -> None:
    """The callback stops on a re-subscribe, an erasure, or the owner's own
    cleanup since the purge started; the audit row and the log say which."""
    import structlog

    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    aborted = WorkspacePurgeReport(
        contexts_deleted=1,
        contexts_emptied=0,
        memories_deleted=4,
        files_deleted=0,
        bytes_released=0,
        aborted=True,
    )

    async def purge(_ws_id, *, actor, should_continue):
        assert await should_continue() is True
        if change == "plan":
            ws.plan_name = "pro"
        elif change == "deleted":
            ws.deleted_at = _NOW
        assert await should_continue() is False
        return aborted

    cleaned = AsyncMock(side_effect=[False, change == "cleanup"])
    with structlog.testing.capture_logs() as logs:
        counts, _, email, db = await _run_purge(
            [ws], now=_NOW, purge=AsyncMock(side_effect=purge), cleaned_up=cleaned
        )
    assert counts.aborted == 1
    assert db.add.call_args.args[0].user_metadata["aborted_reason"] == reason
    event = next(e for e in logs if e["event"] == "capacity_purge_aborted")
    assert event["aborted_reason"] == reason
    email.send_capacity_purge_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_cleanup_check_is_scoped_to_the_purge() -> None:
    """The callback asks about this workspace, since the purge started."""
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    cleaned = AsyncMock(return_value=False)

    async def purge(_ws_id, *, actor, should_continue):
        await should_continue()
        return _REPORT

    with patch("services.capacity_purge._owner_cleaned_up", cleaned):
        stamped, locked, owner, ctx_service, _ = _purge_patches(
            [ws], purge=AsyncMock(side_effect=purge)
        )
        with stamped, locked, owner, ctx_service:
            await purge_expired_workspaces(
                _purge_db(),
                now=_NOW,
                email=MagicMock(send_capacity_purge_complete=AsyncMock(return_value=True)),
            )
    args, kwargs = cleaned.call_args
    assert args[1] == ws.id
    assert isinstance(kwargs["since"], datetime)


@pytest.mark.asyncio
async def test_workspace_erased_mid_run_aborts_without_touching_the_row() -> None:
    """The row vanished (hard-deleted by an erasure) while the purge ran: stop,
    audit it as aborted, and leave the expired instance alone."""
    from sqlalchemy.exc import InvalidRequestError

    since = datetime(2026, 9, 1, 4, 0)
    ws = _purge_ws(since)
    db = _purge_db()
    db.refresh = AsyncMock(side_effect=[None, InvalidRequestError("gone")])
    aborted = WorkspacePurgeReport(
        contexts_deleted=0,
        contexts_emptied=0,
        memories_deleted=0,
        files_deleted=0,
        bytes_released=0,
        aborted=True,
    )

    async def purge(_ws_id, *, actor, should_continue):
        assert await should_continue() is False
        return aborted

    counts, _, email, db = await _run_purge(
        [ws], now=_NOW, db=db, purge=AsyncMock(side_effect=purge)
    )
    assert counts.aborted == 1
    assert ws.capacity_locked_since == since  # not written: the row is gone
    email.send_capacity_purge_complete.assert_not_awaited()
    assert db.add.call_args.args[0].user_metadata["aborted"] is True


@pytest.mark.asyncio
async def test_completion_mail_failure_does_not_undo_the_purge() -> None:
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    email = MagicMock(send_capacity_purge_complete=AsyncMock(side_effect=RuntimeError("smtp")))
    counts, _, _, _ = await _run_purge([ws], now=_NOW, email=email)
    assert counts.purged == 1
    assert ws.capacity_locked_since is None


@pytest.mark.parametrize(
    ("send", "event"),
    [
        (AsyncMock(return_value=True), "capacity_purge_complete_mail_sent"),
        (AsyncMock(return_value=False), "capacity_purge_complete_mail_failed"),
        (AsyncMock(side_effect=TimeoutError()), "capacity_purge_complete_send_raised"),
    ],
)
@pytest.mark.asyncio
async def test_completion_mail_outcome_is_logged_per_workspace(send, event) -> None:
    """The provider log carries no workspace id: the purge logs the mail's
    outcome under the workspace, so a lost completion mail can be alerted on."""
    import structlog

    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    with structlog.testing.capture_logs() as logs:
        await _run_purge([ws], now=_NOW, email=MagicMock(send_capacity_purge_complete=send))
    mail_logs = [e for e in logs if e["event"].startswith("capacity_purge_complete_")]
    assert [(e["event"], e["workspace_id"]) for e in mail_logs] == [(event, str(ws.id))]


@pytest.mark.asyncio
async def test_no_owner_email_still_purges_without_mail() -> None:
    ws = _purge_ws(datetime(2026, 9, 1, 4, 0))
    counts, _, email, db = await _run_purge([ws], now=_NOW, contact=None)
    assert counts.purged == 1
    email.send_capacity_purge_complete.assert_not_awaited()
    assert db.add.call_args.args[0].user_email == ""


# ---------------------------------------------------------------------------
# The sweep: stamp, then notices and purge behind their flags
# ---------------------------------------------------------------------------

_STAMP = StampCounts(evaluated=1, stamped=0, cleared=0, kept=1)
_NOTICES = NoticeCounts(considered=1, sent=1, already_sent=0, skipped=0, failed=0)
_PURGED = PurgeCounts(due=1, purged=1, skipped_within=0, failed=0, aborted=0)


@pytest.mark.parametrize(
    ("notice_on", "purge_on"), [(False, False), (True, False), (False, True), (True, True)]
)
@pytest.mark.asyncio
async def test_sweep_gates_notices_and_purge_on_their_flags(notice_on, purge_on) -> None:
    settings = MagicMock(
        capacity_purge_notice_enabled=notice_on,
        capacity_purge_enabled=purge_on,
        email_provider="resend",
    )
    stamp = AsyncMock(return_value=_STAMP)
    notices = AsyncMock(return_value=_NOTICES)
    purge = AsyncMock(return_value=_PURGED)
    with (
        patch("config.settings.get_settings", return_value=settings),
        patch("db.redis.get_redis_client", return_value=MagicMock()),
        patch("services.email_service.get_email_service", return_value=MagicMock()),
        patch("services.capacity_purge.stamp_capacity_locks", stamp),
        patch("services.capacity_purge.send_due_purge_notices", notices),
        patch("services.capacity_purge.purge_expired_workspaces", purge),
    ):
        result = await run_capacity_purge_sweep(MagicMock(), now=_NOW)
    stamp.assert_awaited_once()
    assert result.stamp == _STAMP
    assert result.notices == (_NOTICES if notice_on else None)
    assert result.purge == (_PURGED if purge_on else None)
    assert notices.await_count == int(notice_on)
    assert purge.await_count == int(purge_on)


@pytest.mark.parametrize("provider", ["logging", "smtp-not-wired"])
@pytest.mark.asyncio
async def test_sweep_without_a_real_mail_provider_sends_nothing_and_deletes_nothing(
    provider,
) -> None:
    """Both flags on, but no provider that actually delivers mail: the logging
    stub would report every notice as sent, so the final notice would be
    recorded and the purge would follow without anyone being told. The notice
    and purge stages are skipped (one warning per run); stamping still runs."""
    import structlog

    settings = MagicMock(
        capacity_purge_notice_enabled=True,
        capacity_purge_enabled=True,
        email_provider=provider,
    )
    stamp = AsyncMock(return_value=_STAMP)
    notices = AsyncMock(return_value=_NOTICES)
    purge = AsyncMock(return_value=_PURGED)
    get_email = MagicMock()
    with (
        patch("config.settings.get_settings", return_value=settings),
        patch("db.redis.get_redis_client", return_value=MagicMock()),
        patch("services.email_service.get_email_service", get_email),
        patch("services.capacity_purge.stamp_capacity_locks", stamp),
        patch("services.capacity_purge.send_due_purge_notices", notices),
        patch("services.capacity_purge.purge_expired_workspaces", purge),
        structlog.testing.capture_logs() as logs,
    ):
        result = await run_capacity_purge_sweep(MagicMock(), now=_NOW)
    stamp.assert_awaited_once()
    assert result == SweepCounts(stamp=_STAMP, notices=None, purge=None)
    notices.assert_not_awaited()
    purge.assert_not_awaited()
    get_email.assert_not_called()
    gated = [e for e in logs if e["event"] == "capacity_purge_no_mail_provider"]
    assert len(gated) == 1 and gated[0]["log_level"] == "warning"


@pytest.mark.parametrize(("provider", "real"), [("resend", True), ("logging", False)])
def test_mail_provider_configured(provider, real) -> None:
    assert mail_provider_configured(MagicMock(email_provider=provider)) is real
