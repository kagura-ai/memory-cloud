"""Redis client for cache and session management.

Singleton pattern for connection pooling.
"""

import redis.asyncio as aioredis

from config.database import REDIS_URL
from utils.exceptions import RedisError
from utils.logger import get_logger
from utils.url_redact import redact_generic_url

logger = get_logger(__name__)

# Singleton Redis client
_redis_client: aioredis.Redis | None = None


def get_redis_client() -> aioredis.Redis:
    """Get singleton Redis client.

    Returns:
        Redis client instance

    Raises:
        RedisError: If connection fails
    """
    global _redis_client

    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(
                REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                max_connections=10,
            )
            logger.info("redis_client_initialized", url=redact_generic_url(REDIS_URL))
        except Exception as e:
            raise RedisError(f"Failed to connect to Redis: {e}") from e

    return _redis_client


async def close_redis() -> None:
    """Close Redis connection."""
    global _redis_client

    if _redis_client:
        await _redis_client.close()
        _redis_client = None
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
        return await client.get(key)
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
        count = await client.incr(key)

        # Set TTL if this is the first increment
        if count == 1 and ttl:
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
