"""Explicit OIDC member observations and authenticated manual request attribution."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from api.routes.sync_feeds import bounded_body, exact_query, json_body
from api.routes.sync_installations import (
    SYNC_RESPONSES,
    Database,
    Manager,
    Reader,
    SyncControlRoute,
)
from models.sync_activity_schemas import (
    ActivityOverview,
    EventPage,
    ManualRequestBody,
    ManualRequestView,
    RunDetail,
    RunPage,
)
from services import sync_activity as activity
from services.sync_requests import create_request

router = APIRouter(
    prefix="/workspaces/{workspace_id}/sync-installations/{installation_id}/activity",
    tags=["workspace-sync"],
    route_class=SyncControlRoute,
    dependencies=[Depends(activity.activity_enabled)],
    responses=SYNC_RESPONSES,
)


@router.get("", response_model=ActivityOverview)
async def get_activity(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Reader, db: Database
) -> dict:
    """Read current mode and the authoritative workspace allowance without consuming it."""
    exact_query(request)
    return await activity.overview(db, workspace_id, installation_id, principal)


@router.get("/runs", response_model=RunPage)
async def get_runs(
    workspace_id: UUID,
    installation_id: UUID,
    request: Request,
    principal: Reader,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: UUID | None = None,
) -> dict:
    """Page receipt metadata; index evidence is evaluated on the individual run endpoint."""
    exact_query(request, {"limit", "cursor"})
    return await activity.runs_page(
        db, workspace_id, installation_id, principal, limit=limit, cursor=cursor
    )


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(
    workspace_id: UUID,
    installation_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Reader,
    db: Database,
) -> dict:
    """Distinguish source admission/commit from actual current search reflection."""
    exact_query(request)
    return await activity.run_detail(db, workspace_id, installation_id, run_id, principal)


@router.get("/events", response_model=EventPage)
async def get_events(
    workspace_id: UUID,
    installation_id: UUID,
    request: Request,
    principal: Reader,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict:
    """Read only protocol-v1 audit references; actor output is pseudonymized."""
    exact_query(request, {"limit", "before_id"})
    return await activity.activity_events(
        db, workspace_id, installation_id, principal, limit=limit, before_id=before_id
    )


@router.post(
    "/requests",
    status_code=201,
    response_model=ManualRequestView,
    openapi_extra=json_body(ManualRequestBody),
)
async def request_sync(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> dict:
    """Record a current Editor's intent without granting any service execution rights."""
    exact_query(request)
    body, _ = await bounded_body(request, ManualRequestBody)
    return await create_request(db, workspace_id, installation_id, principal, body.operation_id)
