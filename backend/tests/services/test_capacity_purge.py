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
    notice_key,
    notice_stage,
    purge_at,
    send_due_purge_notices,
)


def test_purge_at_is_thirty_days_after_lock_start() -> None:
    since = datetime(2026, 10, 1, 4, 0)
    assert purge_at(since) == datetime(2026, 10, 31, 4, 0)
    assert PURGE_AFTER == timedelta(days=30)


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
