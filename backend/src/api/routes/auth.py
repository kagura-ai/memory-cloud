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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from auth.oauth2 import OAuth2Manager
from auth.password import verify_password
from auth.roles import get_role_manager
from auth.session import SessionManager
from auth.totp import verify_totp
from db.base import get_db
from models.auth import User
from services.signup_gate_service import check_signup_access
from services.workspace_service import WorkspaceService
from utils.datetime import utcnow
from utils.encryption import get_encryptor
from utils.exceptions import ConflictError

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


def get_session_manager() -> SessionManager | None:
    """Return the active SessionManager instance (or None if not initialized).

    Use this in code that lives outside ``api.routes.auth`` instead of
    importing the private ``_session_manager`` module attribute directly.
    Returns ``None`` if called before :func:`initialize_auth_routes`.
    """
    return _session_manager


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


async def _check_registration_allowed(
    email: str, db: AsyncSession | None = None, *, user_id: str | None = None
) -> RedirectResponse | None:
    """Check if a new user is allowed to register.

    Returns None if allowed, RedirectResponse if blocked.
    Rules:
    - Existing users: always allowed (login, not registration)
    - First user (no users exist): always allowed (need initial admin)
    - ALLOW_REGISTRATION=true: always allowed
    - Pending invitation for this email: always allowed
    - Otherwise: blocked → redirect to login with error

    Args:
        email: Candidate signup email.
        db: Optional existing session. When provided, the check runs on the
            caller's session (avoids opening a second pool connection when
            dispatched from SignupGateService's fallback path).
        user_id: OAuth sub claim. When provided, an existing-user match on
            *either* email or user_id counts as "existing" so a returning
            user whose IdP email changed is still recognised as a login (not
            blocked as a new signup).
    """
    from sqlalchemy import func, or_, select

    from config.settings import get_settings
    from models.auth import User
    from services.invitation_service import InvitationService

    settings = get_settings()

    async def _run(session: AsyncSession) -> RedirectResponse | None:
        # Existing users can always login. NOTE (#481): an email match here
        # also lets through cross-provider squatters (e.g. Google user
        # alice@x.com → GitHub login with same address). That case is caught
        # downstream by RoleManager.ensure_user, which raises ConflictError
        # → callback redirects to /login?error=email_in_use. Don't tighten
        # this gate to also reject cross-provider — multi-provider account
        # linking is intentionally deferred to a separate issue.
        #
        # user_id (OAuth sub) is included as an OR condition so a returning
        # user whose email changed at the IdP is recognised as existing and
        # never blocked — RoleManager.ensure_user handles the email sync.
        if user_id is not None and user_id:
            cond = or_(User.email == email, User.user_id == user_id)
        else:
            cond = User.email == email
        result = await session.execute(select(User).where(cond))
        if result.scalar_one_or_none():
            return None

        # First user always allowed
        user_count_result = await session.execute(select(func.count()).select_from(User))
        if user_count_result.scalar() == 0:
            return None

        # Registration open
        if settings.allow_registration:
            return None

        # Invited users bypass gate
        invitation_service = InvitationService(session)
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

    if db is not None:
        return await _run(db)
    async for session in get_db():
        return await _run(session)
    return None


def _email_in_use_redirect() -> RedirectResponse:
    """Redirect to the frontend login page on cross-provider email collision.

    Issue #481: when ``RoleManager.ensure_user`` raises ``ConflictError`` (the
    OAuth user's email is already bound to a different provider's account),
    surface a stable error code on the login page rather than a JSON 409 mid-
    redirect. Mirrors the shape of ``_check_registration_allowed``'s blocked
    branch so both Google and GitHub callbacks end on the same UX surface.
    """
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    return RedirectResponse(
        f"{frontend_url}/login?error=email_in_use",
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
    request: Request,
    code: str = Query(..., description="OAuth2 authorization code"),
    state: str = Query(..., description="CSRF state token"),
):
    """Handle Google OAuth2 callback.

    Exchanges authorization code for access token, retrieves user info,
    creates session, and sets HttpOnly cookie.

    Args:
        request: FastAPI request (used for IP / User-Agent capture on the
            audit row written by ``RoleManager.ensure_user`` when the IdP-
            provided email differs from the stored value).
        code: OAuth2 authorization code from Google.
        state: CSRF state token (must match stored state).

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

        # 3.5. Registration gate. Google's own OAuth + workspace configuration
        # decides who can sign up via Google, so the backend gate passes
        # through for provider="google" when admin-configurable mode is on;
        # when off, delegates to _check_registration_allowed (Issue #349) just
        # like the GitHub path. This keeps a single gate abstraction without
        # layering a redundant backend allowlist on top of Google's own.
        blocked = await check_signup_access(
            provider="google",
            oauth_sub=user_info["sub"],
            email=user_info["email"],
        )
        if blocked:
            return blocked

        # 4. Ensure user exists in database & assign role.
        # Issue #481: pass email_verified from Google's userinfo response so
        # ensure_user can sync mutable email/name without trusting the IdP
        # blindly. Google v3 /oauth2/v3/userinfo always populates the
        # email_verified boolean; default False here so a missing field is
        # treated as unverified rather than implicitly trusted.
        role_manager = get_role_manager()
        role = await role_manager.ensure_user(
            email=user_info["email"],
            user_id=user_info["sub"],
            name=user_info.get("name"),
            auth_provider="google",
            # Strict identity check (not bool coercion) so a stringly-typed
            # "True" / "false" from a misconfigured IdP cannot pass the gate.
            email_verified=user_info.get("email_verified") is True,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
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

    except ConflictError:
        return _email_in_use_redirect()
    except SQLAlchemyError:
        # Don't swallow DB errors as 401 — let the app-wide
        # @app.exception_handler(SQLAlchemyError) map them to 503 so DB
        # availability issues surface as retriable, not as auth failures.
        # ConflictError above already handles the email-collision IntegrityError
        # path; anything else here is unexpected DB trouble.
        raise
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
    """Get user info from GitHub API.

    Always fetches ``/user/emails`` and selects the primary verified address,
    ignoring the public-profile ``email`` field on ``/user``. The public field
    is user-mutable in GitHub UI and verification status is not exposed there,
    so trusting it would defeat the ``email_verified`` gate (Issue #481): a
    user could surface an unverified address as their public email and have
    it written into ``users.email`` on every login.

    The returned dict carries ``email_verified=True`` as an invariant — if no
    verified primary exists, the function raises ``ValueError`` and the OAuth
    callback fails before any DB write. Callers can therefore unconditionally
    pass ``email_verified=True`` to ``RoleManager.ensure_user`` without a
    second-guessing branch.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(GITHUB_USER_URL, headers=headers, timeout=10.0)
        user_resp.raise_for_status()
        user_data = user_resp.json()

        emails_resp = await client.get(GITHUB_EMAILS_URL, headers=headers, timeout=10.0)
        emails_resp.raise_for_status()
        primary_verified = next(
            (e["email"] for e in emails_resp.json() if e.get("primary") and e.get("verified")),
            None,
        )

    if not primary_verified:
        raise ValueError("GitHub account has no verified primary email")

    return {
        "sub": str(user_data["id"]),  # GitHub user ID as string (like Google's sub)
        "email": primary_verified,
        "email_verified": True,  # Invariant — only verified primary reaches here
        "name": user_data.get("name") or user_data.get("login"),
        "picture": user_data.get("avatar_url"),
        "login": user_data.get("login"),  # GitHub username for audit logging (Issue #358)
    }


@github_router.get("/callback")
async def github_callback(
    request: Request,
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

        # 3.5. Registration gate: admin-configurable (Issue #358) with legacy
        # _check_registration_allowed delegation when disabled (Issue #349).
        blocked = await check_signup_access(
            provider="github",
            oauth_sub=user_info["sub"],
            email=user_info["email"],
            username=user_info.get("login"),
        )
        if blocked:
            return blocked

        # 4. Ensure user exists & assign role.
        # Issue #481: lookup is by user_id (GitHub sub), so the post-call DB
        # re-query previously needed for cross-provider account linking is
        # no longer correct — when a Google user attempts a GitHub login with
        # the same email, ensure_user raises ConflictError(409) instead of
        # silently linking the GitHub login to the Google row. The session
        # subject can therefore be user_info["sub"] directly.
        # email_verified is an invariant True here: _github_get_user_info
        # always selects the verified primary address from /user/emails or
        # raises before reaching this point.
        role_manager = get_role_manager()
        role = await role_manager.ensure_user(
            email=user_info["email"],
            user_id=user_info["sub"],
            name=user_info.get("name"),
            auth_provider="github",
            # _github_get_user_info enforces the verified-primary invariant
            # and always sets email_verified=True; KeyError on a missing key
            # is the desired loud failure.
            email_verified=user_info["email_verified"],
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

        db_user_id = user_info["sub"]

        # 5. Create session using GitHub sub as user_id
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

    except ConflictError:
        return _email_in_use_redirect()
    except SQLAlchemyError:
        # See google_callback's matching block — let DB errors hit the
        # global SQLAlchemyError → 503 handler instead of being misclassified
        # as 401 auth failures.
        raise
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
            # Update last_login_at
            result = await db.execute(select(User).where(User.user_id == user_id))
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user.last_login_at = utcnow()
                await db.commit()
            break
    except Exception as e:
        logger.error(f"Error ensuring personal workspace for {user_id}: {e}", exc_info=True)

    return session_id


def _set_session_cookie(response: Response, session_id: str) -> None:
    """Set session cookie on response."""
    if not _session_manager:
        return
    is_production = os.getenv("ENVIRONMENT", "development") == "production"
    response.set_cookie(
        key="kagura_session",
        value=session_id,
        path="/",
        httponly=True,
        secure=is_production,
        samesite="lax",
        max_age=_session_manager.session_ttl,
    )


def _safe_redirect_url(return_to: str | None) -> str:
    """Validate return_to to prevent open redirect attacks."""
    from urllib.parse import urlparse

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    default = f"{frontend_url}/workspace/dashboard"

    if not return_to:
        return default

    parsed = urlparse(return_to)
    # Allow relative paths or same-origin URLs
    if not parsed.netloc:
        return return_to
    frontend_host = urlparse(frontend_url).netloc
    api_host = urlparse(os.getenv("API_URL", "http://localhost:8080")).netloc
    if parsed.netloc in (frontend_host, api_host, "localhost:8080", "localhost:3000"):
        return return_to
    return default


_LOGIN_ATTEMPT_PREFIX = "login_attempts:"
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes


def _check_login_rate_limit(login_id: str) -> None:
    """Check brute-force protection. Raises 429 if too many attempts."""
    if not _session_manager:
        return
    key = f"{_LOGIN_ATTEMPT_PREFIX}{login_id}"
    attempts = _session_manager._redis.get(key)
    if attempts and int(attempts) >= _MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please try again later.",
        )


def _record_login_failure(login_id: str) -> None:
    """Record a failed login attempt."""
    if not _session_manager:
        return
    key = f"{_LOGIN_ATTEMPT_PREFIX}{login_id}"
    pipe = _session_manager._redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, _LOGIN_LOCKOUT_SECONDS)
    pipe.execute()


def _clear_login_failures(login_id: str) -> None:
    """Clear failed login attempts on success."""
    if not _session_manager:
        return
    _session_manager._redis.delete(f"{_LOGIN_ATTEMPT_PREFIX}{login_id}")


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

    # Brute-force protection
    _check_login_rate_limit(body.login_id)

    async for db in get_db():
        result = await db.execute(
            select(User).where(User.login_id == body.login_id, User.auth_method == "password")
        )
        user = result.scalar_one_or_none()
        break

    if not user or not user.password_hash:
        _record_login_failure(body.login_id)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(body.password, user.password_hash):
        _record_login_failure(body.login_id)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    _clear_login_failures(body.login_id)

    # MFA check
    if user.totp_enabled and user.totp_secret:
        mfa_token = secrets.token_urlsafe(32)
        _session_manager._redis.setex(f"mfa_pending:{mfa_token}", 300, user.user_id)

        return PasswordLoginResponse(success=True, mfa_required=True, mfa_session_token=mfa_token)

    # No MFA — create session
    session_id = await _create_session_and_workspace(
        user_id=user.user_id, email=user.email, name=user.name, role=user.role
    )

    response = Response(
        content=PasswordLoginResponse(
            success=True, mfa_required=False, redirect_url=_safe_redirect_url(return_to)
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
        totp_secret = get_encryptor().decrypt(user.totp_secret)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to decrypt MFA secret") from e

    if not verify_totp(totp_secret, body.totp_code):
        # Delete MFA token on failed attempt (prevent brute-force replay)
        _session_manager._redis.delete(f"mfa_pending:{body.mfa_session_token}")
        raise HTTPException(status_code=401, detail="Invalid TOTP code. Please login again.")

    _session_manager._redis.delete(f"mfa_pending:{body.mfa_session_token}")

    session_id = await _create_session_and_workspace(
        user_id=user.user_id, email=user.email, name=user.name, role=user.role
    )

    response = Response(
        content=PasswordLoginResponse(
            success=True, mfa_required=False, redirect_url=_safe_redirect_url(return_to)
        ).model_dump_json(),
        media_type="application/json",
    )
    _set_session_cookie(response, session_id)

    logger.info(f"MFA verification successful: {user.email}")
    return response


# Include subrouters
router.include_router(google_router)
router.include_router(github_router)
