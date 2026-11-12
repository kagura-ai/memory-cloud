"""Email + password sign-up endpoints (Issue #1734).

Public: the emailed link is the credential. ``ENABLE_EMAIL_SIGNUP=false``
404s every route (router-level dependency, before body validation).

- ``POST /auth/signup/email/request`` — always 202 with the same body. The
  route only counts its rate limits; the gate check, the account lookup, the
  pending request and the email happen after the response
  (``process_signup_request``), so neither the answer nor its timing tells
  whether an address has an account or may sign up. 429 per client address;
  the per-address limit is silent. When the limiter cannot count (Redis
  down) nothing is scheduled and the answer is still 202. There is no
  return path in the body: the browser that started the sign-up keeps it.
- ``POST /auth/signup/email/preview`` — the address and expiry of a live
  link; consumes nothing (mail scanners may open the page).
- ``POST /auth/signup/email/complete`` — choose the password (and accept the
  terms) and create the account; answers ``{"redirect_url": ...}`` with a
  session cookie. 400 unknown / expired / used link, 403 the sign-up
  controls refuse it now, 409 the address has an account by now, 422 the
  password policy or the terms, 503 when the account was created but the
  session could not be opened (sign in with the password).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_module
from config.settings import get_settings
from db.base import get_db
from db.redis import increment_counter
from models.api_base import TZAwareBaseModel
from services.announcement_consent_service import resolve_signup_consent
from services.beta_invite_service import BETA_INVITE_TOKEN_PATTERN
from services.email_service import redact_recipient
from services.email_signup_service import (
    EmailSignupService,
    SignupConsents,
    process_signup_request,
    validate_signup_address,
)
from services.known_device_service import note_browser_sign_in
from services.workspace_service import ensure_first_workspace
from utils.exceptions import (
    RateLimitError,
    RedisError,
    SignupSessionUnavailableError,
    SurfaceSwitchedOffError,
)
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# Rate limits (fixed windows, via ``increment_counter``).
_WINDOW_SECONDS = 15 * 60
_REQUESTS_PER_IP = 10
_REQUESTS_PER_EMAIL = 3
_LINK_ATTEMPTS_PER_IP = 20

_INVITE_RE = re.compile(BETA_INVITE_TOKEN_PATTERN)


def _require_enabled() -> None:
    """404 the whole surface when email sign-up is off for this deployment.

    Starlette's default ``"Not Found"`` detail, like ``beta_invites``: the
    route must be indistinguishable from one that does not exist.
    """
    if not get_settings().enable_email_signup:
        raise SurfaceSwitchedOffError()


router = APIRouter(
    prefix="/auth/signup/email",
    tags=["authentication"],
    dependencies=[Depends(_require_enabled)],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EmailSignupRequestBody(BaseModel):
    """Body for POST /auth/signup/email/request.

    ``email`` is checked for its shape only (one ASCII mailbox), never for
    whether it exists. ``invite`` is a beta invite token; a malformed one is
    ignored, not an error.
    """

    email: str = Field(..., min_length=3, max_length=254)
    invite: str | None = Field(None, max_length=256)
    locale: str | None = Field(None, max_length=16)
    timezone: str | None = Field(None, max_length=64)

    @field_validator("email")
    @classmethod
    def _valid_address(cls, value: str) -> str:
        return validate_signup_address(value)


class EmailSignupAcceptedResponse(BaseModel):
    """Identical for every sign-up request, whatever happened after it."""

    status: Literal["accepted"] = "accepted"
    message: str = "If this address can be used to sign up, we sent a link to it."


class EmailSignupTokenBody(BaseModel):
    """Body for POST /auth/signup/email/preview."""

    token: str = Field(..., min_length=1, max_length=256)


class EmailSignupPreviewResponse(TZAwareBaseModel):
    """The address a live link was sent to, and when it expires."""

    email: str
    expires_at: datetime


class EmailSignupCompleteBody(BaseModel):
    """Body for POST /auth/signup/email/complete.

    No ``max_length`` on ``new_password``: the policy answers an over-long
    password with its own message. ``return_to`` is the path the browser
    that started the sign-up kept; it is validated like the password
    sign-in's (same-origin only).
    """

    token: str = Field(..., min_length=1, max_length=256)
    new_password: str
    name: str | None = Field(None, max_length=255)
    accepted_terms: str | None = Field(None, max_length=64)
    return_to: str | None = Field(None, max_length=2048)
    # #2761: the announcement-email opt-in and the wording version the page
    # showed. Only here: the box is answered by whoever opened the link.
    announcement_emails: bool | None = None
    announcement_consent_version: str | None = Field(None, max_length=64)


class EmailSignupCompleteResponse(BaseModel):
    """Where the browser goes next (the session cookie is set)."""

    redirect_url: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _count(key: str) -> int | None:
    """Count one request against ``key``; None when Redis cannot count."""
    try:
        return await increment_counter(key, ttl=_WINDOW_SECONDS)
    except RedisError as exc:
        logger.warning("email_signup_rate_limit_unavailable", error_type=type(exc).__name__)
        return None


def _display_name(name: str | None) -> str | None:
    """Collapse whitespace (newlines, tabs) in the chosen name; blank = none."""
    if name is None:
        return None
    collapsed = " ".join(name.split())
    return collapsed or None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/request", status_code=202, response_model=EmailSignupAcceptedResponse)
async def request_email_signup(
    body: EmailSignupRequestBody,
    request: Request,
    background_tasks: BackgroundTasks,
) -> EmailSignupAcceptedResponse:
    """Email a sign-up link if the address may sign up; always 202, same body.

    Sending fails closed: when the limiter cannot count, nothing is scheduled.
    """
    ip = auth_module._login_client_ip(request)
    per_ip = await _count(f"email_signup_ip:{ip}")
    if per_ip is None:
        return EmailSignupAcceptedResponse()
    if per_ip > _REQUESTS_PER_IP:
        raise RateLimitError(
            "Too many sign-up requests. Please try again later.", retry_after=_WINDOW_SECONDS
        )
    # A keyed digest, not the address: the key is logged when Redis fails.
    per_email = await _count(f"email_signup_email:{redact_recipient(body.email)}")
    if per_email is None or per_email > _REQUESTS_PER_EMAIL:
        logger.info("email_signup_request_throttled")
        return EmailSignupAcceptedResponse()

    invite_hash: str | None = None
    if body.invite and get_settings().enable_beta_invites and _INVITE_RE.fullmatch(body.invite):
        invite_hash = sha256_hex(body.invite)
    background_tasks.add_task(
        process_signup_request,
        email=body.email,
        beta_invite_token_hash=invite_hash,
        locale=body.locale,
        timezone=body.timezone,
    )
    return EmailSignupAcceptedResponse()


async def _link_attempt(request: Request) -> None:
    ip = auth_module._login_client_ip(request)
    count = await _count(f"email_signup_link_ip:{ip}")
    if count is not None and count > _LINK_ATTEMPTS_PER_IP:
        raise RateLimitError("Too many attempts. Please try again later.")


@router.post("/preview", response_model=EmailSignupPreviewResponse)
async def preview_email_signup(
    body: EmailSignupTokenBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> EmailSignupPreviewResponse:
    """The address and expiry of a live sign-up link. 400 when it is not live."""
    await _link_attempt(request)
    email, expires_at = await EmailSignupService(db).preview(body.token)
    return EmailSignupPreviewResponse(email=email, expires_at=expires_at)


@router.post("/complete", response_model=EmailSignupCompleteResponse)
async def complete_email_signup(
    body: EmailSignupCompleteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Create the account from a sign-up link and sign this browser in."""
    await _link_attempt(request)
    ip = auth_module._login_client_ip(request)
    created = await EmailSignupService(db).complete(
        raw_token=body.token,
        new_password=body.new_password,
        name=_display_name(body.name),
        consents=SignupConsents(
            terms_version=body.accepted_terms,
            announcement=resolve_signup_consent(
                body.announcement_emails, body.announcement_consent_version
            ),
        ),
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    # The account is committed. A personal workspace unless invitations wait
    # (the OAuth callbacks' rule); never fails the sign-up.
    await ensure_first_workspace(db, created.user_id, created.email)
    try:
        session_id = await auth_module._create_password_session(
            user_id=created.user_id,
            email=created.email,
            name=created.name,
            role=created.role.value,
        )
    except Exception as exc:
        logger.error(
            "email_signup_session_failed", user_id=created.user_id, error_type=type(exc).__name__
        )
        raise SignupSessionUnavailableError() from None
    response = Response(
        content=EmailSignupCompleteResponse(
            redirect_url=auth_module._safe_redirect_url(body.return_to)
        ).model_dump_json(),
        media_type="application/json",
    )
    auth_module._set_session_cookie(response, session_id)
    # #1769: the first browser of a new account registers silently.
    await note_browser_sign_in(
        request, response, user_id=created.user_id, sign_in_method="Password"
    )
    return response
