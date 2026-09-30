"""The token lock shared by the notice flush and the calibration dedup."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from utils.redis_lock import acquire_lock, release_lock


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
