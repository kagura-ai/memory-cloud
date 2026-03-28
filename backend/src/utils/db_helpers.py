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
from sqlalchemy.ext.asyncio import AsyncSession

from utils.logger import get_logger

logger = get_logger(__name__)

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
