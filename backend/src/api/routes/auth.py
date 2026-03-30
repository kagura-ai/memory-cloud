"""OAuth2 Web Login endpoints for Kagura Memory Cloud.

Issue #650 - Google OAuth2 Web Login & API Key Management
Issue #115 - Cookie name changed from 'session_id' to 'kagura_session'
Issue #315 - GitHub OAuth2 Authentication

Provides web-based OAuth2 authentication flow:
1. GET /auth/google/login - Redirect to Google OAuth2
2. GET /auth/google/callback - Handle Google callback
3. GET /auth/github/login - Redirect to GitHub OAuth2
4. GET /auth/github/callback - Handle GitHub callback
5. POST /auth/logout - Delete session and logout

Security features:
- CSRF protection via state parameter
- HttpOnly, Secure, SameSite cookies (kagura_session)
- Session stored in Redis
- First user auto-assigned ADMIN role
"""

import logging
import os
import secrets
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from auth.oauth2 import OAuth2Manager
from auth.roles import get_role_manager
from auth.session import SessionManager
from db.base import get_db
from models.auth import User
from services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])

# Google OAuth2 subrouter (provider-specific endpoints)
google_router = APIRouter(prefix="/google", tags=["authentication", "google-oauth2"])

# Global instances (initialized on server startup)
_oauth2_manager: OAuth2Manager | None = None
_session_manager: SessionManager | None = None


def initialize_auth_routes(oauth2_manager: OAuth2Manager, session_manager: SessionManager):
    """Initialize auth routes with managers.

    Args:
        oauth2_manager: OAuth2 manager instance
        session_manager: Session manager instance
    """
    global _oauth2_manager, _session_manager
    _oauth2_manager = oauth2_manager
    _session_manager = session_manager


# Models
class LoginResponse(BaseModel):
    """OAuth2 login response."""

    authorization_url: str
    state: str


class CallbackResponse(BaseModel):
    """OAuth2 callback response."""

    success: bool
    user_id: str
    email: str
    role: str
    message: str


# ============================================================================
# Registration Gate (Issue #349)
# ============================================================================


async def _check_registration_allowed(email: str) -> RedirectResponse | None:
    """Check if a new user is allowed to register.

    Returns None if allowed, RedirectResponse if blocked.
    Rules:
    - Existing users: always allowed (login, not registration)
    - First user (no users exist): always allowed (need initial admin)
    - ALLOW_REGISTRATION=true: always allowed
    - Pending invitation for this email: always allowed
    - Otherwise: blocked → redirect to login with error
    """
    from sqlalchemy import func, select

    from config.settings import get_settings
    from models.auth import User
    from services.invitation_service import InvitationService

    settings = get_settings()

    async for db in get_db():
        # Existing users can always login
        result = await db.execute(select(User).filter_by(email=email))
        if result.scalar_one_or_none():
            return None

        # First user always allowed
        user_count_result = await db.execute(select(func.count()).select_from(User))
        if user_count_result.scalar() == 0:
            return None

        # Registration open
        if settings.allow_registration:
            return None

        # Invited users bypass gate
        invitation_service = InvitationService(db)
        pending_invites = await invitation_service.get_pending_invitations_for_email(email=email)
        if pending_invites:
            return None

        # Block
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        logger.warning("registration_blocked: %s", email)
        return RedirectResponse(
            f"{frontend_url}/login?error=registration_disabled",
            status_code=303,
        )


# ============================================================================
# Provider Discovery (Issue #360)
# ============================================================================


class ProviderInfo(BaseModel):
    """OAuth provider info."""

    name: str


class ProvidersResponse(BaseModel):
    """Available OAuth providers."""

    providers: list[ProviderInfo]


@router.get("/providers", response_model=ProvidersResponse)
async def get_auth_providers() -> ProvidersResponse:
    """Return list of enabled OAuth providers for login page.

    Checks AUTH_PROVIDERS setting and configured credentials.
    Cached per process (env vars don't change at runtime).
    """
    return ProvidersResponse(providers=_get_enabled_providers())


def _get_enabled_providers() -> list[ProviderInfo]:
    """Compute enabled providers from env vars (cacheable)."""
    auth_providers_setting = os.getenv("AUTH_PROVIDERS", "auto").lower().strip()

    configured = {
        "google": bool(os.getenv("GOOGLE_CLIENT_ID")),
        "github": bool(os.getenv("GITHUB_CLIENT_ID")),
    }

    if auth_providers_setting != "auto":
        allowed = {p.strip() for p in auth_providers_setting.split(",")}
        configured = {k: v for k, v in configured.items() if k in allowed}

    return [ProviderInfo(name=k) for k, enabled in configured.items() if enabled]


# ============================================================================
# OAuth2 Endpoints
# ============================================================================


@google_router.get("/login")
async def google_login(
    redirect_uri: str | None = None,
    return_to: str | None = None,
):
    """Initiate Google OAuth2 login flow.

    Generates OAuth2 authorization URL with CSRF state token.

    Args:
        redirect_uri: Optional custom redirect URI (defaults to configured URI)
        return_to: If provided, auto-redirects browser to Google (for iOS/browser users)

    Returns:
        If return_to: RedirectResponse to Google OAuth (browser user)
        Otherwise: JSON {authorization_url, state} (API client)

    Example:
        GET /api/v1/auth/google/login (API client)
        Response: {"authorization_url": "...", "state": "..."}

        GET /api/v1/auth/google/login?return_to=... (browser user)
        Response: 303 Redirect to Google OAuth

    Note:
        Frontend should redirect user to authorization_url.
        State token is stored in Redis for CSRF validation.
    """
    from fastapi.responses import RedirectResponse

    if not _oauth2_manager:
        raise HTTPException(status_code=500, detail="OAuth2 manager not initialized")

    # Generate CSRF state token
    state = secrets.token_urlsafe(32)

    # Store state in Redis (5 minute TTL)
    if _session_manager:
        _session_manager._redis.setex(f"oauth2_state:{state}", 300, "pending")

        # Issue #102: Store return_to for later redirect after callback
        if return_to:
            _session_manager._redis.setex(f"oauth2_return_to:{state}", 300, return_to)

    # Get authorization URL
    redirect = redirect_uri or os.getenv("GOOGLE_REDIRECT_URI")
    if not redirect:
        raise HTTPException(status_code=500, detail="GOOGLE_REDIRECT_URI not configured")

    auth_url = _oauth2_manager.get_authorization_url_web(redirect, state)

    # Issue #102: Auto-redirect for browser/iOS users
    if return_to:
        return RedirectResponse(auth_url, status_code=303)
    else:
        # API client - return JSON
        return LoginResponse(authorization_url=auth_url, state=state)


@google_router.get("/callback")
async def google_callback(
    code: str = Query(..., description="OAuth2 authorization code"),
    state: str = Query(..., description="CSRF state token"),
):
    """Handle Google OAuth2 callback.

    Exchanges authorization code for access token, retrieves user info,
    creates session, and sets HttpOnly cookie.

    Args:
        code: OAuth2 authorization code from Google
        state: CSRF state token (must match stored state)
        response: FastAPI response object (for cookie setting)

    Returns:
        Redirect to dashboard with session cookie set

    Raises:
        HTTPException(400): Invalid state (CSRF attack)
        HTTPException(401): OAuth2 exchange failed
        HTTPException(500): Session creation failed

    Example:
        GET /api/v1/auth/google/callback?code=xxx&state=yyy
        → Sets cookie: kagura_session=...
        → Redirects to /dashboard

    Security:
        - State validation (CSRF protection)
        - HttpOnly cookie (XSS protection)
        - Secure cookie (HTTPS only)
        - SameSite=Lax (CSRF protection)
    """
    if not _oauth2_manager or not _session_manager:
        raise HTTPException(status_code=500, detail="Auth managers not initialized")

    # 1. Validate CSRF state
    stored_state = _session_manager._redis.get(f"oauth2_state:{state}")
    if not stored_state:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired state token (CSRF protection)",
        )

    # Delete state (one-time use)
    _session_manager._redis.delete(f"oauth2_state:{state}")

    try:
        # 2. Exchange code for token
        redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")
        if not redirect_uri:
            raise HTTPException(status_code=500, detail="GOOGLE_REDIRECT_URI not configured")

        credentials = _oauth2_manager.exchange_code_web(code, redirect_uri)

        # 3. Get user info from Google
        user_info = _oauth2_manager.get_user_info_web(credentials)

        # 3.5. Issue #349: Registration gate
        blocked = await _check_registration_allowed(user_info["email"])
        if blocked:
            return blocked

        # 4. Ensure user exists in database & assign role
        role_manager = get_role_manager()
        role = await role_manager.ensure_user(
            email=user_info["email"],
            user_id=user_info["sub"],
            name=user_info.get("name"),
            auth_provider="google",
        )

        # 5. Create session
        # Note on key naming:
        # - "sub" is OAuth2/OpenID Connect standard (RFC 7519) for user identifier
        # - "user_id" is Kagura Memory Cloud's internal standard used across:
        #   * Memory API endpoints (user_id parameter)
        #   * API Key authentication (returns user_id)
        #   * OAuth2 token authentication (returns user_id)
        #   * MCP authentication (uses user_id)
        #
        # Both keys are included for:
        # - OAuth2 standard compliance ("sub" for audit/debugging)
        # - Internal API compatibility ("user_id" for consistent access)
        #
        # The value is the same: Google OAuth2 user identifier (sub claim)

        # Issue #114: Invalidate old sessions before creating new one
        # This prevents session fixation attacks and ensures only one active session per user
        deleted_count = _session_manager.delete_user_sessions(user_info["sub"])
        if deleted_count > 0:
            logger.info(f"Invalidated {deleted_count} old session(s) for {user_info['email']}")

        session_data = {
            "sub": user_info["sub"],  # OAuth2 standard: user identifier
            "user_id": user_info["sub"],  # Internal API: same value for compatibility
            "email": user_info["email"],
            "name": user_info.get("name"),
            "picture": user_info.get("picture"),
            "role": role.value,
        }
        session_id = _session_manager.create_session(session_data)

        # Issue #212: Auto-create personal workspace on first login
        # Skip if user has pending invitations (they'll get workspace via invitation)
        try:
            async for db in get_db():
                from services.invitation_service import InvitationService

                invitation_service = InvitationService(db)

                # Check for pending invitations
                pending_invites = await invitation_service.get_pending_invitations_for_email(
                    email=user_info["email"]
                )

                if pending_invites:
                    # Issue #276: Skip personal workspace creation if user has pending invitations
                    # Note: If user never accepts invitations, they'll have 0 workspaces.
                    # WorkspaceGuard will redirect to /workspace/dashboard where they can create one manually.
                    logger.info(
                        f"User {user_info['email']} has {len(pending_invites)} pending invitation(s), "
                        f"skipping personal workspace auto-creation"
                    )
                else:
                    # No pending invitations - create personal workspace
                    workspace_service = WorkspaceService(db)
                    await workspace_service.ensure_personal_workspace(
                        user_id=user_info["sub"],
                        email=user_info["email"],
                    )
                break  # Exit async for loop
        except Exception as e:
            logger.error(
                f"Error ensuring personal workspace for user {user_info['sub']} ({user_info['email']}): {e}",
                exc_info=True,
            )
            # Non-blocking: User can create workspace manually if auto-creation fails

        # 6. Set HttpOnly cookie and redirect
        # Issue #102: Restore return_to from Redis if exists
        return_to_url = None
        if _session_manager:
            return_to_url = _session_manager._redis.get(f"oauth2_return_to:{state}")
            if return_to_url:
                # Delete after use (one-time)
                _session_manager._redis.delete(f"oauth2_return_to:{state}")

        # Determine redirect destination
        if return_to_url:
            # Return to original OAuth authorize flow
            redirect_url = return_to_url
        else:
            # Default: frontend workspace overview
            # Issue #258: Redirect to /workspace/dashboard after login
            frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
            redirect_url = f"{frontend_url}/workspace/dashboard"

        redirect = RedirectResponse(url=redirect_url, status_code=303)
        # Issue #115: Cookie name changed from 'session_id' to 'kagura_session'
        redirect.set_cookie(
            key="kagura_session",
            value=session_id,
            path="/",  # Available for all paths
            httponly=True,
            secure=False,  # False for local development (HTTP), True for production (HTTPS)
            samesite="lax",  # CSRF protection
            max_age=_session_manager.session_ttl,
        )

        logger.info(f"OAuth2 login successful: {user_info['email']} (role={role})")

        return redirect

    except Exception as e:
        logger.error(f"OAuth2 callback failed: {e}")
        raise HTTPException(
            status_code=401, detail=f"OAuth2 authentication failed: {str(e)}"
        ) from e


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout user and delete session + clear cookie.

    Issue #93-2: Implement proper logout
    Issue #115: Cookie name changed from 'session_id' to 'kagura_session'

    Args:
        request: FastAPI request (to read cookie)
        response: FastAPI response (to clear cookie)

    Returns:
        Success message

    Example:
        POST /auth/logout
        Cookie: kagura_session=...
        Response: {"success": true}
        Set-Cookie: kagura_session=; Max-Age=0
    """
    if not _session_manager:
        raise HTTPException(status_code=500, detail="Session manager not initialized")

    # Read session_id from cookie (Issue #115: renamed to kagura_session)
    session_id = request.cookies.get("kagura_session")

    if session_id:
        # Delete session from Redis
        _session_manager.delete_session(session_id)
        logger.info(f"User logged out: session={session_id[:8]}...")

        # Clear cookie in browser
        response.delete_cookie(key="kagura_session", path="/")

    return {"success": True, "message": "Logged out successfully"}


@router.get("/me")
async def get_current_user_info(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Get current authenticated user info.

    Issue #252: Now uses require_session_auth (Session-only, no API keys).

    Args:
        user: Current user from session (via require_session_auth)
        db: Database session

    Returns:
        User information from session + database (wrapped in "user" object for frontend compatibility)

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if API key is provided

    Example:
        GET /api/v1/auth/me
        Cookie: kagura_session=...
        Response: {
            "user": {
                "id": "google_123",
                "email": "user@example.com",
                "name": "Example User",
                "role": "admin",
                "current_workspace_id": "uuid..."
            }
        }

    Note:
        Frontend expects {user: {...}} format (Issue #664).
        Response wrapped in "user" object for compatibility.
    """
    from sqlalchemy import select

    from models.auth import User

    user_id = user.get("user_id")

    # Fetch timezone from database
    result = await db.execute(select(User).where(User.user_id == user_id))
    db_user = result.scalar_one_or_none()

    # Return wrapped in "user" object for frontend compatibility
    return {
        "user": {
            "id": user_id,
            "email": user.get("email"),
            "name": user.get("name"),
            "picture": user.get("picture"),
            "role": user.get("role", "user"),
            "timezone": db_user.timezone if db_user else "UTC",  # Issue #175
            "current_workspace_id": str(user["current_workspace_id"])
            if user.get("current_workspace_id")
            else None,
            # Issue #246: current_context_id removed
        }
    }


# ============================================================================
# GitHub OAuth2 (Issue #315)
# ============================================================================

github_router = APIRouter(prefix="/github", tags=["authentication", "github-oauth2"])

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


@github_router.get("/login")
async def github_login(
    return_to: str | None = None,
):
    """Initiate GitHub OAuth2 login flow."""
    if not _session_manager:
        raise HTTPException(status_code=500, detail="Auth managers not initialized")

    client_id = os.getenv("GITHUB_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="GITHUB_CLIENT_ID not configured")

    state = secrets.token_urlsafe(32)
    _session_manager._redis.setex(f"oauth2_state:{state}", 300, "pending")

    if return_to:
        _session_manager._redis.setex(f"oauth2_return_to:{state}", 300, return_to)

    redirect_uri = os.getenv(
        "GITHUB_REDIRECT_URI",
        "http://localhost:8080/api/v1/auth/github/callback",
    )

    auth_url = (
        f"{GITHUB_AUTH_URL}?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=read:user+user:email"
        f"&state={state}"
    )

    if return_to:
        return RedirectResponse(auth_url, status_code=303)
    return LoginResponse(authorization_url=auth_url, state=state)


async def _github_exchange_code(code: str) -> str:
    """Exchange GitHub authorization code for access token."""
    client_id = os.getenv("GITHUB_CLIENT_ID")
    client_secret = os.getenv("GITHUB_CLIENT_SECRET")
    redirect_uri = os.getenv(
        "GITHUB_REDIRECT_URI",
        "http://localhost:8080/api/v1/auth/github/callback",
    )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise ValueError(f"GitHub OAuth error: {data.get('error_description', data['error'])}")

    return data["access_token"]


async def _github_get_user_info(access_token: str) -> dict[str, Any]:
    """Get user info from GitHub API."""
    async with httpx.AsyncClient() as client:
        # Get user profile
        user_resp = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10.0,
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()

        # Get primary email (may be private)
        email = user_data.get("email")
        if not email:
            emails_resp = await client.get(
                GITHUB_EMAILS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10.0,
            )
            emails_resp.raise_for_status()
            for e in emails_resp.json():
                if e.get("primary") and e.get("verified"):
                    email = e["email"]
                    break

        if not email:
            raise ValueError("GitHub account has no verified email")

    return {
        "sub": str(user_data["id"]),  # GitHub user ID as string (like Google's sub)
        "email": email,
        "name": user_data.get("name") or user_data.get("login"),
        "picture": user_data.get("avatar_url"),
    }


@github_router.get("/callback")
async def github_callback(
    code: str = Query(..., description="GitHub authorization code"),
    state: str = Query(..., description="CSRF state token"),
):
    """Handle GitHub OAuth2 callback.

    Same flow as Google callback: CSRF validation → token exchange →
    user info → session creation → cookie → redirect.
    """
    if not _oauth2_manager or not _session_manager:
        raise HTTPException(status_code=500, detail="Auth managers not initialized")

    # 1. Validate CSRF state
    stored_state = _session_manager._redis.get(f"oauth2_state:{state}")
    if not stored_state:
        raise HTTPException(status_code=400, detail="Invalid or expired state token")
    _session_manager._redis.delete(f"oauth2_state:{state}")

    try:
        # 2. Exchange code for token
        access_token = await _github_exchange_code(code)

        # 3. Get user info
        user_info = await _github_get_user_info(access_token)

        # 3.5. Issue #349: Registration gate
        blocked = await _check_registration_allowed(user_info["email"])
        if blocked:
            return blocked

        # 4. Ensure user exists & assign role
        # Use GitHub's user ID for new users, but for existing users (e.g., logged in
        # via Google before), ensure_user returns the role and we need to look up the
        # actual DB user_id which may differ from GitHub's ID.
        role_manager = get_role_manager()
        role = await role_manager.ensure_user(
            email=user_info["email"],
            user_id=user_info["sub"],
            name=user_info.get("name"),
            auth_provider="github",
        )

        # Look up actual DB user_id (may differ from GitHub ID if user first logged in via Google)
        from sqlalchemy import select

        from models.auth import User

        db_user_id = user_info["sub"]  # fallback
        async for db in get_db():
            result = await db.execute(select(User).filter_by(email=user_info["email"]))
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user_id = db_user.user_id
            break

        # 5. Create session using DB user_id (not GitHub's sub)
        deleted_count = _session_manager.delete_user_sessions(db_user_id)
        if deleted_count > 0:
            logger.info(f"Invalidated {deleted_count} old session(s) for {user_info['email']}")

        session_data = {
            "sub": db_user_id,
            "user_id": db_user_id,
            "email": user_info["email"],
            "name": user_info.get("name"),
            "picture": user_info.get("picture"),
            "role": role.value,
        }
        session_id = _session_manager.create_session(session_data)

        # 6. Auto-create personal workspace
        try:
            async for db in get_db():
                from services.invitation_service import InvitationService

                invitation_service = InvitationService(db)
                pending_invites = await invitation_service.get_pending_invitations_for_email(
                    email=user_info["email"]
                )

                if not pending_invites:
                    workspace_service = WorkspaceService(db)
                    await workspace_service.ensure_personal_workspace(
                        user_id=db_user_id,
                        email=user_info["email"],
                    )
                break
        except Exception as e:
            logger.error(f"Error ensuring workspace for GitHub user {db_user_id}: {e}")

        # 7. Set cookie and redirect
        return_to_url = _session_manager._redis.get(f"oauth2_return_to:{state}")
        if return_to_url:
            _session_manager._redis.delete(f"oauth2_return_to:{state}")

        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        redirect_url = return_to_url or f"{frontend_url}/workspace/dashboard"

        redirect = RedirectResponse(url=redirect_url, status_code=303)
        redirect.set_cookie(
            key="kagura_session",
            value=session_id,
            path="/",
            httponly=True,
            secure=False,
            samesite="lax",
            max_age=_session_manager.session_ttl,
        )

        logger.info(f"GitHub OAuth2 login successful: {user_info['email']} (role={role})")
        return redirect

    except Exception as e:
        logger.error(f"GitHub OAuth2 callback failed: {e}")
        raise HTTPException(
            status_code=401, detail=f"GitHub authentication failed: {str(e)}"
        ) from e


# ============================================================================
# Password + MFA Endpoints (Issue #51)
# ============================================================================


class PasswordLoginRequest(BaseModel):
    """Password login request."""

    login_id: str
    password: str


class PasswordLoginResponse(BaseModel):
    """Password login response."""

    success: bool
    mfa_required: bool = False
    mfa_session_token: str | None = None
    redirect_url: str | None = None


class MfaVerifyRequest(BaseModel):
    """MFA verification request."""

    mfa_session_token: str
    totp_code: str


class AuthConfigResponse(BaseModel):
    """Auth configuration for frontend."""

    password_login_enabled: bool
    google_oauth_enabled: bool
    github_oauth_enabled: bool


async def _create_session_and_workspace(
    user_id: str,
    email: str,
    name: str | None,
    role: str,
    picture: str | None = None,
) -> str:
    """Create session and ensure personal workspace exists.

    Shared by OAuth callbacks and password login.
    """
    if not _session_manager:
        raise HTTPException(status_code=500, detail="Session manager not initialized")

    deleted_count = _session_manager.delete_user_sessions(user_id)
    if deleted_count > 0:
        logger.info(f"Invalidated {deleted_count} old session(s) for {email}")

    session_data = {
        "sub": user_id,
        "user_id": user_id,
        "email": email,
        "name": name,
        "picture": picture,
        "role": role,
    }
    session_id = _session_manager.create_session(session_data)

    try:
        async for db in get_db():
            workspace_service = WorkspaceService(db)
            await workspace_service.ensure_personal_workspace(
                user_id=user_id,
                email=email,
            )
            break
    except Exception as e:
        logger.error(f"Error ensuring personal workspace for {user_id}: {e}", exc_info=True)

    return session_id


def _set_session_cookie(response: Response, session_id: str) -> None:
    """Set session cookie on response."""
    if not _session_manager:
        return
    response.set_cookie(
        key="kagura_session",
        value=session_id,
        path="/",
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=_session_manager.session_ttl,
    )


@router.get("/config")
async def get_auth_config():
    """Get authentication configuration (public)."""
    return AuthConfigResponse(
        password_login_enabled=True,
        google_oauth_enabled=bool(os.getenv("GOOGLE_CLIENT_ID")),
        github_oauth_enabled=bool(os.getenv("GITHUB_CLIENT_ID")),
    )


@router.post("/login")
async def password_login(
    body: PasswordLoginRequest,
    return_to: str | None = Query(None),
):
    """Authenticate with login_id and password."""
    if not _session_manager:
        raise HTTPException(status_code=500, detail="Session manager not initialized")

    from auth.password import verify_password

    async for db in get_db():
        result = await db.execute(
            select(User).where(User.login_id == body.login_id, User.auth_method == "password")
        )
        user = result.scalar_one_or_none()
        break

    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # MFA check
    if user.totp_enabled and user.totp_secret:
        mfa_token = secrets.token_urlsafe(32)
        _session_manager._redis.setex(f"mfa_pending:{mfa_token}", 300, user.user_id)

        return PasswordLoginResponse(success=True, mfa_required=True, mfa_session_token=mfa_token)

    # No MFA — create session
    session_id = await _create_session_and_workspace(
        user_id=user.user_id, email=user.email, name=user.name, role=user.role
    )

    async for db in get_db():
        from utils.datetime import utcnow

        result = await db.execute(select(User).where(User.user_id == user.user_id))
        db_user = result.scalar_one_or_none()
        if db_user:
            db_user.last_login_at = utcnow()
            await db.commit()
        break

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    redirect_url = return_to or f"{frontend_url}/workspace/dashboard"

    response = Response(
        content=PasswordLoginResponse(
            success=True, mfa_required=False, redirect_url=redirect_url
        ).model_dump_json(),
        media_type="application/json",
    )
    _set_session_cookie(response, session_id)

    logger.info(f"Password login successful: {user.email}")
    return response


@router.post("/mfa/verify")
async def mfa_verify(
    body: MfaVerifyRequest,
    return_to: str | None = Query(None),
):
    """Verify TOTP code and create session."""
    if not _session_manager:
        raise HTTPException(status_code=500, detail="Session manager not initialized")

    user_id = _session_manager._redis.get(f"mfa_pending:{body.mfa_session_token}")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA session")

    async for db in get_db():
        result = await db.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        break

    if not user or not user.totp_secret:
        raise HTTPException(status_code=401, detail="MFA not configured")

    try:
        from utils.encryption import get_encryptor

        totp_secret = get_encryptor().decrypt(user.totp_secret)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt MFA secret")

    from auth.totp import verify_totp

    if not verify_totp(totp_secret, body.totp_code):
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    _session_manager._redis.delete(f"mfa_pending:{body.mfa_session_token}")

    session_id = await _create_session_and_workspace(
        user_id=user.user_id, email=user.email, name=user.name, role=user.role
    )

    async for db in get_db():
        from utils.datetime import utcnow

        result = await db.execute(select(User).where(User.user_id == user.user_id))
        db_user = result.scalar_one_or_none()
        if db_user:
            db_user.last_login_at = utcnow()
            await db.commit()
        break

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    redirect_url = return_to or f"{frontend_url}/workspace/dashboard"

    response = Response(
        content=PasswordLoginResponse(
            success=True, mfa_required=False, redirect_url=redirect_url
        ).model_dump_json(),
        media_type="application/json",
    )
    _set_session_cookie(response, session_id)

    logger.info(f"MFA verification successful: {user.email}")
    return response


# Include subrouters
router.include_router(google_router)
router.include_router(github_router)
