"""Redis client for cache and session management.

One pooled client per event loop: a single uvicorn worker runs one loop, so in
production this is a process-wide singleton.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, cast

import redis.asyncio as aioredis

from config.database import INVALID_REDIS_URL_MESSAGE, REDIS_URL
from config.settings import get_settings
from db.loop_binding import keep_for_loop, owned_by_another_loop, running_loop, tag_loop
from utils.exceptions import RedisError
from utils.logger import get_logger
from utils.url_redact import redis_location

logger = get_logger(__name__)

# The client for the current event loop (one per worker in production; see
# get_redis_client()). Callers fetch it at the point of use and never keep it.
_redis_client: aioredis.Redis | None = None


def get_redis_client() -> aioredis.Redis:
    """Get the Redis client for the running event loop.

    A ``redis.asyncio`` client's sockets and its pool's waiters belong to the
    loop that first used them. When the running loop is not the one the
    current client was built for, the old client is dropped without being
    closed (its loop is usually gone, so it cannot be closed from here) and a
    new one is built (#1995). That happens in tests, where each
    ``TestClient`` portal and the pytest-asyncio session loop are separate
    loops; a uvicorn worker runs one loop, so production builds once.

    Returns:
        Redis client instance

    Raises:
        RedisError: If connection fails
    """
    global _redis_client

    loop = running_loop()
    if _redis_client is not None and not keep_for_loop(_redis_client, loop):
        logger.debug("redis_client_rebuilt_for_new_loop")
        _redis_client = None

    if _redis_client is None:
        settings = get_settings()
        pool = None
        try:
            # #1556: BlockingConnectionPool queues callers for up to ``timeout``
            # seconds when every connection is checked out. The default
            # ConnectionPool raised ``Too many connections`` immediately, which
            # the quota / rate-limit paths treat as "Redis down" and fail open —
            # so a burst of >max_connections concurrent requests skipped the
            # daily counters. A timed-out wait still surfaces as a
            # ``redis.exceptions.ConnectionError`` (wrapped as RedisError by the
            # helpers below), so the fail-open branches keep working as the
            # backstop. from_url keeps the URL-driven options (rediss:// → TLS).
            pool = aioredis.BlockingConnectionPool.from_url(
                REDIS_URL,
                max_connections=settings.redis_max_connections,
                timeout=settings.redis_pool_timeout_seconds,
                encoding="utf-8",
                decode_responses=True,
            )
        except ValueError:
            # #1881: the URL did not parse. The parser's text quotes part of
            # the password, so it is neither interpolated nor chained: the
            # fixed error is raised below, outside this handler, which leaves
            # both __cause__ and __context__ empty.
            pass
        except Exception as e:
            raise RedisError(f"Failed to connect to Redis: {e}") from e
        if pool is None:
            raise RedisError(INVALID_REDIS_URL_MESSAGE)

        try:
            _redis_client = aioredis.Redis.from_pool(pool)
            tag_loop(_redis_client, loop)
            logger.info(
                "redis_client_initialized",
                # Host and port only: ``redact_generic_url`` keeps the query,
                # where a Redis URL can hold ``?password=`` (#1898).
                url=redis_location(REDIS_URL),
                max_connections=settings.redis_max_connections,
                pool_timeout_seconds=settings.redis_pool_timeout_seconds,
            )
        except Exception as e:
            raise RedisError(f"Failed to connect to Redis: {e}") from e

    return _redis_client


async def close_redis() -> None:
    """Close the Redis client and forget it.

    The client is closed with ``aclose()`` only on the loop it belongs to; a
    client from another loop is just dropped (its connections cannot be closed
    from here). The module forgets the client before closing it, so it is
    reset even when ``aclose()`` raises or is cancelled, and the error is
    raised to the caller.
    """
    global _redis_client

    client = _redis_client
    if client is None:
        return
    other_loop = owned_by_another_loop(client, asyncio.get_running_loop())
    _redis_client = None
    if other_loop:
        logger.debug("redis_client_dropped_from_another_loop")
        return
    await client.aclose()
    logger.info("redis_connection_closed")


# Convenience functions


async def get_cache(key: str) -> str | None:
    """Get value from Redis cache.

    Args:
        key: Cache key

    Returns:
        Cached value or None
    """
    client = get_redis_client()
    try:
        # get_redis_client() builds the client with decode_responses=True, so
        # values really are str; the redis-py stub is not parameterized on that
        # flag and widens to bytes | str (#1442).
        return cast(str | None, await client.get(key))
    except Exception as e:
        logger.error("redis_get_failed", key=key, error=str(e))
        return None


async def set_cache(key: str, value: str, ttl: int | None = None) -> None:
    """Set value in Redis cache.

    Args:
        key: Cache key
        value: Value to cache
        ttl: Time-to-live in seconds (None = no expiration)
    """
    client = get_redis_client()
    try:
        if ttl:
            await client.setex(key, ttl, value)
        else:
            await client.set(key, value)
    except Exception as e:
        logger.error("redis_set_failed", key=key, error=str(e))
        raise RedisError(f"Failed to set cache: {e}") from e


async def set_if_absent(key: str, value: str, ttl: int) -> bool:
    """``SET key value NX EX ttl``: claim a key for ``ttl`` seconds.

    For once-per-window markers (the #2615 plan headroom throttle): the
    first caller in the window gets True, every other caller False until the
    key expires.

    Args:
        key: Marker key
        value: Value to store
        ttl: Time-to-live in seconds

    Returns:
        True when this call created the key, False when it already existed

    Raises:
        RedisError: The command failed (callers decide whether to fail open)
    """
    client = get_redis_client()
    try:
        return bool(await client.set(key, value, nx=True, ex=ttl))
    except Exception as e:
        logger.error("redis_set_nx_failed", key=key, error=str(e))
        raise RedisError(f"Failed to set cache: {e}") from e


async def delete_cache(key: str) -> None:
    """Delete key from Redis cache.

    Args:
        key: Cache key
    """
    client = get_redis_client()
    try:
        await client.delete(key)
    except Exception as e:
        logger.error("redis_delete_failed", key=key, error=str(e))


async def increment_counter(key: str, ttl: int | None = None) -> int:
    """Increment counter in Redis.

    Args:
        key: Counter key
        ttl: Set expiration if key doesn't exist

    Returns:
        New counter value

    Example:
        # Rate limiting
        count = await increment_counter(f"rate_limit:{user_id}:{minute}", ttl=60)
        if count > 100:
            raise RateLimitError()
    """
    client = get_redis_client()
    try:
        # INCR and TTL in one round trip. The TTL is set whenever the key has
        # none, not only on the first increment: if an EXPIRE was lost (a
        # failure between INCR and EXPIRE), the next call repairs it instead
        # of leaving a counter that never resets.
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            if ttl:
                pipe.ttl(key)
            results = await pipe.execute()
        count = int(results[0])

        # ttl() returns -1 when the key has no expiry.
        if ttl and results[1] < 0:
            await client.expire(key, ttl)

        return count
    except Exception as e:
        logger.error("redis_incr_failed", key=key, error=str(e))
        raise RedisError(f"Failed to increment counter: {e}") from e


async def incrby_counter(key: str, amount: int, ttl: int | None = None) -> int:
    """Increment counter by `amount` in Redis, setting TTL only if the key has none.

    Checks TTL after INCRBY and sets expiration only when absent. Works on any
    Redis version (no dependency on EXPIRE NX / Redis 7.0+). Small race window
    between TTL check and EXPIRE is acceptable for advisory rate limiting.
    """
    client = get_redis_client()
    try:
        count = await client.incrby(key, amount)
        if ttl:
            # client.ttl returns -1 when the key has no TTL, -2 when missing.
            remaining = await client.ttl(key)
            if remaining < 0:
                await client.expire(key, ttl)
        return count
    except Exception as e:
        logger.error("redis_incrby_failed", key=key, amount=amount, error=str(e))
        raise RedisError(f"Failed to increment counter: {e}") from e


# ========================================================================
# Returnable fixed-window counter (#3054)
# ========================================================================

# How far the expiry a unit was counted under may seem to differ from the live
# window's when it is returned: both are read on the Redis server's clock as
# ``TIME`` plus ``PTTL``, a few milliseconds apart at most. Windows of one key
# never overlap: the next window opens after the last one expired and lives
# ``ttl`` seconds, so it ends at least ``ttl`` after the last one did, and
# ``take_window_slot`` refuses a ``ttl`` of two tolerances or less.
WINDOW_SLOT_TOLERANCE_MS = 1000


@dataclass(frozen=True)
class WindowSlot:
    """One unit a request counted in a fixed-window counter (``take_window_slot``).

    Attributes:
        key: The counter.
        count: The counter's value right after this unit (the caller's limit
            check reads it).
        expires_at_ms: When the window the unit was counted in ends, in
            milliseconds on the Redis server's clock: what tells the window
            apart from the key's later ones. None when the counter had no
            expiry (the unit is never returned).
    """

    key: str
    count: int
    expires_at_ms: int | None


def _server_ms(time_reply: Any) -> int:
    """A ``TIME`` reply (seconds, microseconds) in milliseconds."""
    seconds, micros = time_reply
    return int(seconds) * 1000 + int(micros) // 1000


async def take_window_slot(key: str, ttl: int) -> WindowSlot:
    """Count one unit in a fixed-window counter, in one transaction.

    ``SET key 0 PX ttl NX`` (the window's first unit gives it its expiry),
    ``INCR``, ``PTTL`` and ``TIME`` in one ``MULTI``: no other client's command
    runs in between, so the unit is counted in the window that ends at
    ``expires_at_ms``. A counter found without an expiry (an older writer's
    increment that lost its ``EXPIRE``) gets one, and the unit names no
    window. Works on any Redis version.

    Args:
        key: The counter.
        ttl: The window's length in seconds; more than two
            ``WINDOW_SLOT_TOLERANCE_MS``.

    Returns:
        The unit, for the caller's limit check and ``return_window_slot``.

    Raises:
        ValueError: ``ttl`` is too short to tell its windows apart.
        RedisError: Redis could not be read or written.
    """
    if ttl * 1000 <= 2 * WINDOW_SLOT_TOLERANCE_MS:
        raise ValueError("a returnable window must be longer than two tolerances")
    client = get_redis_client()
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.set(key, 0, px=ttl * 1000, nx=True)
            pipe.incr(key)
            pipe.pttl(key)
            pipe.time()
            results = await pipe.execute()
        count, remaining = int(results[1]), int(results[2])
        if remaining < 0:
            await client.expire(key, ttl)
            return WindowSlot(key=key, count=count, expires_at_ms=None)
        return WindowSlot(key=key, count=count, expires_at_ms=_server_ms(results[3]) + remaining)
    except Exception as e:
        logger.error("redis_window_slot_take_failed", error_type=type(e).__name__)
        raise RedisError(f"Failed to count a window slot: {type(e).__name__}") from e


# KEYS[1]: the counter. ARGV[1]: when the window the unit was counted in ends
# (ms, server clock). ARGV[2]: the tolerance (ms). Decrements only while that
# window is the live one (the same end, within the tolerance) and holds a
# unit; returns 1 when it did, 0 otherwise. Never creates the key.
# ``replicate_commands`` lets Redis before 5.0 write after ``TIME`` (later
# versions replicate a script's effects anyway; 7.0 keeps it as a no-op).
_RETURN_WINDOW_SLOT_SCRIPT = (
    "if redis.replicate_commands then redis.replicate_commands() end "
    "local ttl = redis.call('pttl', KEYS[1]) "
    "if ttl < 0 then return 0 end "
    "local now = redis.call('time') "
    "local ends = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000) + ttl "
    "if math.abs(ends - tonumber(ARGV[1])) > tonumber(ARGV[2]) then return 0 end "
    "local count = tonumber(redis.call('get', KEYS[1])) "
    "if count == nil or count <= 0 then return 0 end "
    "redis.call('decr', KEYS[1]) "
    "return 1"
)
# The registered script and the ``id()`` of the client it belongs to (as
# ``services.storage_quota_service`` caches its own): a new client per event
# loop registers it again.
_return_window_slot_script: tuple[int, Any] | None = None


async def return_window_slot(slot: WindowSlot) -> bool:
    """Give one unit back to the window it was counted in, if still live.

    Atomic (a Lua script): the counter is decremented only while the window
    ``slot`` was counted in is the key's live window and holds a unit, so a
    return never takes the counter below zero, never creates the key, and
    never reaches a later window of the key, however late it runs. Each slot
    is returned at most once by its caller.

    Args:
        slot: What ``take_window_slot`` answered.

    Returns:
        True when the unit was given back; False when its window had ended or
        it names none.

    Raises:
        RedisError: Redis could not be read or written.
    """
    global _return_window_slot_script
    if slot.expires_at_ms is None:
        return False
    client = get_redis_client()
    if _return_window_slot_script is None or _return_window_slot_script[0] != id(client):
        _return_window_slot_script = (
            id(client),
            client.register_script(_RETURN_WINDOW_SLOT_SCRIPT),
        )
    script = _return_window_slot_script[1]
    try:
        returned = await script(
            keys=[slot.key], args=[slot.expires_at_ms, WINDOW_SLOT_TOLERANCE_MS]
        )
    except Exception as e:
        logger.error("redis_window_slot_return_failed", error_type=type(e).__name__)
        raise RedisError(f"Failed to return a window slot: {type(e).__name__}") from e
    return int(returned) == 1


# ========================================================================
# Co-activation Persistence (Issue #84 Phase 2C)
# ========================================================================


async def get_co_activation(user_id: str, node_1: str, node_2: str) -> dict | None:
    """Get co-activation record from Redis.

    Args:
        user_id: User ID
        node_1: First node ID
        node_2: Second node ID

    Returns:
        Co-activation record dict or None
    """
    import json

    # Normalize order for consistent keys
    if node_1 > node_2:
        node_1, node_2 = node_2, node_1

    key = f"co_act:{user_id}:{node_1}:{node_2}"
    cached = await get_cache(key)

    return json.loads(cached) if cached else None


async def set_co_activation(
    user_id: str,
    node_1: str,
    node_2: str,
    record: dict,
    ttl: int = 604800,  # 7 days
) -> None:
    """Save co-activation record to Redis with 7-day TTL.

    Args:
        user_id: User ID
        node_1: First node ID
        node_2: Second node ID
        record: Co-activation record dict
        ttl: Time-to-live in seconds (default: 7 days)
    """
    import json

    # Normalize order for consistent keys
    if node_1 > node_2:
        node_1, node_2 = node_2, node_1

    key = f"co_act:{user_id}:{node_1}:{node_2}"
    await set_cache(key, json.dumps(record), ttl=ttl)


async def get_all_co_activations(user_id: str) -> dict[tuple[str, str], dict]:
    """Get all co-activation records for a user (bulk load on startup).

    Args:
        user_id: User ID

    Returns:
        Dict mapping (node_1, node_2) to record dict
    """
    import json

    client = get_redis_client()
    pattern = f"co_act:{user_id}:*"
    records = {}

    try:
        async for key in client.scan_iter(match=pattern):
            parts = key.split(":")
            if len(parts) != 4:
                continue

            node_1, node_2 = parts[2], parts[3]
            value = await client.get(key)

            if value:
                records[(node_1, node_2)] = json.loads(value)

        logger.info("co_activations_loaded", user_id=user_id, count=len(records))
        return records

    except Exception as e:
        logger.error("co_activation_load_failed", user_id=user_id, error=str(e))
        return {}


async def _delete_keys_by_patterns(
    patterns: list[str],
    *,
    log_event: str,
    **log_kw: object,
) -> int:
    """SCAN-and-delete every key matching any pattern. Returns deletion count.

    Used by GDPR-erasure helpers that need to wipe per-user key namespaces.
    Best-effort: returns 0 on failure rather than raising, matching the
    discipline that Postgres is the source of truth.

    Batches keys into a single ``DELETE k1 k2 ...`` per 500-key chunk
    rather than one ``DELETE`` round-trip per key — Redis ``DEL`` accepts
    variadic keys.
    """
    client = get_redis_client()
    deleted = 0
    batch: list[str] = []

    async def _flush() -> int:
        nonlocal batch
        if not batch:
            return 0
        n = await client.delete(*batch)
        batch = []
        return n

    try:
        for pattern in patterns:
            async for key in client.scan_iter(match=pattern):
                batch.append(key)
                if len(batch) >= 500:
                    deleted += await _flush()
        deleted += await _flush()
        logger.info(log_event, deleted=deleted, **log_kw)
        return deleted
    except Exception as e:
        logger.error(f"{log_event}_failed", error=str(e), **log_kw)
        return 0


async def clear_co_activations(user_id: str) -> int:
    """Clear all co-activation data for a user (GDPR compliance)."""
    return await _delete_keys_by_patterns(
        [f"co_act:{user_id}:*"],
        log_event="co_activations_cleared",
        user_id=user_id,
    )


async def clear_user_rate_limits(user_id: str) -> int:
    """Clear per-user rate-limit and quota counters for GDPR erasure.

    Covers ``rate_limit:user:{user_id}:*`` (per-minute burst counters) and
    ``quota:user:{user_id}:*`` (daily user-scoped quota counters — the
    rate_limit middleware writes these as ``quota:{scope_key}:{mode}:{date}``
    where ``scope_key = f"user:{user_id}"``, NOT the brace-wrapped form).

    Workspace-scoped quota keys (``quota:ws:{workspace_id}:*``) are NOT
    touched here — workspace IDs survive after the user's individual rows
    are gone and are not user-identifying.
    """
    return await _delete_keys_by_patterns(
        [
            f"rate_limit:user:{user_id}:*",
            f"quota:user:{user_id}:*",
        ],
        log_event="user_rate_limits_cleared",
        user_id=user_id,
    )
