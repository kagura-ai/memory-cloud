"""Internal billing publisher API for finite paid integration rights (#2798)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.internal_billing import check_billing_service_token
from auth.single_authorization import sole_authorization
from db.base import get_db
from models.integration_entitlement_schemas import (
    EntitlementWindow,
    IntegrationEntitlementPush,
    IntegrationEntitlementResult,
    IntegrationEntitlementView,
)
from services.integration_entitlements import (
    apply_integration_entitlements,
    read_integration_entitlements,
)

# Keep the window type available with the endpoint's contract.
__all__ = ["EntitlementWindow", "router"]
router = APIRouter(prefix="/internal", tags=["internal-billing"])
_BILLING = HTTPBearer(
    auto_error=False,
    scheme_name="BillingPublisher",
    description="Internal billing service credential; no browser, OIDC or integration-token fallback.",
)


async def billing_integration_caller(
    request: Request,
    _credential: Annotated[HTTPAuthorizationCredentials | None, Security(_BILLING)],
) -> None:
    """Reuse billing authority; refuse ambiguous headers and every session fallback.

    ``_credential`` only documents the ``BillingPublisher`` scheme in OpenAPI;
    the check reads the request's single ``Authorization`` header, the same
    shared check as the other billing routes (#2801).
    """
    await check_billing_service_token(sole_authorization(request))


@router.get(
    "/workspaces/{workspace_id}/integration-entitlements", response_model=IntegrationEntitlementView
)
async def get_integration_entitlements(
    workspace_id: UUID,
    response: Response,
    _: None = Depends(billing_integration_caller),
    db: AsyncSession = Depends(get_db),
) -> IntegrationEntitlementView:
    """Read current state for trusted billing reconciliation, without granting authority."""
    response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache"})
    return await read_integration_entitlements(db, workspace_id)


@router.put(
    "/workspaces/{workspace_id}/integration-entitlements",
    response_model=IntegrationEntitlementResult,
)
async def put_integration_entitlements(
    workspace_id: UUID,
    body: IntegrationEntitlementPush,
    response: Response,
    _: None = Depends(billing_integration_caller),
    db: AsyncSession = Depends(get_db),
) -> IntegrationEntitlementResult:
    """Atomically accept a complete, ordered snapshot from the billing authority only."""
    response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache"})
    result = await apply_integration_entitlements(db, workspace_id, body)
    await db.commit()
    return result
