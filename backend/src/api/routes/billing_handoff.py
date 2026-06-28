"""Owner-only billing handoff endpoint (#1093).

``POST /api/v1/billing/handoff`` lets an authenticated workspace **owner** start
a billing session on the external billing host without that host re-implementing
user auth. It mints a short-lived Ed25519-signed token (see
``auth.billing_handoff.BillingHandoffSigner``) bound to
``(user_id, workspace_id, role=owner)``.

Two-layer gate, deliberately strict:

1. **session-auth only** (``SessionUser`` → ``require_session_auth``): a Bearer
   credential (API key / OAuth) is rejected 403. A leaked long-lived API key must
   never be able to open a billing session (mirrors the #398 billing RBAC stance).
2. **owner-only against the explicit target workspace**: the owner check runs on
   the request-body ``workspace_id``, never the caller's mutable
   ``current_workspace_id`` — closing the #389 multi-workspace cross-tenant trap.

Stripe-agnostic and always mounted (unlike the ``BILLING_ENABLED``-gated Stripe
plugin): the token is the auth primitive the billing host consumes, independent
of which payment processor it wraps.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.billing_handoff import BillingHandoffSigner
from auth.dependencies import SessionUser
from config.settings import get_settings
from db.base import get_db
from db.redis import incrby_counter
from models.api_base import TZAwareBaseModel
from models.auth import Workspace
from services.permission_service import PermissionService
from utils.auth_helpers import get_user_id
from utils.exceptions import RateLimitError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["billing-handoff"])

# Per-(owner, workspace) mint rate-limit window. Mirrors the Redis counter idiom
# in ``public_search.check_bound_key_rate_limit`` (#626): ``incrby_counter`` sets
# a TTL even under concurrent first-increments so the bucket cannot grow forever.
_RATE_WINDOW_SECONDS = 60


async def _check_handoff_rate_limit(
    user_id: str, workspace_id: UUID | str, limit_per_minute: int
) -> None:
    """Bound a single owner's mint rate for one workspace (#1104).

    Defense-in-depth on top of the owner gate + short-TTL token: caps abusive
    minting per ``(user_id, workspace_id)`` per minute. ``limit<=0`` disables
    minting (fail-safe). **Fail-open** on a Redis outage — the primary controls
    (owner-only + session-only + short TTL + audit log) still hold, and a billing
    handoff must not become unavailable because the rate-limit store is down
    (consistent with the ``public_search`` buckets).

    Raises:
        RateLimitError: 429 when the per-minute bucket is exhausted (or disabled).
    """
    if limit_per_minute <= 0:
        raise RateLimitError(
            message="Billing handoff minting is not available on this deployment",
            retry_after=_RATE_WINDOW_SECONDS,
        )

    redis_key = f"billing_handoff:{user_id}:{workspace_id}:minute"
    try:
        current = await incrby_counter(redis_key, amount=1, ttl=_RATE_WINDOW_SECONDS)
        if current > limit_per_minute:
            logger.warning(
                "billing_handoff_rate_limit_exceeded",
                user_id=user_id,
                workspace_id=str(workspace_id),
                current=current,
                limit=limit_per_minute,
            )
            raise RateLimitError(
                message=(
                    f"Billing handoff rate limit exceeded: {current}/{limit_per_minute} per minute"
                ),
                retry_after=_RATE_WINDOW_SECONDS,
            )
    except RateLimitError:
        raise
    except Exception as exc:
        # Fail-open on any Redis error — availability of the handoff must not
        # depend on the rate-limit store (mirrors public_search's buckets).
        logger.error("billing_handoff_rate_limit_check_failed", error=str(exc))


def _build_handoff_url(base_url: str, token: str) -> str | None:
    """Build the ready-to-use handoff redirect URL, or None when unconfigured (#1118).

    ``{base}/enter?t={token}`` — ``/enter?t=`` is the FROZEN cross-repo handoff
    entry contract; this only materializes ``{base}`` (operator config) + that path.
    Returns None when ``base_url`` is empty/blank (including a slash-only value) so
    the response keeps the decoupled raw-token shape (#1098). No query-encoding is
    needed: the token is a URL-safe JWT (base64url segments + '.'), and there is no
    user input in the URL (``base_url`` is trusted, startup-validated operator
    config), so this cannot be injection-shaped.
    """
    # rstrip BEFORE the empty-guard so a slash-only base ("/", "//") collapses to
    # "" → None, not a relative "/enter?t=..." that resolves against the API host.
    base = base_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/enter?t={token}"


class BillingHandoffRequest(BaseModel):
    """Request to mint a handoff token for one owned workspace."""

    workspace_id: UUID = Field(
        ...,
        description=(
            "The workspace to start a billing session for. The caller MUST be "
            "its owner — the owner check binds to this value, not the session's "
            "current workspace."
        ),
    )


class BillingHandoffResponse(TZAwareBaseModel):
    """The minted handoff token and the metadata the billing host needs."""

    token: str = Field(..., description="Ed25519-signed JWT (alg=EdDSA).")
    token_type: str = Field(default="billing_handoff", description="Token kind discriminator.")
    kid: str = Field(..., description="Signing key id — selects the verifier's public key.")
    jti: str = Field(..., description="Unique token id (verifier enforces single-use).")
    expires_at: datetime = Field(..., description="Token expiry (UTC, short-lived).")
    url: str | None = Field(
        default=None,
        description=(
            "Ready-to-use redirect URL ({base}/enter?t={token}) when "
            "payment_public_base_url is configured; null otherwise (the decoupled "
            "raw-token contract, #1098). The caller redirects the owner's browser here."
        ),
    )


@router.post("/handoff", response_model=BillingHandoffResponse)
async def mint_billing_handoff(
    body: BillingHandoffRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> BillingHandoffResponse:
    """Mint an owner-scoped, short-lived billing handoff token.

    Owner-only + session-only. Returns 403 for admin/member/API-key callers and
    503 when the signing key is unconfigured (fail-closed).

    The token's ``iss`` / ``aud`` claims (``billing_handoff_issuer`` /
    ``billing_handoff_audience``) are FROZEN cross-repo JWT contract values
    verified by the external billing service — they are NOT repo or service
    names. ``billing`` is the function; the external service provides it.
    Changing either claim is a coordinated breaking change (the verifier must
    change in lockstep), never a local rename.
    """
    user_id = get_user_id(user)
    settings = get_settings()

    # Owner gate against the EXPLICIT target workspace — check_workspace_owner
    # raises AuthorizationError (403) for non-owners and never trusts
    # current_workspace_id, so a multi-workspace member cannot mint for a
    # workspace they merely belong to (#389 guard).
    await PermissionService(db).check_workspace_owner(user_id, body.workspace_id)

    # Rate-limit the owner's mint rate per workspace (#1104), AFTER the owner gate
    # (non-owners 403 without consuming quota) and BEFORE mint. 429 on exceed.
    await _check_handoff_rate_limit(
        user_id,
        body.workspace_id,
        settings.billing_handoff_rate_limit_per_minute,
    )

    # Stamp the workspace's live ownership epoch into the token (#1100) so the
    # external verifier can reject it once ownership is transferred (the epoch
    # advances). Read AFTER the owner gate confirmed the workspace exists; a newer
    # epoch read here would only make the token immediately stale (fail-safe).
    ownership_epoch = (
        await db.execute(
            select(Workspace.ownership_epoch).where(
                Workspace.id == body.workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    # Mint AFTER authz so an unconfigured deployment returns 403 to non-owners
    # (no info leak) and 503 only to an authorized owner.
    minted = BillingHandoffSigner().mint(
        user_id=user_id,
        workspace_id=body.workspace_id,
        ownership_epoch=ownership_epoch,
    )

    # Issuance is audit-logged exactly once, inside BillingHandoffSigner.mint()
    # as "billing_handoff_minted" (same fields + expires_at) — no duplicate here.
    return BillingHandoffResponse(
        token=minted.token,
        kid=minted.kid,
        jti=minted.jti,
        expires_at=minted.expires_at,
        # Opt-in convenience (#1118): a ready-to-use redirect URL when the billing
        # host base is configured; None otherwise (raw-token contract preserved).
        url=_build_handoff_url(settings.payment_public_base_url, minted.token),
    )
