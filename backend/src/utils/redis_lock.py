"""A short-lived Redis lock: ``SET NX EX`` with a token, released by
compare-and-delete.

The token makes the release safe: a holder whose lock expired (and was taken
by someone else) cannot delete the new holder's lock. The release is one
WATCH / MULTI transaction rather than a Lua script, so it also runs against
Redis stand-ins without scripting.

``acquire_lock_sync`` / ``release_lock_sync`` are the same lock for a
synchronous client (the session store's). ``watch_token_sync`` is the
compare half of the release on its own, for a caller that wants its own
writes in the transaction (#1918): they then land only if the key still
holds the token.
"""

from __future__ import annotations

import secrets
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.exceptions import WatchError


def connection_failure(exc: WatchError) -> BaseException | None:
    """The connection or timeout failure redis-py reported as ``exc``, if any.

    redis-py raises ``WatchError`` for a connection or timeout failure while
    a key is watched too (the watch cannot survive a new connection), with
    the failure as the error's context. That one is a Redis failure, not a
    key that changed: the transaction may or may not have run.

    Args:
        exc: The ``WatchError`` an EXEC raised.

    Returns:
        The underlying failure to raise instead, or ``None`` when the watched
        key did change.
    """
    failure = exc.__context__
    if isinstance(failure, RedisConnectionError | RedisTimeoutError):
        return failure
    return None


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


async def watch_token(pipe: Any, key: str, token: str) -> bool:
    """WATCH ``key`` and open the transaction only if it holds ``token``.

    Args:
        pipe: A transaction pipeline, not yet in MULTI.
        key: The key to watch.
        token: The value it must hold.

    Returns:
        True with the pipeline in MULTI: queue the writes and EXEC. False
        with the key unwatched: it is gone or holds another value.
    """
    await pipe.watch(key)
    held = await pipe.get(key)
    if isinstance(held, bytes):
        held = held.decode()
    if held != token:
        await pipe.unwatch()
        return False
    pipe.multi()
    return True


async def release_lock(client: Any, key: str, token: str) -> bool:
    """Delete the lock if ``token`` still holds it.

    Args:
        client: Async Redis client.
        key: The lock key.
        token: The token :func:`acquire_lock` returned.

    Returns:
        True when the lock was deleted; False when it had expired or belongs
        to another holder (including one that took it during this call).

    Raises:
        Exception: A Redis connection or timeout failure, as it is (not as
            ``WatchError``): the lock is then left to its TTL.
    """
    async with client.pipeline(transaction=True) as pipe:
        if not await watch_token(pipe, key, token):
            return False
        pipe.delete(key)
        try:
            await pipe.execute()
        except WatchError as exc:
            failure = connection_failure(exc)
            if failure is not None:
                raise failure from exc
            return False
    return True


def acquire_lock_sync(client: Any, key: str, ttl_seconds: int) -> str | None:
    """:func:`acquire_lock` for a synchronous Redis client.

    Args:
        client: Sync Redis client.
        key: The lock key.
        ttl_seconds: After this long the lock frees itself.

    Returns:
        The token to release it with, or ``None`` when someone else holds it.
    """
    token = secrets.token_hex(16)
    if client.set(key, token, nx=True, ex=ttl_seconds):
        return token
    return None


def watch_token_sync(pipe: Any, key: str, token: str) -> bool:
    """:func:`watch_token` for a synchronous pipeline.

    Args:
        pipe: A transaction pipeline, not yet in MULTI.
        key: The key to watch.
        token: The value it must hold.

    Returns:
        True with the pipeline in MULTI: queue the writes and EXEC. False
        with the key unwatched: it is gone or holds another value.
    """
    pipe.watch(key)
    held = pipe.get(key)
    if isinstance(held, bytes):
        held = held.decode()
    if held != token:
        pipe.unwatch()
        return False
    pipe.multi()
    return True


def release_lock_sync(client: Any, key: str, token: str) -> bool:
    """:func:`release_lock` for a synchronous Redis client.

    Args:
        client: Sync Redis client.
        key: The lock key.
        token: The token :func:`acquire_lock_sync` returned.

    Returns:
        True when the lock was deleted; False when it had expired or belongs
        to another holder (including one that took it during this call).

    Raises:
        Exception: A Redis connection or timeout failure, as it is (not as
            ``WatchError``): the lock is then left to its TTL.
    """
    with client.pipeline(transaction=True) as pipe:
        if not watch_token_sync(pipe, key, token):
            return False
        pipe.delete(key)
        try:
            pipe.execute()
        except WatchError as exc:
            failure = connection_failure(exc)
            if failure is not None:
                raise failure from exc
            return False
    return True
