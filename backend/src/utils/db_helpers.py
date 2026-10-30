"""Database helper utilities.

Provides common utilities for database operations including
transaction management and error handling.

Issue #106: Consolidate redundant code patterns
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import lru_cache, wraps
from typing import Any, ParamSpec, TypeVar

from fastapi import HTTPException, status
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from utils.logger import get_logger

logger = get_logger(__name__)

# SQLSTATE of the transaction Postgres aborts to break a lock cycle (#1985).
PG_DEADLOCK_DETECTED = "40P01"


def is_deadlock(exc: BaseException) -> bool:
    """Whether ``exc`` is a Postgres deadlock victim (SQLSTATE ``40P01``).

    asyncpg's ``DeadlockDetectedError`` reaches callers as a bare
    ``DBAPIError`` whose ``orig`` (the asyncpg adapter's error) carries
    ``sqlstate``; psycopg exposes ``pgcode``. The driver exception chained
    under ``orig`` is checked too. The aborted transaction is safe to retry.

    Args:
        exc: Any exception.

    Returns:
        True for a deadlock victim, False otherwise.
    """
    return _has_sqlstate(exc, PG_DEADLOCK_DETECTED)


def _has_sqlstate(exc: BaseException, sqlstate: str) -> bool:
    """Whether the driver error under ``exc`` (``exc.orig``, or the exception
    chained under it) carries ``sqlstate``."""
    if not isinstance(exc, DBAPIError):
        return False
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        if candidate is None:
            continue
        found = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if found == sqlstate:
            return True
    return False


# SQLSTATE of a lock request that could not be granted at once: a ``NOWAIT``
# lock held by another transaction, or a ``lock_timeout`` that ran out (#2418).
PG_LOCK_NOT_AVAILABLE = "55P03"


def is_lock_not_available(exc: BaseException) -> bool:
    """Whether ``exc`` is Postgres ``lock_not_available`` (SQLSTATE ``55P03``).

    Read like :func:`is_deadlock` (asyncpg ``sqlstate``, psycopg ``pgcode``,
    on ``exc.orig`` or the driver exception chained under it). The failed
    transaction is aborted and must be rolled back; nothing it did is
    committed.

    Args:
        exc: Any exception.

    Returns:
        True for a lock that was not available, False otherwise.
    """
    return _has_sqlstate(exc, PG_LOCK_NOT_AVAILABLE)


P = ParamSpec("P")
T = TypeVar("T")


@lru_cache(maxsize=128)
def _get_param_index(func_id: int, param_name: str, param_names: tuple[str, ...]) -> int | None:
    """Get cached parameter index for a function.

    Args:
        func_id: id() of the function (for cache key)
        param_name: Name of the parameter to find
        param_names: Tuple of parameter names (hashable for cache)

    Returns:
        Index of the parameter or None if not found
    """
    if param_name in param_names:
        return param_names.index(param_name)
    return None


@asynccontextmanager
async def db_transaction(
    db: AsyncSession,
    operation_name: str = "database operation",
    error_message: str | None = None,
):
    """Context manager for database transactions with error handling.

    Automatically handles:
    - HTTPException passthrough (no rollback needed)
    - Generic exceptions: rollback + log + raise HTTPException 500

    Args:
        db: Database session
        operation_name: Name of operation for logging
        error_message: Custom error message for HTTPException (default: generic)

    Yields:
        None

    Example:
        >>> async with db_transaction(db, "create_user", "Failed to create user"):
        ...     user = User(name="test")
        ...     db.add(user)
        ...     await db.commit()
    """
    try:
        yield
    except HTTPException:
        # Let HTTPException pass through without rollback
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"{operation_name}_failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_message or f"Failed to complete {operation_name}",
        ) from e


def with_db_transaction(
    operation_name: str = "database operation",
    error_message: str | None = None,
    db_param: str = "db",
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator for database transactions with error handling.

    Wraps async functions that use database sessions with automatic
    error handling and rollback.

    Args:
        operation_name: Name of operation for logging
        error_message: Custom error message for HTTPException
        db_param: Name of the db parameter in the decorated function

    Returns:
        Decorator function

    Example:
        >>> @with_db_transaction("create_user", "Failed to create user")
        ... async def create_user(db: AsyncSession, name: str):
        ...     user = User(name=name)
        ...     db.add(user)
        ...     await db.commit()
        ...     return user
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        # Cache signature inspection at decoration time
        sig = inspect.signature(func)
        param_names = tuple(sig.parameters.keys())

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # Get db session from kwargs or args
            db = kwargs.get(db_param)
            if db is None:
                # Use cached parameter index lookup
                idx = _get_param_index(id(func), db_param, param_names)
                if idx is not None and idx < len(args):
                    db = args[idx]

            if db is None:
                # If we can't find db, just run the function normally
                return await func(*args, **kwargs)

            async with db_transaction(db, operation_name, error_message):
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


async def execute_with_rollback(
    db: AsyncSession,
    operation: Callable[[], Any],
    operation_name: str = "database operation",
    error_message: str | None = None,
) -> Any:
    """Execute an operation with automatic rollback on failure.

    Args:
        db: Database session
        operation: Async callable to execute
        operation_name: Name for logging
        error_message: Custom error message

    Returns:
        Result of the operation

    Example:
        >>> async def create_user():
        ...     user = User(name="test")
        ...     db.add(user)
        ...     await db.commit()
        ...     return user
        >>> user = await execute_with_rollback(db, create_user, "create_user")
    """
    async with db_transaction(db, operation_name, error_message):
        return await operation()
