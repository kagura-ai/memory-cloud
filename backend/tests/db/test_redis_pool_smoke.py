"""Real-Redis smoke test for the blocking connection pool (Issue #1556).

Reproduces the gate2 measurement from #1549 against a live Redis: with the
old non-blocking ``max_connections=10`` pool, 60 concurrent quota increments
raised ``Too many connections`` for most callers and the counter ended at 10.
With ``BlockingConnectionPool`` every caller queues for a connection, so all
60 increments land.

Skipped when no Redis answers at ``REDIS_URL`` (unit runs without docker).
"""

import asyncio
import uuid

import pytest
import redis

import db.redis as redis_mod
from config.database import REDIS_URL
from db.redis import get_redis_client, incrby_counter


def _redis_available() -> bool:
    """Sync PING with a short connect timeout, evaluated once at collection."""
    try:
        return bool(redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(), reason="requires a reachable Redis at REDIS_URL"
)


class _PoolSettings:
    """Settings stub: a pool far smaller than the burst, so callers must queue."""

    redis_max_connections = 5
    redis_pool_timeout_seconds = 2.0


async def test_sixty_concurrent_incrby_all_counted(monkeypatch):
    """60 parallel increments through a 5-connection blocking pool all land."""
    original = redis_mod._redis_client
    monkeypatch.setattr(redis_mod, "_redis_client", None)
    monkeypatch.setattr(redis_mod, "get_settings", lambda: _PoolSettings())
    key = f"test:pool_smoke:{uuid.uuid4().hex}"
    client = get_redis_client()
    try:
        assert isinstance(client.connection_pool, redis_mod.aioredis.BlockingConnectionPool)
        assert client.connection_pool.max_connections == 5

        results = await asyncio.gather(*(incrby_counter(key, 1, ttl=60) for _ in range(60)))

        # Every increment returned a distinct running total — none failed or
        # was swallowed — and the stored counter matches.
        assert sorted(results) == list(range(1, 61))
        assert await client.get(key) == "60"
    finally:
        await client.delete(key)
        await client.aclose()
        monkeypatch.setattr(redis_mod, "_redis_client", original)
