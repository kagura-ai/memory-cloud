"""The token lock shared by the notice flush and the calibration dedup."""

from __future__ import annotations

import fakeredis
import fakeredis.aioredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.exceptions import WatchError

from utils.redis_lock import (
    acquire_lock,
    acquire_lock_sync,
    connection_failure,
    release_lock,
    release_lock_sync,
    watch_token_sync,
)


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


# ---------------------------------------------------------------------------
# A connection failure while watching is not a lost lock (#1918)
# ---------------------------------------------------------------------------


def _dressed_watch_error(failure: Exception) -> WatchError:
    """What redis-py raises when the connection dies while a key is watched.

    It raises the ``WatchError`` from the handler of the failure, so the
    failure is the error's implicit context.
    """
    exc = WatchError(f"A {type(failure).__name__} occurred while watching one or more keys")
    exc.__context__ = failure
    return exc


def _connection_dressed_watch_error() -> WatchError:
    return _dressed_watch_error(RedisConnectionError("Connection closed by server."))


def _timeout_dressed_watch_error() -> WatchError:
    return _dressed_watch_error(RedisTimeoutError("Timeout reading from socket"))


def test_connection_failure_tells_a_dead_connection_from_a_changed_key() -> None:
    changed = WatchError("Watched variable changed.")
    assert connection_failure(changed) is None
    assert isinstance(connection_failure(_connection_dressed_watch_error()), RedisConnectionError)
    assert isinstance(connection_failure(_timeout_dressed_watch_error()), RedisTimeoutError)


def test_sync_release_raises_the_connection_failure_rather_than_answering_false(
    sync_redis,
) -> None:
    # False would read as "the lock was not ours any more"; the caller
    # would then log an expired lease for what is a Redis outage.
    token = acquire_lock_sync(sync_redis, "lock:a", 30)
    assert token is not None
    real_pipeline = sync_redis.pipeline

    def _pipeline(transaction: bool = True):
        pipe = real_pipeline(transaction=transaction)

        def _exec_loses_the_connection():
            pipe.reset()
            raise _connection_dressed_watch_error()

        pipe.execute = _exec_loses_the_connection  # type: ignore[method-assign]
        return pipe

    sync_redis.pipeline = _pipeline  # type: ignore[method-assign]

    with pytest.raises(RedisConnectionError):
        release_lock_sync(sync_redis, "lock:a", token)


@pytest.mark.asyncio
async def test_release_raises_the_connection_failure_rather_than_answering_false(redis) -> None:
    token = await acquire_lock(redis, "lock:a", 30)
    assert token is not None
    real_pipeline = redis.pipeline

    def _pipeline(transaction: bool = True):
        pipe = real_pipeline(transaction=transaction)

        async def _exec_loses_the_connection():
            await pipe.reset()
            raise _timeout_dressed_watch_error()

        pipe.execute = _exec_loses_the_connection  # type: ignore[method-assign]
        return pipe

    redis.pipeline = _pipeline  # type: ignore[method-assign]

    with pytest.raises(RedisTimeoutError):
        await release_lock(redis, "lock:a", token)


def test_sync_watch_token_opens_the_transaction_only_for_the_holder(sync_redis) -> None:
    token = acquire_lock_sync(sync_redis, "lock:a", 30)
    assert token is not None
    with sync_redis.pipeline(transaction=True) as pipe:
        assert watch_token_sync(pipe, "lock:a", token) is True
        pipe.set("written", "under-the-lock")
        pipe.execute()
    assert sync_redis.get("written") == "under-the-lock"

    with sync_redis.pipeline(transaction=True) as pipe:
        assert watch_token_sync(pipe, "lock:a", "not-mine") is False
        assert pipe.watching is False
    with sync_redis.pipeline(transaction=True) as pipe:
        assert watch_token_sync(pipe, "lock:missing", token) is False
