"""Self-service password endpoints (Issue #1678).

Public (the emailed link is the credential):

- ``POST /auth/password/reset-request`` — always 202 with the same body; a
  reset link is emailed only when the address names an account with a
  verified email and a password.
- ``POST /auth/password/reset`` — set a new password from a reset link; every
  browser session and every OAuth2 / MCP grant (tokens, pending codes) of the
  account is revoked (#1738). No automatic sign-in.
- ``POST /auth/password/setup`` — set the first password from a set-up link;
  also marks the email verified; the account's other browser sessions are
  revoked.

Signed in (browser session only — never an API key):

- ``POST /me/password/setup-request`` — email a set-up link to an account
  without a password (following it proves the mailbox).
- ``POST /me/password/change`` — change the password (current one required);
  other browser sessions are revoked.
- ``DELETE /me/password`` — remove the password (current one required),
  refused while it is the last sign-in method.

None of these creates an account. A reset (the compromise-recovery path) also
revokes the account's OAuth / MCP grants, in the password write's transaction;
the other flows revoke browser sessions only. API keys, OAuth client secrets,
share keys and resource tokens are never revoked here — the reset email and
page point the user at them. Revocation runs before the password write
commits, and a failure answers 503 with the password unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_module
from auth.dependencies import SessionUser
from db.base import get_db
from db.redis import increment_counter
from services.email_service import redact_recipient
from services.password_account_service import (
    PasswordAccountService,
    normalize_email,
    process_reset_request,
)
from utils.exceptions import MemoryCloudException, RateLimitError, RedisError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/auth/password", tags=["authentication"])
me_router = APIRouter(prefix="/me/password", tags=["account-password"])

# Rate limits (fixed windows, via ``increment_counter``). A Redis outage lets
# requests through with a warning, like the device-flow limiter.
_RESET_WINDOW_SECONDS = 15 * 60
_RESET_REQUESTS_PER_IP = 10
_RESET_REQUESTS_PER_EMAIL = 3
_LINK_ATTEMPTS_PER_IP = 20
_SETUP_REQUESTS_PER_USER = 3
_CURRENT_PASSWORD_ATTEMPTS_PER_USER = 10


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PasswordResetRequestBody(BaseModel):
    """Body for POST /auth/password/reset-request."""

    email: str = Field(..., min_length=3, max_length=320)


class PasswordEmailAcceptedResponse(BaseModel):
    """Identical for every reset request, whether or not an email was sent."""

    status: Literal["accepted"] = "accepted"
    message: str = "If an account with that email exists, we sent a link to reset its password."


class PasswordSetupRequestedResponse(BaseModel):
    """Returned once the set-up link was emailed to the signed-in user."""

    status: Literal["sent"] = "sent"


class PasswordLinkBody(BaseModel):
    """Body for POST /auth/password/reset and /auth/password/setup.

    No ``max_length`` on ``new_password``: the policy check answers an
    over-long password with its own message (bcrypt's 72-byte limit).
    """

    token: str = Field(..., min_length=1, max_length=256)
    new_password: str


class PasswordChangeBody(BaseModel):
    """Body for POST /me/password/change."""

    current_password: str
    new_password: str


class PasswordRemoveBody(BaseModel):
    """Body for DELETE /me/password."""

    current_password: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _over_limit(key: str, limit: int, window: int = _RESET_WINDOW_SECONDS) -> bool:
    """Count one request against ``key``; True when it is over ``limit``."""
    try:
        count = await increment_counter(key, ttl=window)
    except RedisError as exc:
        logger.warning("password_rate_limit_unavailable", error_type=type(exc).__name__)
        return False
    return count > limit


def _revocation_unavailable() -> MemoryCloudException:
    """The 503 a password write gets when its sessions cannot be revoked."""
    return MemoryCloudException(
        "Could not sign out the account's other sessions. Please try again.",
        status_code=503,
        error_code="AUTH-304",
    )


def _session_revoker(keep_session_id: str | None) -> Callable[[str], None]:
    """Build the revocation step a password write runs before its commit.

    Deletes the user's browser sessions except ``keep_session_id`` (if any).
    A Redis failure raises (503) instead of reading as "0 revoked": the
    service then rolls the password write back, so the flow never reports
    success while the old sessions survive. OAuth / MCP tokens and API keys
    are not touched here.
    """

    def revoke(user_id: str) -> None:
        manager = auth_module._session_manager
        if manager is None:
            # No session store means the old sessions cannot be signed out;
            # fail closed so the password write rolls back.
            logger.error("password_sessions_revoke_failed", user_id=user_id, error_type="NoManager")
            raise _revocation_unavailable()
        try:
            deleted = manager.delete_user_sessions(
                user_id, exclude_session_id=keep_session_id, strict=True
            )
        except Exception as exc:
            logger.error(
                "password_sessions_revoke_failed", user_id=user_id, error_type=type(exc).__name__
            )
            raise _revocation_unavailable() from None
        logger.info("password_sessions_revoked", user_id=user_id, deleted=deleted)

    return revoke


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


@router.post("/reset-request", status_code=202, response_model=PasswordEmailAcceptedResponse)
async def request_password_reset(
    body: PasswordResetRequestBody,
    request: Request,
    background_tasks: BackgroundTasks,
) -> PasswordEmailAcceptedResponse:
    """Email a password-reset link if the address names an eligible account.

    Always answers 202 with the same body. The request path only checks the
    rate limits and schedules the same background task for every address;
    the account lookup, the token, the audit row and the email all happen
    after the response, on their own session, so neither the answer nor its
    timing reveals whether an account exists. Limited per client address
    (429) and per address (silently: no further emails).
    """
    ip = auth_module._login_client_ip(request)
    if await _over_limit(f"pw_reset_ip:{ip}", _RESET_REQUESTS_PER_IP):
        raise RateLimitError(
            "Too many password reset requests. Please try again later.",
            retry_after=_RESET_WINDOW_SECONDS,
        )
    email = normalize_email(body.email)
    # A keyed digest, not the address: ``increment_counter`` logs the key
    # when Redis fails, and the key would otherwise sit in Redis in clear.
    if await _over_limit(f"pw_reset_email:{redact_recipient(email)}", _RESET_REQUESTS_PER_EMAIL):
        logger.info("password_reset_request_throttled")
        return PasswordEmailAcceptedResponse()

    background_tasks.add_task(
        process_reset_request,
        email=email,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    return PasswordEmailAcceptedResponse()


@router.post("/reset", status_code=204, response_class=Response)
async def reset_password(
    body: PasswordLinkBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Set a new password from a reset link; sign out every browser session.

    Returns 204; the person then signs in with the new password. 400 for an
    unknown, expired or used link; 422 when the password breaks the policy.
    """
    ip = auth_module._login_client_ip(request)
    if await _over_limit(f"pw_link_ip:{ip}", _LINK_ATTEMPTS_PER_IP):
        raise RateLimitError("Too many attempts. Please try again later.")
    await PasswordAccountService(db).complete_reset(
        raw_token=body.token,
        new_password=body.new_password,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
        revoke_sessions=_session_revoker(keep_session_id=None),
    )
    return Response(status_code=204)


@router.post("/setup", status_code=204, response_class=Response)
async def setup_password(
    body: PasswordLinkBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Set the first password from a set-up link.

    Following the link proves the mailbox, so the email is marked verified.
    The account's other browser sessions are revoked; a session in this browser (the
    one that asked for the link) is kept.
    """
    ip = auth_module._login_client_ip(request)
    if await _over_limit(f"pw_link_ip:{ip}", _LINK_ATTEMPTS_PER_IP):
        raise RateLimitError("Too many attempts. Please try again later.")
    await PasswordAccountService(db).complete_setup(
        raw_token=body.token,
        new_password=body.new_password,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
        revoke_sessions=_session_revoker(
            keep_session_id=request.cookies.get(auth_module.SESSION_COOKIE_NAME)
        ),
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Signed in
# ---------------------------------------------------------------------------


@me_router.post("/setup-request", status_code=202, response_model=PasswordSetupRequestedResponse)
async def request_password_setup(
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> PasswordSetupRequestedResponse:
    """Email a set-a-password link to the signed-in account's address.

    409 when the account already has a password; 400 for a local CLI account;
    429 after a few requests; 503 when the email could not be sent.
    """
    user_id = user["user_id"]
    if await _over_limit(f"pw_setup_user:{user_id}", _SETUP_REQUESTS_PER_USER):
        raise RateLimitError(
            "Too many requests. Please check your email or try again later.",
            retry_after=_RESET_WINDOW_SECONDS,
        )
    await PasswordAccountService(db).request_setup(
        user_id=user_id,
        ip_address=auth_module._login_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return PasswordSetupRequestedResponse()


@me_router.post("/change", status_code=204, response_class=Response)
async def change_password(
    body: PasswordChangeBody,
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Change the password; every other browser session is revoked.

    403 when ``current_password`` is wrong; 409 when there is no password;
    422 when the new password breaks the policy.
    """
    user_id = user["user_id"]
    if await _over_limit(f"pw_current_user:{user_id}", _CURRENT_PASSWORD_ATTEMPTS_PER_USER):
        raise RateLimitError("Too many attempts. Please try again later.")
    await PasswordAccountService(db).change(
        user_id=user_id,
        current_password=body.current_password,
        new_password=body.new_password,
        ip_address=auth_module._login_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        revoke_sessions=_session_revoker(
            keep_session_id=request.cookies.get(auth_module.SESSION_COOKIE_NAME)
        ),
    )
    return Response(status_code=204)


@me_router.delete("", status_code=204, response_class=Response)
async def remove_password(
    body: PasswordRemoveBody,
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove the password. Refused (409) while no OAuth provider is linked.

    403 when ``current_password`` is wrong. Every other browser session is
    revoked.
    """
    user_id = user["user_id"]
    if await _over_limit(f"pw_current_user:{user_id}", _CURRENT_PASSWORD_ATTEMPTS_PER_USER):
        raise RateLimitError("Too many attempts. Please try again later.")
    await PasswordAccountService(db).remove(
        user_id=user_id,
        current_password=body.current_password,
        ip_address=auth_module._login_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        revoke_sessions=_session_revoker(
            keep_session_id=request.cookies.get(auth_module.SESSION_COOKIE_NAME)
        ),
    )
    return Response(status_code=204)
