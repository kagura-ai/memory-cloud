"""Stripe billing service.

Issue #351: Stripe integration for SaaS deployments.
Issue #468: synchronous stripe-python calls are wrapped via asyncio
(see in-body docs at the executor declaration).
Only active when BILLING_ENABLED=true.
"""

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar
from uuid import UUID

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import get_plan_tier
from config.settings import get_settings
from models.auth import ENTITLEMENT_SOURCE_ADMIN_GRANT, PlanChange, Workspace
from utils.datetime import utcnow
from utils.exceptions import StripeError
from utils.logger import get_logger

logger = get_logger(__name__)

# Plan name → Stripe Price ID mapping
_PLAN_PRICE_MAP: dict[str, str] = {}

# Issue #468: dedicated ThreadPoolExecutor for the GDPR erasure sweep so a
# burst of erasure requests cannot starve checkout/portal traffic on the
# default ``asyncio.to_thread`` pool. The sweep is internally serialized
# (one workspace at a time, cancel → delete in order), so workers=2 is
# enough to allow concurrent erasures across users while keeping the pool
# small. Lazy-initialized to avoid creating threads when billing is off.
_erasure_executor: ThreadPoolExecutor | None = None

T = TypeVar("T")


def _get_erasure_executor() -> ThreadPoolExecutor:
    """Lazily create and return the dedicated erasure ThreadPoolExecutor."""
    global _erasure_executor
    if _erasure_executor is None:
        _erasure_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="stripe-erasure",
        )
    return _erasure_executor


def shutdown_erasure_executor() -> None:
    """Tear down the erasure executor on app shutdown.

    Registered from ``api.main.lifespan``. Safe to call when the executor
    was never created (no-op) and safe to call twice (second call is a
    no-op).

    Uses ``wait=False`` so this returns immediately rather than blocking
    the lifespan on in-flight Stripe HTTP calls. ``cancel_futures=True``
    drops queued (not-yet-started) tasks. Any in-flight Stripe call
    keeps running in its worker thread until either the call completes
    or the process is killed by the orchestrator's SIGKILL after grace
    period — erasure is best-effort, so an orphaned ack is acceptable
    (Stripe processes the cancel/delete server-side regardless).
    """
    global _erasure_executor
    if _erasure_executor is not None:
        _erasure_executor.shutdown(wait=False, cancel_futures=True)
        _erasure_executor = None


async def _run_stripe(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous stripe-python call on the default thread pool.

    Suitable for low-rate user-initiated calls (checkout, portal). Use
    ``_run_stripe_erasure`` for the erasure sweep so a burst of
    erasures cannot starve checkout/portal.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


async def _run_stripe_erasure(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous stripe-python call on the dedicated erasure pool.

    ``functools.partial`` is required so ``**kwargs`` survive the
    ``run_in_executor`` boundary (which only forwards positionals).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _get_erasure_executor(), functools.partial(func, *args, **kwargs)
    )


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

    session = await _run_stripe(stripe.checkout.Session.create, **checkout_params)
    if session.url is None:
        # ``Session.url`` is typed ``Optional[str]`` in stripe-python because
        # non-redirect modes (``ui_mode="embedded"``, certain ``mode`` values)
        # do not populate it. We always pass ``mode="subscription"`` with
        # ``success_url``/``cancel_url`` here, so a ``None`` means an
        # unexpected upstream change — surface as a typed 502.
        raise StripeError("checkout Session.create returned no URL")

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

    session = await _run_stripe(
        stripe.billing_portal.Session.create,
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
    # #805: memory_limit is no longer a Workspace column. Source the audit
    # "old" value from the old plan tier (captured before plan_name mutates) —
    # this is more correct than the dropped column, which could be stale.
    old_memory_limit = get_plan_tier(old_plan).memory_limit
    new_tier = get_plan_tier(new_plan_name)

    # Update workspace
    workspace.plan_name = new_plan_name
    # #1095: the legacy in-app Stripe path is NOT managed by the external billing
    # reconciler (kagura-billing#5) — it has its own subscription lifecycle
    # (_handle_subscription_cancelled). Mark it locally-owned so a reconcile pass
    # never reverts an in-app Stripe plan it has no record of.
    workspace.entitlement_source = ENTITLEMENT_SOURCE_ADMIN_GRANT
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
    # #1095: in-app Stripe cancel is locally-owned (not external-reconciler-managed).
    workspace.entitlement_source = ENTITLEMENT_SOURCE_ADMIN_GRANT
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
            await _run_stripe_erasure(stripe.Subscription.cancel, workspace.stripe_subscription_id)
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
            await _run_stripe_erasure(stripe.Customer.delete, workspace.stripe_customer_id)
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
