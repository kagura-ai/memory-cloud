"""User-facing referral endpoints (Issue #1470).

Routes under ``/api/v1/referrals``. Session-authenticated only — a referral is
an act by a human in a browser, and allowing an API key to redeem would hand a
farmer a scriptable endpoint.

Every route 404s when ``settings.enable_referrals`` is false, matching the BYOK
(#1167) and Plan-page (#1145) precedent: the deployment flag is surfaced
read-only via ``GET /api/v1/system/info`` ``features.referrals`` so the web UI
hides the card instead of rendering a control that always fails.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from config.settings import get_settings
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.referral_service import ReferralService
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/referrals", tags=["referrals"])


def _require_enabled() -> None:
    """404 the whole surface when the program is switched off for this deployment."""
    if not get_settings().enable_referrals:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Referral program is not enabled",
        )


class ReferralSummaryResponse(BaseModel):
    """The inviter's own standing.

    Reward amounts are included because the user is being asked to share a link
    — they need to know what they and their invitee get. This is not the same as
    exposing them publicly: the route requires a session.
    """

    code: str
    max_grants: int
    used_grants: int
    remaining_grants: int
    referee_reward_memories: int
    referrer_reward_memories: int
    earned_memories: int


class RedeemRequest(BaseModel):
    """A referral code submitted by a newly-signed-up user."""

    code: str = Field(min_length=1, max_length=24)


class ReferralGrantResponse(TZAwareBaseModel):
    """A ledger row as seen by the user who earned it.

    Inherits ``TZAwareBaseModel`` (not ``BaseModel``) because it carries
    datetimes: the naive-UTC columns must serialize with a ``Z`` suffix or JS
    clients read them as local time.
    """

    id: str
    granted_at: datetime
    referrer_bonus_memories: int
    referred_bonus_memories: int
    revoked_at: datetime | None


@router.get("/me", response_model=ReferralSummaryResponse)
async def get_my_referral(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> ReferralSummaryResponse:
    """Return the caller's referral code and standing, minting a code on first call.

    Args:
        user: Authenticated session user.
        db: Database session.

    Returns:
        The caller's referral summary.
    """
    _require_enabled()
    service = ReferralService(db)
    summary = await service.get_summary(user["user_id"])
    return ReferralSummaryResponse(
        code=summary.code,
        max_grants=summary.max_grants,
        used_grants=summary.used_grants,
        remaining_grants=max(0, summary.max_grants - summary.used_grants),
        referee_reward_memories=summary.referee_reward_memories,
        referrer_reward_memories=summary.referrer_reward_memories,
        earned_memories=summary.earned_memories,
    )


@router.post("/redeem", response_model=ReferralGrantResponse, status_code=status.HTTP_201_CREATED)
async def redeem_referral(
    payload: RedeemRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> ReferralGrantResponse:
    """Redeem a referral code and credit both parties.

    Refusals are 400s with a ``REFERRAL-*`` error code (see
    ``utils/exceptions.py``); they are deliberately uniform so probing a code
    reveals nothing about whether it exists or who owns it.

    Args:
        payload: The submitted code.
        user: Authenticated session user (the invitee).
        db: Database session.

    Returns:
        The created grant.
    """
    _require_enabled()
    service = ReferralService(db)
    grant = await service.redeem(referred_user_id=user["user_id"], code=payload.code)
    return ReferralGrantResponse(
        id=str(grant.id),
        granted_at=grant.granted_at,
        referrer_bonus_memories=grant.referrer_bonus_memories,
        referred_bonus_memories=grant.referred_bonus_memories,
        revoked_at=grant.revoked_at,
    )
