"""The token lock shared by the notice flush and the calibration dedup."""

from __future__ import annotations

import fakeredis
import fakeredis.aioredis
import pytest

from utils.redis_lock import acquire_lock, acquire_lock_sync, release_lock, release_lock_sync


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_second_acquire_fails_until_release(redis) -> None:
    token = await acquire_lock(redis, "lock:a", 30)
    assert token is not None
    assert await acquire_lock(redis, "lock:a", 30) is None
    assert await release_lock(redis, "lock:a", token) is True
    assert await acquire_lock(redis, "lock:a", 30) is not None


@pytest.mark.asyncio
async def test_lock_expires(redis) -> None:
    await acquire_lock(redis, "lock:a", 30)
    assert 0 < await redis.ttl("lock:a") <= 30


@pytest.mark.asyncio
async def test_release_with_a_stale_token_keeps_the_new_holder(redis) -> None:
    stale = await acquire_lock(redis, "lock:a", 30)
    assert stale is not None
    await redis.delete("lock:a")  # expired
    current = await acquire_lock(redis, "lock:a", 30)
    assert await release_lock(redis, "lock:a", stale) is False
    assert await redis.get("lock:a") == current


@pytest.mark.asyncio
async def test_release_of_a_missing_lock_is_a_no_op(redis) -> None:
    assert await release_lock(redis, "lock:a", "token") is False


@pytest.mark.asyncio
async def test_bytes_client_is_supported() -> None:
    raw = fakeredis.aioredis.FakeRedis()
    token = await acquire_lock(raw, "lock:a", 30)
    assert token is not None
    assert await release_lock(raw, "lock:a", token) is True


# ---------------------------------------------------------------------------
# The synchronous twin (#1878)
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def test_sync_second_acquire_fails_until_release(sync_redis) -> None:
    token = acquire_lock_sync(sync_redis, "lock:a", 30)
    assert token is not None
    assert 0 < sync_redis.ttl("lock:a") <= 30
    assert acquire_lock_sync(sync_redis, "lock:a", 30) is None
    assert release_lock_sync(sync_redis, "lock:a", token) is True
    assert acquire_lock_sync(sync_redis, "lock:a", 30) is not None


def test_sync_release_with_a_stale_token_keeps_the_new_holder(sync_redis) -> None:
    stale = acquire_lock_sync(sync_redis, "lock:a", 30)
    assert stale is not None
    sync_redis.delete("lock:a")  # expired
    current = acquire_lock_sync(sync_redis, "lock:a", 30)
    assert release_lock_sync(sync_redis, "lock:a", stale) is False
    assert sync_redis.get("lock:a") == current


def test_sync_release_of_a_missing_lock_is_a_no_op(sync_redis) -> None:
    assert release_lock_sync(sync_redis, "lock:a", "token") is False


def test_sync_release_loses_to_a_holder_that_took_the_lock_meanwhile() -> None:
    # The lease expires and another worker takes the lock after this
    # release read the token but before its DELETE: the WATCH aborts the
    # transaction, so the new holder's lock survives.
    server = fakeredis.FakeServer()
    mine = fakeredis.FakeRedis(server=server, decode_responses=True)
    other = fakeredis.FakeRedis(server=server, decode_responses=True)
    token = acquire_lock_sync(mine, "lock:a", 30)
    assert token is not None

    class _RacingClient:
        def pipeline(self, transaction: bool = True):
            pipe = mine.pipeline(transaction=transaction)
            real_get = pipe.get

            def _get_then_lose_the_lock(key: str):
                held = real_get(key)
                other.set(key, "new-holder")
                return held

            pipe.get = _get_then_lose_the_lock  # type: ignore[method-assign]
            return pipe

    assert release_lock_sync(_RacingClient(), "lock:a", token) is False
    assert mine.get("lock:a") == "new-holder"


def test_sync_bytes_client_is_supported() -> None:
    raw = fakeredis.FakeRedis()
    token = acquire_lock_sync(raw, "lock:a", 30)
    assert token is not None
    assert release_lock_sync(raw, "lock:a", token) is True
