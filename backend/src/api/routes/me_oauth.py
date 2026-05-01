"""Manual OAuth re-sync endpoint (Issue #515).

Lets a logged-in user force a fresh round-trip to their original IdP so
the existing ``RoleManager.ensure_user`` path syncs ``email`` / ``name``
on demand — useful when the user has changed their IdP primary email
and does not want to wait for the next natural login (Issue #481 already
syncs on every login; this just shortcuts the wait).

Flow:

1. ``POST /api/v1/me/refresh-oauth`` — authenticated. Returns the IdP
   ``authorization_url`` and the CSRF state. Frontend redirects.
2. The IdP returns to the existing ``/auth/{provider}/callback`` route.
   That callback now reads ``oauth2_state_intent:{state}`` and, if it
   reads ``"refresh"``, branches:
     - skip ``delete_user_sessions`` + ``create_session`` (the user keeps
       their current session — refresh ≠ re-login);
     - verify the IdP-returned ``sub`` matches the originating user;
     - redirect to the operator-supplied ``return_to`` (default
       ``/profile?refreshed=1``) instead of the dashboard.

Rate limit: 1 request per minute per user, via ``increment_counter``.
The IdP may shed the request itself, but we throttle locally so a
buggy frontend cannot hammer the IdP.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_module
from auth.dependencies import SessionUser
from db.base import get_db
from db.redis import increment_counter
from models.auth import User
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/me", tags=["me-oauth"])

# State-token sub-keys (parallel to oauth2_state:{state} in auth.py).
# Storing intent + originating user under separate Redis keys avoids
# changing the value contract of the existing oauth2_state:{state} key
# (which auth.py reads back as the literal string "pending").
INTENT_KEY = "oauth2_state_intent:{state}"
USER_KEY = "oauth2_state_user:{state}"
STATE_TTL = 300  # 5 minutes — matches auth.py's existing oauth2_state TTL

# Rate limit: per-user, per-minute window.
RATE_LIMIT_PER_MINUTE = 1


def _normalize_return_to(value: str) -> str | None:
    """Validate and normalize ``return_to`` for use as a same-origin
    redirect target.

    Returns the trimmed, validated value when accepted, or ``None`` when
    rejected. Callers should use the return value verbatim — never the
    raw input — when persisting to Redis or building the
    ``RedirectResponse`` Location header.

    Why normalize before persist (Copilot review #5): if we validated
    a stripped form but persisted the raw input, leading/trailing
    whitespace or embedded control characters would survive into the
    eventual ``Location`` header. Even if browsers tolerate
    ``Location: /profile  ``, embedded ``\\r\\n`` characters can enable
    HTTP response splitting on intermediaries that don't reject them.
    Reject any C0 control character (0x00–0x1F, 0x7F) outright.

    Accepted shapes:
    - Single leading ``/`` followed by a non-``/`` byte (e.g. ``/profile``),
      with no embedded control characters.
    Rejected shapes:
    - Empty / whitespace.
    - Embedded CR / LF / NUL / any other C0 control byte.
    - Missing leading ``/``.
    - Protocol-relative (``//host/...``).
    - Absolute URL (``http://...``, ``https://...``, ``javascript:...``).
    - Backslash path tricks (``\\evil`` — some browsers treat as ``//evil``).
    - URL-encoded equivalents of the above (``%2F%2Fevil``, ``%5Cevil``).
    """
    # Reject any C0 control byte (0x00–0x1F, 0x7F) anywhere in the raw
    # input. This MUST run before .strip() — Python treats \t, \r, \n,
    # \v, \f as whitespace and would silently strip a trailing CR or LF,
    # turning a CRLF-injection probe like ``/profile\\r`` into a clean
    # ``/profile`` that flows through to Redis. Rejecting outright at
    # the raw boundary forces buggy / hostile callers to fix their input.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    s = value.strip(" ")  # only ASCII space, not \t / \r / \n / \v / \f
    if len(s) < 2:
        return None
    if not s.startswith("/"):
        return None
    if s.startswith("//"):
        return None
    if s.startswith("/\\"):
        return None
    lowered = s.lower()
    if lowered.startswith("/%2f") or lowered.startswith("/%5c"):
        return None
    return s


def _is_safe_relative_return_to(value: str) -> bool:
    """Backward-compat shim used by tests that only need the predicate.
    New callers should use ``_normalize_return_to`` and consume the
    normalized value, not the raw input."""
    return _normalize_return_to(value) is not None


class RefreshOAuthRequest(BaseModel):
    """Optional ``return_to`` lets the frontend control the post-callback
    landing page. Must be a same-origin relative path starting with ``/``;
    absolute or protocol-relative URLs are rejected at the endpoint to
    block open-redirect after successful re-auth. Defaults to
    ``/profile?refreshed=1`` so a vanilla button click works without
    extra wiring."""

    return_to: str | None = None


class RefreshOAuthResponse(BaseModel):
    """Frontend redirects ``window.location`` to ``authorization_url``."""

    authorization_url: str
    state: str


@router.post("/refresh-oauth", response_model=RefreshOAuthResponse)
async def refresh_oauth(
    payload: RefreshOAuthRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> RefreshOAuthResponse:
    """Initiate a manual IdP refresh for the current user.

    Returns:
        JSON with ``authorization_url`` (frontend redirects to it) and
        the CSRF ``state`` token (for symmetry with ``/auth/{provider}/login``).

    Raises:
        HTTPException(400): user is password-auth, or has no usable
            ``auth_provider`` (legacy null pre-#361 — those users must
            log out and log back in instead).
        HTTPException(429): more than 1 request in the current minute
            window. ``Retry-After`` header is set on the response.
        HTTPException(500): OAuth managers not initialised, or required
            environment variables missing.
    """
    if not auth_module._session_manager:
        raise HTTPException(status_code=500, detail="Auth managers not initialized")

    # Validate + normalize return_to up front. The normalized form is
    # what gets persisted to Redis (Copilot review #5: validating a
    # stripped form while persisting the raw input lets whitespace or
    # control bytes survive into the Location header).
    if payload.return_to is not None:
        normalized_return_to = _normalize_return_to(payload.return_to)
        if normalized_return_to is None:
            raise HTTPException(
                status_code=400,
                detail="return_to must be a same-origin relative path starting with '/'",
            )
    else:
        normalized_return_to = None

    user_id = user["user_id"]

    # Look up the originating IdP. Issue #361 added auth_provider; pre-#361
    # rows have it null — those users can't be refreshed (we don't know
    # which IdP) and must log out/back in to repopulate the column.
    result = await db.execute(select(User).where(User.user_id == user_id))
    db_user = result.scalar_one_or_none()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    if db_user.auth_method != "oauth":
        # Password users have no IdP to refresh from.
        raise HTTPException(
            status_code=400,
            detail="refresh-oauth is only available for OAuth users",
        )
    provider = db_user.auth_provider
    if provider not in ("google", "github"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Your account has no recorded OAuth provider. "
                "Please sign out and sign in again to refresh your identity."
            ),
        )

    # Resolve provider-specific config BEFORE writing any Redis state
    # (Copilot review #3: a missing GOOGLE_REDIRECT_URI / GITHUB_CLIENT_ID
    # would otherwise 500 with 4 stale keys orphaned in Redis for 5min).
    if provider == "google":
        if not auth_module._oauth2_manager:
            raise HTTPException(status_code=500, detail="OAuth2 manager not initialized")
        google_redirect_uri = os.getenv("GOOGLE_REDIRECT_URI")
        if not google_redirect_uri:
            raise HTTPException(status_code=500, detail="GOOGLE_REDIRECT_URI not configured")
        github_client_id: str | None = None
        github_redirect_uri: str | None = None
    else:  # github
        google_redirect_uri = None
        github_client_id = os.getenv("GITHUB_CLIENT_ID")
        if not github_client_id:
            raise HTTPException(status_code=500, detail="GITHUB_CLIENT_ID not configured")
        github_redirect_uri = os.getenv(
            "GITHUB_REDIRECT_URI",
            "http://localhost:8080/api/v1/auth/github/callback",
        )

    # Rate limit: 1/minute/user. Window-based on minute floor so a
    # single user can't burst more than once per minute even by spreading
    # calls across multiple processes.
    now = datetime.now(UTC)
    minute_window = now.strftime("%Y%m%d%H%M")
    rl_key = f"rate_limit:refresh_oauth:{user_id}:{minute_window}"
    try:
        count = await increment_counter(rl_key, ttl=60)
    except Exception:
        # Don't fail the request on Redis trouble — increment_counter
        # already logged. Better to let the user refresh than 503 the
        # whole flow because of an infra blip.
        count = 1

    if count > RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail="Too many refresh requests; try again in a minute",
            headers={"Retry-After": "60"},
        )

    # Generate a fresh CSRF state token. Use the same key naming auth.py
    # uses ("oauth2_state:{state}" = "pending") so the existing callback
    # can validate it without per-intent branching at the validation step.
    state = secrets.token_urlsafe(32)
    redis = auth_module._session_manager._redis
    redis.setex(f"oauth2_state:{state}", STATE_TTL, "pending")
    redis.setex(INTENT_KEY.format(state=state), STATE_TTL, "refresh")
    # Pin the originating user_id so the callback can reject a same-state
    # token returning with a different IdP account (CSRF + account
    # confusion defence).
    redis.setex(USER_KEY.format(state=state), STATE_TTL, user_id)

    # Frontend default: bring the user back to /profile with a flag the
    # page can read to show a success toast. Persist the *normalized*
    # value (whitespace stripped, control bytes already rejected above).
    return_to = normalized_return_to or "/profile?refreshed=1"
    redis.setex(f"oauth2_return_to:{state}", STATE_TTL, return_to)

    if provider == "google":
        # google_redirect_uri is non-None here — guarded above.
        assert google_redirect_uri is not None
        authorization_url = auth_module._oauth2_manager.get_authorization_url_web(
            google_redirect_uri, state
        )
    else:  # github
        assert github_client_id is not None and github_redirect_uri is not None
        authorization_url = auth_module.build_github_authorization_url(
            client_id=github_client_id, redirect_uri=github_redirect_uri, state=state
        )

    logger.info("refresh_oauth_initiated", user_id=user_id, provider=provider)

    return RefreshOAuthResponse(authorization_url=authorization_url, state=state)
