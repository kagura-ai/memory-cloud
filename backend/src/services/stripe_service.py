"""Stripe billing service.

Issue #351: Stripe integration for SaaS deployments.
Only active when BILLING_ENABLED=true.
"""

from uuid import UUID

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import get_plan_tier
from config.settings import get_settings
from models.auth import PlanChange, Workspace
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Plan name → Stripe Price ID mapping
_PLAN_PRICE_MAP: dict[str, str] = {}


def _init_stripe() -> None:
    """Initialize Stripe API key and price mapping."""
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise ValueError("STRIPE_SECRET_KEY is required when billing is enabled")

    stripe.api_key = settings.stripe_secret_key

    if settings.stripe_price_basic:
        _PLAN_PRICE_MAP["basic"] = settings.stripe_price_basic
    if settings.stripe_price_pro:
        _PLAN_PRICE_MAP["pro"] = settings.stripe_price_pro


def get_price_id(plan_name: str) -> str:
    """Get Stripe Price ID for a plan."""
    if not _PLAN_PRICE_MAP:
        _init_stripe()
    price_id = _PLAN_PRICE_MAP.get(plan_name)
    if not price_id:
        raise ValueError(f"No Stripe Price configured for plan: {plan_name}")
    return price_id


async def create_checkout_session(
    db: AsyncSession,
    workspace_id: UUID,
    plan_name: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Create Stripe Checkout Session for plan upgrade.

    Returns:
        Checkout session URL to redirect the user to.
    """
    _init_stripe()

    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise ValueError(f"Workspace {workspace_id} not found")

    price_id = get_price_id(plan_name)

    # Reuse existing Stripe customer or create new
    customer_id = workspace.stripe_customer_id
    checkout_params: dict = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "workspace_id": str(workspace_id),
            "plan_name": plan_name,
        },
    }

    if customer_id:
        checkout_params["customer"] = customer_id
    else:
        checkout_params["customer_creation"] = "always"

    session = stripe.checkout.Session.create(**checkout_params)

    logger.info(
        "stripe_checkout_created",
        workspace_id=str(workspace_id),
        plan_name=plan_name,
        session_id=session.id,
    )

    return session.url


async def create_portal_session(
    db: AsyncSession,
    workspace_id: UUID,
    return_url: str,
) -> str:
    """Create Stripe Customer Portal session.

    Returns:
        Portal URL to redirect the user to.
    """
    _init_stripe()

    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = result.scalar_one_or_none()
    if not workspace or not workspace.stripe_customer_id:
        raise ValueError("No Stripe customer linked to this workspace")

    session = stripe.billing_portal.Session.create(
        customer=workspace.stripe_customer_id,
        return_url=return_url,
    )

    return session.url


async def handle_webhook_event(
    db: AsyncSession,
    payload: bytes,
    signature: str,
) -> dict:
    """Handle incoming Stripe webhook event.

    Returns:
        Dict with event type and processing result.
    """
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET not configured")

    event = stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)

    event_type = event["type"]
    logger.info("stripe_webhook_received", event_type=event_type, event_id=event["id"])

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        workspace_id = session["metadata"].get("workspace_id")
        plan_name = session["metadata"].get("plan_name")
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")

        if workspace_id and plan_name:
            await _apply_plan_change(
                db, UUID(workspace_id), plan_name, customer_id, subscription_id
            )

    elif event_type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer")
        await _handle_subscription_cancelled(db, customer_id)

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        logger.warning(
            "stripe_payment_failed",
            customer_id=invoice.get("customer"),
            invoice_id=invoice.get("id"),
        )

    return {"event_type": event_type, "status": "processed"}


async def _apply_plan_change(
    db: AsyncSession,
    workspace_id: UUID,
    new_plan_name: str,
    customer_id: str | None,
    subscription_id: str | None,
) -> None:
    """Apply plan change after successful checkout."""
    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = result.scalar_one_or_none()
    if not workspace:
        logger.error("stripe_workspace_not_found", workspace_id=str(workspace_id))
        return

    old_plan = workspace.plan_name
    old_memory_limit = workspace.memory_limit
    new_tier = get_plan_tier(new_plan_name)

    # Update workspace
    workspace.plan_name = new_plan_name
    workspace.memory_limit = new_tier.memory_limit
    workspace.daily_api_limit = new_tier.daily_api_limit
    workspace.weekly_api_limit = new_tier.weekly_api_limit
    if customer_id:
        workspace.stripe_customer_id = customer_id
    if subscription_id:
        workspace.stripe_subscription_id = subscription_id

    # Audit log
    audit = PlanChange(
        workspace_id=workspace_id,
        old_plan=old_plan,
        new_plan=new_plan_name,
        changed_by="stripe",
        changed_at=utcnow(),
        reason="Stripe checkout completed",
        old_memory_limit=old_memory_limit,
        new_memory_limit=new_tier.memory_limit,
    )
    db.add(audit)
    await db.commit()

    logger.info(
        "stripe_plan_changed",
        workspace_id=str(workspace_id),
        old_plan=old_plan,
        new_plan=new_plan_name,
    )


async def _handle_subscription_cancelled(
    db: AsyncSession,
    customer_id: str,
) -> None:
    """Downgrade to free when subscription is cancelled."""
    result = await db.execute(select(Workspace).where(Workspace.stripe_customer_id == customer_id))
    workspace = result.scalar_one_or_none()
    if not workspace:
        logger.warning("stripe_customer_not_found", customer_id=customer_id)
        return

    free_tier = get_plan_tier("free")
    old_plan = workspace.plan_name

    workspace.plan_name = "free"
    workspace.memory_limit = free_tier.memory_limit
    workspace.daily_api_limit = free_tier.daily_api_limit
    workspace.weekly_api_limit = free_tier.weekly_api_limit
    workspace.stripe_subscription_id = None

    audit = PlanChange(
        workspace_id=workspace.id,
        old_plan=old_plan,
        new_plan="free",
        changed_by="stripe",
        changed_at=utcnow(),
        reason="Subscription cancelled",
    )
    db.add(audit)
    await db.commit()

    logger.info(
        "stripe_subscription_cancelled",
        workspace_id=str(workspace.id),
        old_plan=old_plan,
    )


async def cancel_subscription_and_delete_customer_for_erasure(
    workspace: Workspace,
) -> dict[str, bool]:
    """Cancel Stripe subscription and delete customer for GDPR right-to-erasure.

    Issue #360: AccountErasureService calls this once per owned workspace
    that has a Stripe customer linked. Best-effort: every Stripe call is
    wrapped — failures are logged and the erasure flow continues. The
    workspace row is about to be deleted regardless, so an orphaned Stripe
    customer is the worst case (and is recoverable by ops via the Stripe
    dashboard if it ever happens).

    No-op when ``BILLING_ENABLED`` is false or when the workspace has no
    Stripe IDs populated, so it is safe to call unconditionally.

    Args:
        workspace: Workspace ORM instance whose Stripe state should be
            torn down.

    Returns:
        ``{"subscription_cancelled": bool, "customer_deleted": bool}`` —
        included verbatim in the deleted_data_summary JSONB on the
        ``erasure_requests`` row for audit.
    """
    from plugins.billing import is_billing_enabled

    result = {"subscription_cancelled": False, "customer_deleted": False}

    if not is_billing_enabled():
        return result

    if not workspace.stripe_customer_id and not workspace.stripe_subscription_id:
        return result

    _init_stripe()

    if workspace.stripe_subscription_id:
        try:
            stripe.Subscription.cancel(workspace.stripe_subscription_id)
            result["subscription_cancelled"] = True
            logger.info(
                "stripe_subscription_cancelled_for_erasure",
                workspace_id=str(workspace.id),
                subscription_id=workspace.stripe_subscription_id,
            )
        except Exception as e:
            logger.error(
                "stripe_subscription_cancel_failed_during_erasure",
                workspace_id=str(workspace.id),
                subscription_id=workspace.stripe_subscription_id,
                error=str(e),
            )

    if workspace.stripe_customer_id:
        try:
            stripe.Customer.delete(workspace.stripe_customer_id)
            result["customer_deleted"] = True
            logger.info(
                "stripe_customer_deleted_for_erasure",
                workspace_id=str(workspace.id),
                customer_id=workspace.stripe_customer_id,
            )
        except Exception as e:
            logger.error(
                "stripe_customer_delete_failed_during_erasure",
                workspace_id=str(workspace.id),
                customer_id=workspace.stripe_customer_id,
                error=str(e),
            )

    return result
