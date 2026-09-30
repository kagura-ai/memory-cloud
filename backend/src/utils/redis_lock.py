"""A short-lived Redis lock: ``SET NX EX`` with a token, released by
compare-and-delete.

The token makes the release safe: a holder whose lock expired (and was taken
by someone else) cannot delete the new holder's lock. The release is one
WATCH / MULTI transaction rather than a Lua script, so it also runs against
Redis stand-ins without scripting.
"""

from __future__ import annotations

import secrets
from typing import Any


async def acquire_lock(client: Any, key: str, ttl_seconds: int) -> str | None:
    """Take the lock.

    Args:
        client: Async Redis client.
        key: The lock key.
        ttl_seconds: After this long the lock frees itself.

    Returns:
        The token to release it with, or ``None`` when someone else holds it.
    """
    token = secrets.token_hex(16)
    if await client.set(key, token, nx=True, ex=ttl_seconds):
        return token
    return None


async def release_lock(client: Any, key: str, token: str) -> bool:
    """Delete the lock if ``token`` still holds it.

    Args:
        client: Async Redis client.
        key: The lock key.
        token: The token :func:`acquire_lock` returned.

    Returns:
        True when the lock was deleted; False when it had expired or belongs
        to another holder (including one that took it during this call).
    """
    from redis.exceptions import WatchError

    async with client.pipeline(transaction=True) as pipe:
        await pipe.watch(key)
        held = await pipe.get(key)
        if isinstance(held, bytes):
            held = held.decode()
        if held != token:
            await pipe.unwatch()
            return False
        pipe.multi()
        pipe.delete(key)
        try:
            await pipe.execute()
        except WatchError:
            return False
    return True
