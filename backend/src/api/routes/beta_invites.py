"""Closed-beta invite link endpoints (Issues #1581, #1595).

Routes under ``/api/v1/beta-invites``. A signed-in user mints a one-time
``/join/{token}`` URL that lets one new person through the admin-configured
signup gate; the redemption itself happens at the OAuth callback
(``services/signup_gate_service.py``), not here.

The inviter-facing routes are session-authenticated only — minting an invite is
an act by a human in a browser, and an API key that could mint them would be a
scriptable account-creation endpoint. The preview is public: the invitee has no
account yet.

Every route 404s when ``settings.enable_beta_invites`` is false (the referrals
#1470 precedent); the flag is surfaced read-only via
``GET /api/v1/system/info`` ``features.beta_invites`` so the web UI hides its
entry points.

The plaintext URL appears in the response that mints it — ``POST /beta-invites``
or ``POST /beta-invites/{id}/reissue`` — exactly once, and is never logged. Every
other payload and every error names an invite by ``id``.

#1595: an invite's ``label`` (the inviter's free-text note) and ``redeemed_email``
(the admitted account's address) are returned to the inviter and appear nowhere
else — nothing in this module logs either, and no error message carries them.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from config.settings import get_settings
from db.base import get_db
from db.redis import incrby_counter
from models.api_base import TZAwareBaseModel
from models.beta_invite import BetaInviteStatus
from services.beta_invite_service import (
    BETA_INVITE_TOKEN_PATTERN,
    BetaInviteService,
    MintedBetaInvite,
    normalize_beta_invite_label,
)
from utils.exceptions import NotFoundException, RateLimitError
from utils.logger import get_logger

logger = get_logger(__name__)

# Per-IP budget for the public preview. A landing-page load is one call, so this
# is generous for people behind a shared NAT and useless for guessing 256-bit
# tokens — it exists to keep an anonymous endpoint from being a free DB-read tap.
PREVIEW_RATE_LIMIT_PER_MINUTE = 30

_TOKEN_RE = re.compile(BETA_INVITE_TOKEN_PATTERN)


def _require_enabled() -> None:
    """404 the whole surface when invite links are switched off for this deployment.

    Starlette's default ``"Not Found"`` detail, deliberately (same reasoning as
    ``referrals._require_enabled``): a feature-specific message would make the
    route distinguishable from one that does not exist at all.
    """
    if not get_settings().enable_beta_invites:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


# Declared as a ROUTER-level dependency, listed first, so it is evaluated before
# the per-route auth dependency: an unauthenticated caller must get the same 404
# as an authenticated one, or the 401/404 split advertises the endpoint.
router = APIRouter(
    prefix="/beta-invites",
    tags=["beta-invites"],
    dependencies=[Depends(_require_enabled)],
)


class BetaInviteCreateRequest(BaseModel):
    """Optional body of ``POST /beta-invites`` (#1595). No body at all is valid."""

    label: str | None = None

    @field_validator("label")
    @classmethod
    def _normalize_label(cls, value: str | None) -> str | None:
        # Trim; blank -> None; > 100 chars, a control character or a lone
        # surrogate -> 422. The ``ValueError`` text reaches the 422 body, so it
        # never quotes the label.
        return normalize_beta_invite_label(value)


class BetaInviteItem(TZAwareBaseModel):
    """One invite as its inviter sees it.

    Lifecycle, the inviter's own ``label``, and — for a ``redeemed`` invite whose
    admitted account still exists — that account's current e-mail. #1581 showed
    status only; #1595 reverses that on purpose: the inviter is the person who
    vouched for the invitee, so this is not a new disclosure. ``redeemed_email``
    is ``null`` for every other status, and turns ``null`` again once the account
    is erased. Never the token, its hash or the URL.
    """

    id: str
    status: BetaInviteStatus
    label: str | None
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None
    revoked_at: datetime | None
    redeemed_email: str | None


class BetaInviteSummaryResponse(TZAwareBaseModel):
    """The caller's quota standing. ``null`` quota / remaining = unlimited (admin).

    ``used`` counts the invites occupying a slot; ``active`` and ``redeemed`` are
    its two parts (``active + redeemed == used``), so a client never re-derives
    them from the list.
    """

    quota: int | None
    used: int
    active: int
    redeemed: int
    remaining: int | None
    invites: list[BetaInviteItem]


class BetaInviteCreatedResponse(TZAwareBaseModel):
    """A freshly minted invite — the only payload that ever carries the URL.

    Returned by both create and reissue.
    """

    id: str
    url: str
    expires_at: datetime
    label: str | None


class BetaInvitePreviewResponse(TZAwareBaseModel):
    """What the public landing page may know about a usable link."""

    valid: bool
    expires_at: datetime


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _created_response(minted: MintedBetaInvite) -> BetaInviteCreatedResponse:
    return BetaInviteCreatedResponse(
        id=str(minted.invite.id),
        url=minted.url,
        expires_at=minted.invite.expires_at,
        label=minted.invite.label,
    )


async def _check_preview_rate_limit(client_ip: str) -> None:
    """Per-IP minute bucket for the unauthenticated preview.

    Same primitive as the public-search buckets (``incrby_counter`` sets the TTL
    whenever it is absent, so a lost first-increment race cannot leave a counter
    that never expires). Fails open on Redis trouble: a limiter outage must not
    take the signup landing page down with it.

    Args:
        client_ip: The caller's address — the bucket key. Never the token.

    Raises:
        RateLimitError: 429 when the bucket is exhausted.
    """
    try:
        count = await incrby_counter(f"beta_invite_preview:{client_ip}:minute", amount=1, ttl=60)
    except Exception as exc:  # noqa: BLE001 - fail open, see docstring
        logger.error("beta_invite_preview_rate_limit_check_failed", error=str(exc))
        return
    if count > PREVIEW_RATE_LIMIT_PER_MINUTE:
        logger.warning("beta_invite_preview_rate_limit_exceeded", ip=client_ip, count=count)
        raise RateLimitError(message="Too many requests. Please try again later.", retry_after=60)


@router.get("/me", response_model=BetaInviteSummaryResponse)
async def get_my_beta_invites(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> BetaInviteSummaryResponse:
    """Return the caller's invite quota standing and their invites, newest first.

    Args:
        user: Authenticated session user.
        db: Database session.

    Returns:
        ``quota`` / ``used`` (= ``active`` + ``redeemed``) / ``remaining`` and the
        per-invite lifecycle, label and — for redeemed invites — admitted e-mail.
    """
    summary = await BetaInviteService(db).get_summary(user["user_id"])
    items = []
    for invite in summary.invites:
        invite_status = invite.status
        items.append(
            BetaInviteItem(
                id=str(invite.id),
                status=invite_status,
                label=invite.label,
                created_at=invite.created_at,
                expires_at=invite.expires_at,
                redeemed_at=invite.redeemed_at,
                revoked_at=invite.revoked_at,
                # Only ever on a ``redeemed`` row, whatever the mapping holds.
                redeemed_email=(
                    summary.redeemed_emails.get(invite.id) if invite_status == "redeemed" else None
                ),
            )
        )
    return BetaInviteSummaryResponse(
        quota=summary.quota,
        used=summary.used,
        active=summary.active,
        redeemed=summary.redeemed,
        remaining=summary.remaining,
        invites=items,
    )


@router.post("", response_model=BetaInviteCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_beta_invite(
    request: Request,
    user: SessionUser,
    body: BetaInviteCreateRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> BetaInviteCreatedResponse:
    """Mint a one-time invite link. The URL is returned here and never again.

    Args:
        request: FastAPI request (IP / User-Agent for the audit row).
        user: Authenticated session user (the inviter).
        body: Optional ``{"label": ...}`` (#1595). Clients that predate it send
            no body at all, which keeps working.
        db: Database session.

    Returns:
        The invite id, its plaintext URL, its expiry and its label.

    Raises:
        BetaInviteQuotaExceededError: 409 ``BETA-INVITE-001`` at the cap.
    """
    minted = await BetaInviteService(db).create(
        user_id=user["user_id"],
        user_email=user.get("email", user["user_id"]),
        label=body.label if body is not None else None,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _created_response(minted)


@router.post(
    "/{invite_id}/reissue",
    response_model=BetaInviteCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def reissue_beta_invite(
    invite_id: UUID,
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> BetaInviteCreatedResponse:
    """Replace an own ``active`` or ``expired`` invite with a fresh link (#1595).

    For a recipient who lost the link: only the hash is stored, so the URL cannot
    be shown again. The old invite is revoked and a new one carrying the same
    label is minted in one transaction; the quota standing of an ``active``
    invite is unchanged. Takes no body.

    Args:
        invite_id: The invite to replace.
        request: FastAPI request (IP / User-Agent for the audit rows).
        user: Authenticated session user.
        db: Database session.

    Returns:
        Exactly the create payload — the new invite and its plaintext URL, once.

    Raises:
        NotFoundException: 404 — unknown id, or someone else's invite.
        BetaInviteAlreadyRedeemedError: 409 ``BETA-INVITE-002``.
        BetaInviteAlreadyRevokedError: 409 ``BETA-INVITE-003`` — also what the
            second request of a double-click gets; it mints nothing.
        BetaInviteQuotaExceededError: 409 ``BETA-INVITE-001`` — an ``expired``
            invite holds no slot, so reissuing one at the cap is refused (and the
            invite is left as it was).
    """
    minted = await BetaInviteService(db).reissue(
        user_id=user["user_id"],
        invite_id=invite_id,
        user_email=user.get("email", user["user_id"]),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _created_response(minted)


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_beta_invite(
    invite_id: UUID,
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Revoke one of the caller's own unused invites (frees its quota slot).

    Args:
        invite_id: The invite to revoke.
        request: FastAPI request (IP / User-Agent for the audit row).
        user: Authenticated session user.
        db: Database session.

    Returns:
        204 No Content.

    Raises:
        NotFoundException: 404 — unknown id, or someone else's invite.
        BetaInviteAlreadyRedeemedError: 409 ``BETA-INVITE-002``.
    """
    await BetaInviteService(db).revoke(
        user_id=user["user_id"],
        invite_id=invite_id,
        user_email=user.get("email", user["user_id"]),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{token}/preview", response_model=BetaInvitePreviewResponse)
async def preview_beta_invite(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BetaInvitePreviewResponse:
    """Tell the public landing page whether a link is still usable. No auth.

    Read-only — previewing never consumes the invite. Reveals nothing about the
    inviter.

    Args:
        token: The plaintext token from the ``/join/{token}`` URL.
        request: FastAPI request (caller IP for the rate-limit bucket).
        db: Database session.

    Returns:
        ``{"valid": true, "expires_at": ...}`` for a usable link.

    Raises:
        RateLimitError: 429 when the caller's per-IP bucket is exhausted.
        NotFoundException: 404 — unknown, revoked, or malformed token.
        BetaInviteGoneError: 410 — expired or already redeemed.
    """
    await _check_preview_rate_limit(_client_ip(request))
    if not _TOKEN_RE.fullmatch(token):
        # Cannot be a token we minted — answer without a DB round trip.
        raise NotFoundException("Beta invite")
    invite = await BetaInviteService(db).preview(token)
    return BetaInvitePreviewResponse(valid=True, expires_at=invite.expires_at)
