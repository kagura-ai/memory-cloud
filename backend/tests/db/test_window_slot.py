"""Real-Redis tests of the returnable fixed-window counter (#3054).

``take_window_slot`` counts one unit in a fixed window (the key's first unit
gives it its expiry, in the same transaction) and remembers which window it
was counted in: when that window ends, on the Redis server's clock.
``return_window_slot`` gives that unit back, atomically, only while the same
window is live and holds a unit: never below zero, never a key created by the
return, never into a later window of the same key.

Skipped when no Redis answers at ``REDIS_URL`` (unit runs without docker).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import redis

from config.database import REDIS_URL
from db.redis import WindowSlot, get_redis_client, return_window_slot, take_window_slot


def _redis_available() -> bool:
    """Sync PING with a short connect timeout, evaluated once at collection."""
    try:
        return bool(redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(), reason="requires a reachable Redis at REDIS_URL"
)


def _key() -> str:
    return f"test:window_slot:{uuid.uuid4().hex}"


async def _cleanup(key: str) -> None:
    await get_redis_client().delete(key)


async def test_a_first_unit_opens_a_window_with_its_expiry() -> None:
    key = _key()
    try:
        slot = await take_window_slot(key, ttl=60)

        assert slot.key == key
        assert slot.count == 1
        now = await get_redis_client().time()
        now_ms = int(now[0]) * 1000 + int(now[1]) // 1000
        assert slot.expires_at_ms is not None
        assert 59_000 < slot.expires_at_ms - now_ms <= 60_000
        assert await get_redis_client().get(key) == "1"
        assert 59_000 < await get_redis_client().pttl(key) <= 60_000
    finally:
        await _cleanup(key)


async def test_later_units_keep_the_window() -> None:
    key = _key()
    try:
        first = await take_window_slot(key, ttl=60)
        await asyncio.sleep(0.05)
        second = await take_window_slot(key, ttl=60)

        assert (first.count, second.count) == (1, 2)
        # The same window: the same end.
        assert first.expires_at_ms is not None and second.expires_at_ms is not None
        assert abs(second.expires_at_ms - first.expires_at_ms) <= 5
    finally:
        await _cleanup(key)


async def test_a_returned_unit_leaves_the_window() -> None:
    key = _key()
    try:
        await take_window_slot(key, ttl=60)
        slot = await take_window_slot(key, ttl=60)
        ttl_before = await get_redis_client().pttl(key)

        assert await return_window_slot(slot) is True

        assert await get_redis_client().get(key) == "1"
        # The window keeps its expiry.
        assert 0 < await get_redis_client().pttl(key) <= ttl_before
    finally:
        await _cleanup(key)


async def test_concurrent_returns_never_go_below_what_was_taken() -> None:
    key = _key()
    try:
        slots = await asyncio.gather(*(take_window_slot(key, ttl=60) for _ in range(20)))

        returned = await asyncio.gather(*(return_window_slot(slot) for slot in slots[:15]))

        assert all(returned)
        assert await get_redis_client().get(key) == "5"
    finally:
        await _cleanup(key)


async def test_a_return_never_goes_below_zero() -> None:
    key = _key()
    try:
        slot = await take_window_slot(key, ttl=60)
        # Whatever brought the window to zero, a return does not go below it.
        await get_redis_client().set(key, 0, keepttl=True)

        assert await return_window_slot(slot) is False

        assert await get_redis_client().get(key) == "0"
    finally:
        await _cleanup(key)


async def test_a_return_after_the_window_ended_creates_nothing() -> None:
    key = _key()
    try:
        slot = await take_window_slot(key, ttl=60)
        await get_redis_client().delete(key)

        assert await return_window_slot(slot) is False

        assert await get_redis_client().exists(key) == 0
    finally:
        await _cleanup(key)


async def test_a_return_never_reaches_a_later_window_of_the_same_key() -> None:
    """The first window ends; a later request opens the next one. The unit
    taken in the first window is not given back to the second."""
    key = _key()
    try:
        slot = await take_window_slot(key, ttl=3)
        await asyncio.sleep(3.2)
        later = await take_window_slot(key, ttl=3)
        assert later.count == 1

        assert await return_window_slot(slot) is False

        assert await get_redis_client().get(key) == "1"
    finally:
        await _cleanup(key)


async def test_a_slot_whose_window_does_not_match_is_not_returned() -> None:
    """The window is told by its end: one that differs by more than the
    tolerance is another window (a key's later window ends at least its whole
    length after an earlier one)."""
    key = _key()
    try:
        taken = await take_window_slot(key, ttl=60)
        assert taken.expires_at_ms is not None
        earlier_window = WindowSlot(key=key, count=1, expires_at_ms=taken.expires_at_ms - 60_000)

        assert await return_window_slot(earlier_window) is False

        assert await get_redis_client().get(key) == "1"
    finally:
        await _cleanup(key)


async def test_a_window_without_expiry_is_repaired_and_its_unit_kept() -> None:
    """A counter an older writer left without an expiry gets one; the unit
    taken there names no window, so it is never given back."""
    key = _key()
    try:
        await get_redis_client().set(key, 4)

        slot = await take_window_slot(key, ttl=60)

        assert slot.count == 5
        assert slot.expires_at_ms is None
        assert 0 < await get_redis_client().ttl(key) <= 60
        assert await return_window_slot(slot) is False
        assert await get_redis_client().get(key) == "5"
    finally:
        await _cleanup(key)


async def test_a_window_shorter_than_twice_the_tolerance_is_refused() -> None:
    with pytest.raises(ValueError):
        await take_window_slot(_key(), ttl=1)
