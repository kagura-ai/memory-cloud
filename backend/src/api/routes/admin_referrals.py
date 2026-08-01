"""Admin referral ledger + revoke (Issue #1470).

Routes under ``/api/v1/admin/referrals`` — system-admin only via ``AdminUser``.

These ship in the SAME change as the grant engine on purpose. A feature that
mints entitlement without a ledger view and a per-grant undo cannot answer
"who got what, and why" during an abuse incident, and the only alternative
remediation would be hand-editing production rows.

Note the deliberate divergence from the closest precedent: ``SignupGateService``
writes its config with no ``AuditLog`` row and not even a log line
(``services/signup_gate_service.py`` ``update_config``). That is tolerable for a
boolean gate; it is not tolerable here, because a revoke removes quota a user is
actively storing against. The audit shape is copied from the
``workspace_slot_bonus`` PATCH in ``api/routes/admin.py`` instead, including its
required-reason rule.

Unlike the user-facing routes, these are NOT gated on
``settings.enable_referrals``: an operator must be able to inspect and unwind
existing grants after flipping the kill switch off.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import AuditLog
from services.referral_service import ReferralService
from utils.db_helpers import db_transaction
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/referrals", tags=["admin-referrals"])


class AdminReferralGrantResponse(TZAwareBaseModel):
    """A ledger row with both sides visible."""

    id: str
    referrer_user_id: str | None
    referrer_workspace_id: str | None
    referred_user_id: str | None
    referred_workspace_id: str | None
    referrer_bonus_memories: int
    referred_bonus_memories: int
    granted_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None


class AdminReferralListResponse(BaseModel):
    """Paged ledger listing."""

    items: list[AdminReferralGrantResponse]
    total: int
    limit: int
    offset: int


class RevokeRequest(BaseModel):
    """Revocation payload.

    ``reason`` is required — not optional-with-a-default. Revoking shrinks a
    live quota, so there is no non-destructive case in which an empty reason
    would be acceptable (contrast ``admin.py``'s conditional-reason rule for
    ``workspace_slot_bonus``, where a non-negative delta is harmless).
    """

    reason: str = Field(min_length=1, max_length=500)


def _to_response(grant) -> AdminReferralGrantResponse:  # noqa: ANN001 - ORM row
    """Map a ledger row onto the admin read model."""
    return AdminReferralGrantResponse(
        id=str(grant.id),
        referrer_user_id=grant.referrer_user_id,
        referrer_workspace_id=(
            str(grant.referrer_workspace_id) if grant.referrer_workspace_id else None
        ),
        referred_user_id=grant.referred_user_id,
        referred_workspace_id=(
            str(grant.referred_workspace_id) if grant.referred_workspace_id else None
        ),
        referrer_bonus_memories=grant.referrer_bonus_memories,
        referred_bonus_memories=grant.referred_bonus_memories,
        granted_at=grant.granted_at,
        revoked_at=grant.revoked_at,
        revoked_reason=grant.revoked_reason,
    )


@router.get("", response_model=AdminReferralListResponse)
async def list_referral_grants(
    user: AdminUser,
    referrer_user_id: str | None = Query(default=None),
    include_revoked: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> AdminReferralListResponse:
    """List referral grants, newest first.

    Args:
        user: Authenticated system admin.
        referrer_user_id: Restrict to a single inviter.
        include_revoked: Include already-revoked rows (default true).
        limit: Page size.
        offset: Page offset.
        db: Database session.

    Returns:
        A page of ledger rows plus the total count.
    """
    service = ReferralService(db)
    rows, total = await service.list_grants(
        referrer_user_id=referrer_user_id,
        include_revoked=include_revoked,
        limit=limit,
        offset=offset,
    )
    return AdminReferralListResponse(
        items=[_to_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{grant_id}/revoke", response_model=AdminReferralGrantResponse)
async def revoke_referral_grant(
    grant_id: uuid.UUID,
    payload: RevokeRequest,
    user: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> AdminReferralGrantResponse:
    """Revoke a grant and recompute both sides' referral bonuses.

    Idempotent: revoking an already-revoked grant is a no-op that still returns
    the row, so an admin retry (or a double-click) is harmless.

    Args:
        grant_id: The ledger row to revoke.
        payload: Revocation reason.
        user: Authenticated system admin.
        db: Database session.

    Returns:
        The revoked grant.

    Raises:
        HTTPException: 404 if no such grant exists.
    """
    service = ReferralService(db)
    actor_id = user.get("user_id", "unknown")

    async with db_transaction(db, "revoke_referral_grant", "Failed to revoke referral grant"):
        grant = await service.revoke(grant_id=grant_id, reason=payload.reason)
        if grant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Referral grant not found",
            )
        audit = AuditLog(
            user_email=user.get("email", actor_id),
            user_id=actor_id,
            action="referral_grant_revoke",
            resource=f"referral_grant:{grant_id}",
            old_value_hash=str(grant.referrer_bonus_memories + grant.referred_bonus_memories),
            new_value_hash="0",
            user_metadata={
                "actor_user_id": actor_id,
                "referrer_user_id": grant.referrer_user_id,
                "referred_user_id": grant.referred_user_id,
                "referrer_bonus_memories": grant.referrer_bonus_memories,
                "referred_bonus_memories": grant.referred_bonus_memories,
                "reason": payload.reason,
            },
        )
        db.add(audit)
        await db.commit()

    return _to_response(grant)
