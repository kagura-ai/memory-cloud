"""Owner-only billing handoff endpoint (Issue #1093 — RFC kagura-payment §1).

``POST /api/v1/billing/handoff`` mints a short-lived Ed25519-signed JWT that the
external payment service (payment.kagura-ai.com) redeems to establish the
user-auth handoff (Layer-1). memory-cloud stays the source of truth for *who*
the user is and *which* workspace they own; the payment service trusts the
signed assertion instead of re-authenticating.

Security posture:

- **Owner-only, session-only.** Guarded by ``WorkspaceOwnerSession`` so only a
  workspace owner on a real browser session can mint a handoff. A leaked API
  key / OAuth bearer is rejected at the door (403) — the dependency chains
  through ``require_session_auth``.
- **No request-controlled claims.** The token is minted from
  ``(user["user_id"], user["current_workspace_id"])`` only; the request has no
  body. This forecloses a member from minting an owner token for another
  workspace.
- **Fail-closed.** With no signing key configured the mint raises HANDOFF-001
  (503) — the endpoint never returns an unsigned/forgeable token.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from auth.dependencies import WorkspaceOwnerSession
from config.settings import get_settings
from services.billing_handoff_service import HANDOFF_TTL_SECONDS, mint_handoff_token
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["billing-handoff"])


class HandoffResult(BaseModel):
    """Redirect target for the payment-service handoff."""

    url: str
    expires_in: int


@router.post("/handoff", response_model=HandoffResult)
async def create_billing_handoff(user: WorkspaceOwnerSession) -> HandoffResult:
    """Mint a short-lived handoff token for the calling owner's workspace.

    Returns the payment-service ``/enter`` URL carrying the signed token and the
    token TTL. Owner-only, session-only; fails closed (503 HANDOFF-001) when the
    signing key is unset.
    """
    settings = get_settings()
    user_id = user["user_id"]
    workspace_id = user["current_workspace_id"]

    # mint_handoff_token raises HANDOFF-001 (503) when fail-closed.
    token = mint_handoff_token(user_id, workspace_id)

    logger.info(
        "billing_handoff_minted",
        user_id=user_id,
        workspace_id=str(workspace_id),
        expires_in=HANDOFF_TTL_SECONDS,
    )
    base = settings.payment_public_base_url.rstrip("/")
    return HandoffResult(url=f"{base}/enter?t={token}", expires_in=HANDOFF_TTL_SECONDS)
