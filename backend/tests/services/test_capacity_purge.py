"""#1969: the capacity purge — clock arithmetic and the countdown notices (mocked DB)."""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.capacity_lock import CapacityLock
from services.capacity_purge import (
    NOTICE_DAYS,
    NOTICE_KEY_TTL_SECONDS,
    PURGE_AFTER,
    PURGE_MAX_WORKSPACES_PER_RUN,
    SWEEP_HOUR_UTC,
    NoticeCounts,
    PurgeCounts,
    StampCounts,
    notice_key,
    notice_stage,
    purge_at,
    purge_due,
    purge_expired_workspaces,
    run_capacity_purge_sweep,
    send_due_purge_notices,
)
from services.context_service import CAPACITY_PURGE_ACTOR, WorkspacePurgeReport


def test_purge_at_is_thirty_days_after_lock_start() -> None:
    since = datetime(2026, 10, 1, 4, 0)
    assert purge_at(since) == datetime(2026, 10, 31, 4, 0)
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
    deadline = purge_at(since)
    assert deadline == datetime(2026, 10, 31, SWEEP_HOUR_UTC, 0)
    assert purge_due(since, deadline)
    assert not purge_due(since, deadline - timedelta(days=1))


def test_purge_at_none_when_not_locked() -> None:
    assert purge_at(None) is None


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
    assert kwargs["purge_at"] == purge_at(ws.capacity_locked_since)
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
    email = _email(return_value=True)
    with _locked():
        await send_due_purge_notices(
            _db([ws]), now=datetime(2026, 10, 30, 4, 0, 0), redis=_Redis(), email=email
        )
    assert email.send_capacity_purge_notice.call_args.kwargs["days_left"] == 1


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


def _purge_ws(since: datetime | None, *, plan: str = "free"):
    ws = _ws(since, plan=plan)
    ws.deleted_at = None
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


async def _run_purge(workspaces, *, now, email=None, db=None, **kwargs):
    stamped, locked, owner, ctx_service, service = _purge_patches(workspaces, **kwargs)
    email = email or MagicMock(send_capacity_purge_complete=AsyncMock(return_value=True))
    db = db or _purge_db()
    with stamped, locked, owner, ctx_service:
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
    }


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
    db.commit = AsyncMock(side_effect=[RuntimeError("db gone"), None])
    counts, service, email, db = await _run_purge([first, second], now=_NOW, db=db)
    assert (counts.failed, counts.purged) == (1, 1)
    assert service.purge_workspace_data.await_count == 2
    db.rollback.assert_awaited_once()
    assert email.send_capacity_purge_complete.await_count == 1
    assert second.capacity_locked_since is None


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
    settings = MagicMock(capacity_purge_notice_enabled=notice_on, capacity_purge_enabled=purge_on)
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
