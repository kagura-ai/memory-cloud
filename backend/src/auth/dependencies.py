"""FastAPI dependencies for authentication and authorization.

Based on: kagura-ai/src/kagura/api/dependencies.py
Issue #82: Context-based multi-collection support
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import VerifiedKey
from auth.oauth2_bearer import is_oauth_bearer_token, verify_oauth_bearer_token
from db.base import get_db
from models.auth import User
from utils.exceptions import AuthorizationError
from utils.logger import get_logger

# HTTP method → required OAuth scope. GET/HEAD/OPTIONS → memory:read;
# mutating verbs → memory:write. Routes that need finer enforcement
# (admin-only, etc.) layer their own scope check on top.
_METHOD_REQUIRED_SCOPE: dict[str, str] = {
    "GET": "memory:read",
    "HEAD": "memory:read",
    "OPTIONS": "memory:read",
    "POST": "memory:write",
    "PUT": "memory:write",
    "PATCH": "memory:write",
    "DELETE": "memory:write",
}


def _required_scope_for_method(method: str) -> str:
    """Return the OAuth scope required for an HTTP method.

    Unknown methods fail closed to ``memory:write`` so a future verb
    cannot accidentally bypass enforcement.
    """
    return _METHOD_REQUIRED_SCOPE.get(method.upper(), "memory:write")


def _scope_granted(granted: str | None, required: str) -> bool:
    """Check whether ``required`` is present in a space-separated scope string.

    RFC 6749 §3.3: scope is a space-delimited list of case-sensitive
    strings. Membership test only — there is no hierarchy (``memory:admin``
    does NOT imply ``memory:write``); routes that want admin-level access
    must require ``memory:admin`` explicitly.
    """
    if not granted:
        return False
    return required in granted.split()


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


async def verify_api_key(api_key: str) -> VerifiedKey | None:
    """Verify API key and return a ``VerifiedKey`` view.

    Issue #169: ``workspace_id`` for workspace-scoped API keys.
    Issue #626: ``bound_context_id`` for public-bound API keys (REJECTED
    here — see below).
    Migration 034: Removed context_id (deprecated).

    Standalone function for MCP authentication (non-FastAPI context).
    This function does not use FastAPI dependencies, making it suitable
    for use in MCP server authentication flows.

    **Public-bound key rejection (#626)**: A key with
    ``bound_context_id != None`` is intended ONLY for the REST public
    endpoint (`/api/v1/public/{ctx}/*` via ``_resolve_public_attribution``)
    and MUST NOT authenticate any other surface. Returning ``None`` here
    treats such a key as "not authenticated" for MCP and any other
    consumer of this standalone function — preventing a public-bound
    key from being repurposed as a full-account API key (which would be
    a privilege escalation: the bound key carries the owner's
    workspace_id implicitly via the row, and downstream principal
    construction would otherwise grant it). The public endpoint reaches
    ``APIKeyManager.verify_key`` directly (not through this wrapper)
    and bypasses this guard.

    Args:
        api_key: API key to verify (e.g., "kagura_...")

    Returns:
        ``VerifiedKey`` if valid AND not public-bound; ``None`` if
        invalid/revoked/expired OR if the key is public-bound (#626).

    Example:
        result = await verify_api_key("kagura_1234567890abcdef...")
        if result:
            # API key is valid and not public-bound
            user_id = result.user_id
            ...
    """
    from auth.api_keys import APIKeyManager
    from db.base import get_db

    try:
        async for db in get_db():
            manager = APIKeyManager(db)
            verified = await manager.verify_key(api_key)
            if verified is None:
                return None
            if verified.bound_context_id is not None:
                # Public-bound key (#626) — reject from non-public surfaces.
                # The public endpoint calls APIKeyManager.verify_key directly
                # (not this wrapper) and is unaffected.
                logger.warning(
                    "public_bound_key_rejected_outside_public_endpoint",
                    bound_context_id=str(verified.bound_context_id),
                )
                return None
            # #945: this wrapper consumes get_db() via `async for ... return`,
            # so the early return raises GeneratorExit at get_db's yield and its
            # post-yield `await session.commit()` never runs — the last_used_at
            # flushed by verify_key would be rolled back. Commit explicitly.
            # Isolated try/except: a commit failure must NOT fail auth
            # (last_used_at is non-critical metadata, and the outer
            # `except Exception: return None` would otherwise reject a valid key).
            try:
                await db.commit()
            except Exception as commit_err:
                logger.warning("api_key_last_used_commit_failed", error=str(commit_err))
            return verified
    except Exception:
        # Silent failure - return None on any error
        return None
    return None


async def _build_api_key_user_dict(
    user_id: str,
    api_key_workspace_id: UUID | None,
    db: AsyncSession,
) -> dict:
    """Build user info dict from verified API key data.

    Shared by verify_api_key_user and get_user_from_api_key_or_session.

    Note (Issue #626): callers MUST reject public-bound keys
    (``VerifiedKey.bound_context_id is not None``) BEFORE invoking this
    helper. A public-bound key reaching this function would silently
    inherit the owner's ``current_workspace_id`` via
    ``_get_user_workspace_id``, granting the bound key full account
    access — a privilege escalation. The dependency wrappers above
    (``verify_api_key_user`` / ``get_user_from_api_key_or_session``)
    enforce this gate; the standalone ``verify_api_key`` returns ``None``
    for bound keys so MCP gets the same effective rejection.

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
        workspace_id=str(current_workspace_id) if current_workspace_id else None,
    )

    return {
        "user_id": user_id,
        "email": f"{user_id}@api",
        "role": "user",
        "current_context_id": None,  # Issue #246: always None, must be explicit
        "current_workspace_id": current_workspace_id,
        "api_key_workspace_id": api_key_workspace_id,
    }


async def _build_oauth_user_dict(
    user_id: str,
    granted_scope: str | None,
    db: AsyncSession,
) -> dict:
    """Build user info dict from a verified OAuth Bearer token.

    Mirrors the session-auth shape rather than the API-key shape: OAuth
    tokens are NOT workspace-bound (``OAuth2Token`` has no workspace_id
    column, and the workspace_id in the device-flow token response is
    point-in-time and explicitly not security-bearing per the comment
    in ``auth.oauth2_server``). Workspace gating is delegated downstream
    to ``PermissionService.check_workspace_access``, same as session
    auth. ``oauth_scope`` is attached so routes that want extra
    introspection (e.g. require ``memory:admin``) can layer it.
    """
    current_workspace_id = await _get_user_workspace_id(user_id, db)

    logger.info(
        "oauth_bearer_authenticated",
        user_id=user_id,
        workspace_id=str(current_workspace_id) if current_workspace_id else None,
        scope=granted_scope,
    )

    return {
        "user_id": user_id,
        "email": f"{user_id}@oauth",
        "role": "user",
        "current_context_id": None,
        "current_workspace_id": current_workspace_id,
        "oauth_scope": granted_scope,
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

    # Issue #626: public-bound keys MUST NOT authenticate outside the
    # /api/v1/public/* attribution flow. Without this gate, a bound key
    # would inherit the owner's workspace_id via _build_api_key_user_dict
    # and grant full account access — privilege escalation.
    if result.bound_context_id is not None:
        logger.warning(
            "public_bound_key_rejected_outside_public_endpoint",
            bound_context_id=str(result.bound_context_id),
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "Public-bound API keys cannot authenticate here; "
                "they are only valid on /api/v1/public/{context_id}/* endpoints"
            ),
        )

    return await _build_api_key_user_dict(result.user_id, result.workspace_id, db)


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
    # Web UI endpoints reject every Bearer credential — both ``kagura_*``
    # API keys and OAuth device-flow tokens carry programmatic / CLI
    # semantics, and UI-only flows (billing checkout, MFA) must require
    # a real browser session. The event key stays ``api_key_rejected_for_web_ui``
    # for dashboard continuity since #252; OAuth-vs-API-key is surfaced
    # via the ``token_kind`` field instead of a new event name.
    if api_key:
        logger.warning(
            "api_key_rejected_for_web_ui",
            path=request.url.path,
            token_kind="oauth" if is_oauth_bearer_token(api_key) else "api_key",
            token_prefix=api_key[:8],
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "Bearer tokens (API keys or OAuth) are not allowed for Web UI endpoints. "
                "Use browser session authentication."
            ),
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
    # Priority 0: OAuth Bearer. The prefix check skips the OAuth DB
    # lookup for ``kagura_*`` API keys (and vice versa), so each Bearer
    # request hits only one of the two stores.
    if api_key and is_oauth_bearer_token(api_key):
        result = await verify_oauth_bearer_token(api_key, db)
        if result is None:
            logger.warning("invalid_oauth_bearer_attempt", token_prefix=api_key[:8])
            raise HTTPException(status_code=401, detail="Invalid or expired Bearer token")

        user_id, granted_scope = result

        # Scope enforcement (RFC 6750 §3): map HTTP method → required scope,
        # 403 with ``WWW-Authenticate: Bearer error="insufficient_scope"``
        # on miss so SDK clients can recover via the upgrade flow.
        required = _required_scope_for_method(request.method)
        if not _scope_granted(granted_scope, required):
            logger.warning(
                "oauth_bearer_insufficient_scope",
                user_id=user_id,
                required=required,
                granted=granted_scope,
                method=request.method,
                path=request.url.path,
            )
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient scope: '{required}' required",
                headers={
                    "WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{required}"'
                },
            )

        return await _build_oauth_user_dict(user_id, granted_scope, db)

    # Priority 1: API Key authentication
    if api_key:
        from auth.api_keys import APIKeyManager

        manager = APIKeyManager(db)
        result = await manager.verify_key(api_key)

        if result:
            # Issue #626: reject public-bound keys here too — same rationale
            # as verify_api_key_user above. The public endpoint reaches
            # APIKeyManager.verify_key directly and bypasses this gate.
            if result.bound_context_id is not None:
                logger.warning(
                    "public_bound_key_rejected_outside_public_endpoint",
                    bound_context_id=str(result.bound_context_id),
                )
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Public-bound API keys cannot authenticate here; "
                        "they are only valid on /api/v1/public/{context_id}/* endpoints"
                    ),
                )
            return await _build_api_key_user_dict(result.user_id, result.workspace_id, db)

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
    except AuthorizationError as exc:
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
            reason=exc.reason,
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
    except AuthorizationError as exc:
        logger.warning(
            "workspace_admin_session_denied",
            user_id=user_id,
            workspace_id=str(workspace_id),
            status_code=exc.status_code,
            reason=exc.reason,
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
    except AuthorizationError as exc:
        # WARN on deny so audit pipelines can surface workspace-admin violations
        # (same audit-log pattern as require_workspace_owner — Issue #389 gate1).
        logger.warning(
            "workspace_admin_denied",
            user_id=user_id,
            workspace_id=str(workspace_id),
            status_code=exc.status_code,
            reason=exc.reason,
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
