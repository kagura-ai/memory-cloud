"""Trusted paid activation; never accepts a human token or arbitrary worker target."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.internal_integrations import billing_integration_caller
from api.routes.sync_installations import SYNC_RESPONSES, SyncControlRoute, sync_control_enabled
from db.base import get_db
from models.sync_installation_schemas import (
    SyncActivation,
    SyncAuthorizationView,
    SyncInstallationView,
)
from services.sync_installations import activate_installation, validate_authorization

router = APIRouter(
    prefix="/internal",
    tags=["internal-billing"],
    route_class=SyncControlRoute,
    dependencies=[Depends(billing_integration_caller), Depends(sync_control_enabled)],
    responses=SYNC_RESPONSES,
)


@router.post(
    "/workspaces/{workspace_id}/sync-authorizations/validate", response_model=SyncAuthorizationView
)
async def validate_pending_sync(
    workspace_id: UUID,
    body: SyncActivation,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncAuthorizationView:
    """Check pending owner consent before charging; activation rechecks independently."""
    return await validate_authorization(db, workspace_id, body)


@router.post(
    "/workspaces/{workspace_id}/sync-installations/activate", response_model=SyncInstallationView
)
async def activate_paid_sync(
    workspace_id: UUID,
    body: SyncActivation,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncInstallationView:
    """Recheck finite approval and paid rights internally, without reusing a human login."""
    return await activate_installation(db, workspace_id, body)
