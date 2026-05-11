"""FastAPI dependencies for authentication and authorization.

Based on: kagura-ai/src/kagura/api/dependencies.py
Issue #82: Context-based multi-collection support
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from models.auth import User
from utils.exceptions import AuthorizationError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


# ============================================================================
# Context Helper (Issue #82)
# ============================================================================


# Issue #246: get_user_current_context_id() and _resolve_context_id() removed
# Context is now always None (must be explicit from API parameter or Frontend URL)


async def _get_user_workspace_id(
    user_id: str,
    db: AsyncSession,
) -> UUID | None:
    """Get user's current workspace ID.

    Issue #146: Workspace-scoped API keys support.

    Args:
        user_id: User ID (varchar, e.g., Google OAuth2 ID)
        db: Database session

    Returns:
        Workspace UUID or None if not set
    """
    result = await db.execute(select(User.current_workspace_id).where(User.user_id == user_id))
    workspace_id = result.scalar_one_or_none()
    return workspace_id


# ============================================================================
# Session-based Authentication (OAuth2)
# ============================================================================


async def get_current_user(request: Request) -> dict:
    """Get current authenticated user from session.

    Dependency that extracts user from request.state (injected by SessionMiddleware).
    Authentication is always required.

    Args:
        request: FastAPI request (with SessionMiddleware applied)

    Returns:
        User info dict with email, user_id, role

    Raises:
        HTTPException: 401 if not authenticated

    Example:
        @app.get("/me")
        async def get_me(user: dict = Depends(get_current_user)):
            return user
    """
    # SessionMiddleware sets request.state.user if logged in
    if not hasattr(request.state, "user") or request.state.user is None:
        logger.warning("unauthorized_access_attempt", path=request.url.path)
        raise HTTPException(status_code=401, detail="Not authenticated")

    return request.state.user


async def get_current_user_optional(request: Request) -> dict | None:
    """Get current user or None if not authenticated.

    Args:
        request: FastAPI request

    Returns:
        User info dict or None

    Example:
        @app.get("/public")
        async def public_endpoint(user: dict | None = Depends(get_current_user_optional)):
            if user:
                return {"message": f"Hello {user['email']}"}
            return {"message": "Hello anonymous"}
    """
    return getattr(request.state, "user", None)


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Require ADMIN role.

    Args:
        user: Current user (from get_current_user)

    Returns:
        User info dict

    Raises:
        HTTPException: 403 if not admin

    Example:
        @app.delete("/admin/users/{user_id}")
        async def delete_user(user: dict = Depends(require_admin)):
            # Only admins can access
            ...
    """
    if user.get("role") != "admin":
        logger.warning(
            "unauthorized_admin_access",
            user_email=user.get("email"),
            user_role=user.get("role"),
        )
        raise HTTPException(status_code=403, detail="Admin role required")

    return user


# ============================================================================
# API Key Authentication (Bearer Token)
# ============================================================================


async def get_api_key(authorization: str | None = Header(None)) -> str | None:
    """Extract API key from Authorization header.

    Args:
        authorization: Authorization header value

    Returns:
        API key or None

    Example:
        Authorization: Bearer kagura_1234567890abcdef...
    """
    if not authorization:
        return None

    if not authorization.startswith("Bearer "):
        return None

    return authorization[7:]  # Remove "Bearer " prefix


async def verify_api_key(api_key: str) -> tuple[str, UUID | None] | None:
    """Verify API key and return (user_id, workspace_id).

    Issue #169: Returns workspace_id for workspace-scoped API keys.
    Migration 034: Removed context_id (deprecated).

    Standalone function for MCP authentication (non-FastAPI context).
    This function does not use FastAPI dependencies, making it suitable
    for use in MCP server authentication flows.

    Args:
        api_key: API key to verify (e.g., "kagura_...")

    Returns:
        (user_id, workspace_id) tuple if valid, None if invalid/revoked/expired.
        - For workspace-scoped keys: workspace_id=<UUID>
        - For global keys: workspace_id=None

    Example:
        result = await verify_api_key("kagura_1234567890abcdef...")
        if result:
            user_id, workspace_id = result
            # API key is valid
            ...
    """
    from auth.api_keys import APIKeyManager
    from db.base import get_db

    try:
        async for db in get_db():
            manager = APIKeyManager(db)
            result = await manager.verify_key(api_key)  # Returns 3-tuple
            return result
    except Exception:
        # Silent failure - return None on any error
        return None


async def _build_api_key_user_dict(
    user_id: str,
    api_key_workspace_id: UUID | None,
    db: AsyncSession,
) -> dict:
    """Build user info dict from verified API key data.

    Shared by verify_api_key_user and get_user_from_api_key_or_session.

    Args:
        user_id: Authenticated user ID
        api_key_workspace_id: Workspace UUID from API key scope (None for global keys)
        db: Database session

    Returns:
        User info dict compatible with get_current_user format
    """
    if api_key_workspace_id:
        current_workspace_id = api_key_workspace_id
    else:
        current_workspace_id = await _get_user_workspace_id(user_id, db)

    logger.info(
        "api_key_authenticated",
        user_id=user_id,
        workspace_id=str(current_workspace_id),
    )

    return {
        "user_id": user_id,
        "email": f"{user_id}@api",
        "role": "user",
        "current_context_id": None,  # Issue #246: always None, must be explicit
        "current_workspace_id": current_workspace_id,
        "api_key_workspace_id": api_key_workspace_id,
    }


async def verify_api_key_user(
    api_key: str | None = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verify API key and return user info. Raises 401 if missing/invalid.

    Example:
        @app.get("/api/v1/memories")
        async def list_memories(user: dict = Depends(verify_api_key_user)):
            ...
    """
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    from auth.api_keys import APIKeyManager

    manager = APIKeyManager(db)
    result = await manager.verify_key(api_key)

    if not result:
        logger.warning("invalid_api_key_attempt", key_prefix=api_key[:16] if api_key else None)
        raise HTTPException(status_code=401, detail="Invalid or expired API key")

    user_id, api_key_workspace_id = result
    return await _build_api_key_user_dict(user_id, api_key_workspace_id, db)


async def require_session_auth(
    request: Request,
    api_key: str | None = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Require session authentication only (no API keys).

    Issue #252: For Web UI endpoints that should only be accessed via browser sessions,
    not programmatic API keys. This is a security enhancement to separate concerns:
    - Web UI endpoints: Session-only (this dependency)
    - MCP endpoints: API Key + Session (get_user_from_api_key_or_session)

    Args:
        request: FastAPI request
        api_key: API key from Authorization header (should be None)
        db: Database session

    Returns:
        User info dict from session with current_workspace_id

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if API key is provided (not allowed for Web UI)

    Example:
        @router.get("/contexts")
        async def list_contexts(user: dict = Depends(require_session_auth)):
            # Only accessible via browser session
            ...
    """
    # Reject API key authentication for Web UI endpoints
    if api_key:
        logger.warning(
            "api_key_rejected_for_web_ui",
            path=request.url.path,
            key_prefix=api_key[:16] if api_key else None,
        )
        raise HTTPException(
            status_code=403,
            detail="API keys are not allowed for Web UI endpoints. Use browser session authentication.",
        )

    # Require session authentication
    user = await get_current_user(request)

    # Add current workspace ID for session users
    if user.get("user_id"):
        current_workspace_id = await _get_user_workspace_id(user["user_id"], db)
        user["current_workspace_id"] = current_workspace_id

    return user


async def get_user_from_api_key_or_session(
    request: Request,
    api_key: str | None = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | UUID | None]:
    """Get user from API key (priority) or session authentication.

    This dependency tries API key authentication first. If no API key is provided,
    it falls back to session-based authentication (OAuth2).

    Issue #82: Now includes current_context_id for context-based multi-collection support.

    Args:
        request: FastAPI request
        api_key: API key from Authorization header (optional)
        db: Database session

    Returns:
        User info dict with:
        - user_id: User ID
        - email: User email
        - role: User role
        - current_context_id: Current context UUID or None (Issue #82)

    Raises:
        HTTPException: 401 if not authenticated

    Example:
        @app.post("/api/v1/memory/remember")
        async def remember(user: dict = Depends(get_user_from_api_key_or_session)):
            context_id = user.get("current_context_id")
            return await save_memory(user["user_id"], context_id, ...)
    """
    # Priority 1: API Key authentication
    if api_key:
        from auth.api_keys import APIKeyManager

        manager = APIKeyManager(db)
        result = await manager.verify_key(api_key)

        if result:
            user_id, api_key_workspace_id = result
            return await _build_api_key_user_dict(user_id, api_key_workspace_id, db)

        logger.warning("invalid_api_key_attempt", key_prefix=api_key[:16])
        raise HTTPException(status_code=401, detail="Invalid or expired API key")

    # Priority 2: Session-based authentication (OAuth2)
    user = await get_current_user(request)

    # Issue #146: Add current workspace ID for session users
    # Issue #246: current_context_id removed (always None, must be explicit from Frontend)
    if user.get("user_id"):
        current_workspace_id = await _get_user_workspace_id(user["user_id"], db)
        user["current_workspace_id"] = current_workspace_id

    return user


# ============================================================================
# Workspace Owner (API Key + Session)
# ============================================================================


async def require_workspace_owner(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
) -> tuple[str, UUID]:
    """Verify user is workspace owner and return (user_id, workspace_id).

    Issue #276: DRY principle - consolidated workspace owner verification.
    Accepts both session auth and API key auth (for SDK/CI automation).

    Args:
        user: Current authenticated user (session or API key)
        db: Database session

    Returns:
        Tuple of (user_id, workspace_id)

    Raises:
        HTTPException: 400 if no workspace selected
        HTTPException: 403 if not workspace owner
    """
    user_id = user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400, detail="No workspace selected. Please select a workspace first."
        )

    # Check owner permission
    from services.permission_service import PermissionService

    perm_service = PermissionService(db)
    try:
        await perm_service.check_workspace_owner(user_id, workspace_id)
    except (NotFoundException, AuthorizationError) as exc:
        # WARN on deny so audit pipelines can surface workspace-owner violations
        # (#389 gate1 review). The structured ``reason`` (workspace_deleted /
        # not_a_member / role_too_low) is the actionable classification —
        # exc.message is the uniform "Insufficient permissions" string by
        # CWE-639 design, so logging it adds no signal.
        logger.warning(
            "workspace_owner_denied",
            user_id=user_id,
            workspace_id=str(workspace_id),
            status_code=exc.status_code,
            reason=exc.details.get("reason"),
        )
        raise

    logger.info("workspace_owner_verified", user_id=user_id, workspace_id=str(workspace_id))

    return user_id, workspace_id


async def require_workspace_admin_session(
    user: dict = Depends(require_session_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verify user is workspace admin or owner — session auth only.

    Issue #398: billing checkout/portal use this variant so a leaked API key
    cannot initiate Stripe checkout sessions on behalf of an admin/owner.
    Mirrors require_workspace_admin but rejects API-key auth at the door
    (require_session_auth raises 403 for any Bearer token).

    Args:
        user: Current authenticated user (session only)
        db: Database session

    Returns:
        User info dict (full dict, like require_workspace_member)

    Raises:
        HTTPException: 400 if no workspace selected
        HTTPException: 403 if API key was provided OR if viewer/member
    """
    user_id = user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400, detail="No workspace selected. Please select a workspace first."
        )

    from services.permission_service import PermissionService

    perm_service = PermissionService(db)
    try:
        await perm_service.check_workspace_admin(user_id, workspace_id)
    except (NotFoundException, AuthorizationError) as exc:
        logger.warning(
            "workspace_admin_session_denied",
            user_id=user_id,
            workspace_id=str(workspace_id),
            status_code=exc.status_code,
            reason=exc.details.get("reason"),
        )
        raise

    logger.info("workspace_admin_session_verified", user_id=user_id, workspace_id=str(workspace_id))

    return user


async def require_workspace_admin(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verify user is workspace admin or owner.

    Issue #398: admin-level operations that can be invoked via session OR API
    key. For UI-only actions where API-key auth would be inappropriate (e.g.
    billing checkout), use require_workspace_admin_session instead.
    Mirrors require_workspace_owner but also accepts the 'admin' role.
    Accepts both session auth and API key auth.

    Args:
        user: Current authenticated user (session or API key)
        db: Database session

    Returns:
        User info dict (full dict, like require_workspace_member)

    Raises:
        HTTPException: 400 if no workspace selected
        HTTPException: 403 if viewer/member (not admin or owner)
    """
    user_id = user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400, detail="No workspace selected. Please select a workspace first."
        )

    from services.permission_service import PermissionService

    perm_service = PermissionService(db)
    try:
        await perm_service.check_workspace_admin(user_id, workspace_id)
    except (NotFoundException, AuthorizationError) as exc:
        # WARN on deny so audit pipelines can surface workspace-admin violations
        # (same audit-log pattern as require_workspace_owner — Issue #389 gate1).
        logger.warning(
            "workspace_admin_denied",
            user_id=user_id,
            workspace_id=str(workspace_id),
            status_code=exc.status_code,
            reason=exc.details.get("reason"),
        )
        raise

    logger.info("workspace_admin_verified", user_id=user_id, workspace_id=str(workspace_id))

    return user


async def require_workspace_member(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verify user has at least member role (excludes viewers).

    Issue #59: Viewers should not access certain workspace features.
    Accepts both session auth and API key auth.
    Returns the full user dict (unlike WorkspaceOwner which returns a tuple).

    Args:
        user: Current authenticated user (session or API key)
        db: Database session

    Returns:
        User info dict (same as APIKeyOrSessionUser)

    Raises:
        HTTPException: 400 if no workspace selected
        HTTPException: 403 if viewer or not a member
    """
    user_id = user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400, detail="No workspace selected. Please select a workspace first."
        )

    from services.permission_service import PermissionService

    perm_service = PermissionService(db)
    await perm_service.check_workspace_access(user_id, workspace_id, required_role="member")

    return user


# ============================================================================
# Type Aliases
# ============================================================================

CurrentUser = Annotated[dict, Depends(get_current_user)]
CurrentUserOptional = Annotated[dict | None, Depends(get_current_user_optional)]
AdminUser = Annotated[dict, Depends(require_admin)]
APIKeyUser = Annotated[dict, Depends(verify_api_key_user)]
APIKeyOrSessionUser = Annotated[dict, Depends(get_user_from_api_key_or_session)]
SessionUser = Annotated[dict, Depends(require_session_auth)]  # Issue #252
WorkspaceOwner = Annotated[tuple[str, UUID], Depends(require_workspace_owner)]  # Issue #276
WorkspaceAdmin = Annotated[dict, Depends(require_workspace_admin)]  # Issue #398
WorkspaceAdminSession = Annotated[dict, Depends(require_workspace_admin_session)]  # Issue #398
WorkspaceMember = Annotated[dict, Depends(require_workspace_member)]  # Issue #59
