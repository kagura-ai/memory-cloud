"""Billing Plugin for Kagura Memory Cloud.

Provides self-service plan management with Stripe integration.
When disabled (default), plan changes are admin-only via /admin/plans endpoints.

Enable by setting:
    BILLING_ENABLED=true
    STRIPE_SECRET_KEY=sk_...
    STRIPE_WEBHOOK_SECRET=whsec_...
"""

from config.settings import get_settings


def is_billing_enabled() -> bool:
    """Check if billing plugin is enabled."""
    return get_settings().billing_enabled
