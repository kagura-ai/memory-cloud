"""Authentication helper utilities.

Provides common utilities for extracting user information from
various authentication sources (OAuth2 session, API key, etc.).

Issue #106: Consolidate redundant code patterns
"""

from __future__ import annotations

from typing import Any, Protocol, TypeGuard, runtime_checkable

from fastapi import HTTPException, status


@runtime_checkable
class UserLike(Protocol):
    """Protocol for user-like objects (ORM User, etc.)."""

    id: str
    email: str | None
    role: str


# Type alias for authenticated user (dict from session or UserLike object)
AuthenticatedUser = dict[str, Any] | UserLike


def _is_user_like(obj: Any) -> TypeGuard[UserLike]:
    """Type guard to check if object is UserLike.

    Uses TypeGuard for proper static type narrowing.
    """
    return hasattr(obj, "id") and hasattr(obj, "email") and hasattr(obj, "role")


def get_user_id(current_user: AuthenticatedUser) -> str:
    """Extract user ID from authenticated user object.

    Handles multiple authentication sources:
    - OAuth2 session: dict with "user_id" or "sub" key
    - API Key auth: dict with "user_id" key
    - Direct User ORM object

    Args:
        current_user: Authenticated user from dependency injection.
            Can be dict (from session/API key) or User ORM object.

    Returns:
        User ID string

    Raises:
        HTTPException: 401 if user ID cannot be extracted

    Example:
        >>> user = {"user_id": "abc123", "email": "user@example.com"}
        >>> get_user_id(user)
        'abc123'

        >>> user = User(id="abc123", email="user@example.com")
        >>> get_user_id(user)
        'abc123'
    """
    if isinstance(current_user, dict):
        # Try multiple keys (OAuth2 session uses 'sub', API key uses 'user_id')
        # Use explicit None check to handle falsy values like 0
        for key in ("user_id", "sub", "id"):
            user_id = current_user.get(key)
            if user_id is not None:
                return str(user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID not found in session",
        )

    # UserLike object (ORM User, etc.)
    if _is_user_like(current_user):
        return str(current_user.id)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Cannot extract user ID from {type(current_user).__name__}",
    )


def get_user_email(current_user: AuthenticatedUser) -> str | None:
    """Extract user email from authenticated user object.

    Args:
        current_user: Authenticated user from dependency injection

    Returns:
        User email or None if not available
    """
    if isinstance(current_user, dict):
        return current_user.get("email")

    if _is_user_like(current_user):
        return current_user.email

    return None


def get_user_role(current_user: AuthenticatedUser) -> str:
    """Extract user role from authenticated user object.

    Args:
        current_user: Authenticated user from dependency injection

    Returns:
        User role (default: "user")
    """
    if isinstance(current_user, dict):
        return current_user.get("role", "user")

    if _is_user_like(current_user):
        return current_user.role

    return "user"


def is_admin(current_user: AuthenticatedUser) -> bool:
    """Check if user has admin role.

    Args:
        current_user: Authenticated user from dependency injection

    Returns:
        True if user has admin role
    """
    return get_user_role(current_user) == "admin"


def verify_ownership(
    resource_user_id: str | None,
    current_user: AuthenticatedUser,
    resource_name: str = "resource",
    allow_admin: bool = True,
) -> None:
    """Verify that the current user owns the resource.

    Args:
        resource_user_id: User ID of the resource owner
        current_user: Authenticated user from dependency injection
        resource_name: Name of resource for error message
        allow_admin: If True, admins can access any resource

    Raises:
        HTTPException: 404 if resource not found or not owned by user

    Example:
        >>> # In a route handler
        >>> memory = await get_memory(memory_id)
        >>> verify_ownership(memory.user_id, current_user, "memory")
    """
    if resource_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_name.capitalize()} not found",
        )

    user_id = get_user_id(current_user)

    # Admin bypass
    if allow_admin and is_admin(current_user):
        return

    if resource_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_name.capitalize()} not found or not owned by you",
        )
