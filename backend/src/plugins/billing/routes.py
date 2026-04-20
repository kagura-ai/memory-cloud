"""Billing Plugin Routes.

Issue #351: Self-service plan management powered by Stripe.
These routes are only registered when BILLING_ENABLED=true.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceAdmin
from db.base import get_db
from services import stripe_service
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    """Request to create a Stripe checkout session."""

    plan_name: str  # "basic" or "pro"
    success_url: str
    cancel_url: str


class CheckoutResponse(BaseModel):
    """Checkout session response."""

    checkout_url: str


class PortalResponse(BaseModel):
    """Customer portal response."""

    portal_url: str


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    request: CheckoutRequest,
    user: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe Checkout Session for plan upgrade.

    Admin/owner-only (Issue #398). Redirects user to Stripe-hosted payment page.
    """
    workspace_id = user["current_workspace_id"]

    if request.plan_name not in ("basic", "pro"):
        raise HTTPException(status_code=400, detail="Invalid plan. Must be 'basic' or 'pro'")

    try:
        checkout_url = await stripe_service.create_checkout_session(
            db=db,
            workspace_id=workspace_id,
            plan_name=request.plan_name,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
        )
        return CheckoutResponse(checkout_url=checkout_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("stripe_checkout_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create checkout session") from e


@router.post("/webhook")
async def handle_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Stripe webhook events.

    No auth required — verified via Stripe webhook signature.
    """
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    if not signature:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        result = await stripe_service.handle_webhook_event(db, payload, signature)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("stripe_webhook_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Webhook processing failed") from e


@router.get("/portal", response_model=PortalResponse)
async def get_portal(
    return_url: str,
    user: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
):
    """Get Stripe Customer Portal URL.

    Admin/owner-only (Issue #398). Allows managing payment methods and subscriptions.
    """
    workspace_id = user["current_workspace_id"]

    try:
        portal_url = await stripe_service.create_portal_session(
            db=db,
            workspace_id=workspace_id,
            return_url=return_url,
        )
        return PortalResponse(portal_url=portal_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error("stripe_portal_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create portal session") from e
