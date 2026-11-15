"""Human-only feed controls and canonical data projection with explicit consent."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from api.routes.sync_feeds import Database, bounded_body, exact_query, json_body
from api.routes.sync_installations import SYNC_RESPONSES, Manager, SyncControlRoute
from auth.sync_identity import SyncPrincipal, sync_records_reader
from models.sync_feed_schemas import MAX_RECORDS, ConfirmDiff, FeedConfig
from services.sync_feed_authority import runs_enabled
from services.sync_feed_human import approve_diff, configure_feed, human_diff, project_records

router = APIRouter(
    prefix="/workspaces/{workspace_id}/sync-installations/{installation_id}",
    tags=["workspace-sync"],
    route_class=SyncControlRoute,
    dependencies=[Depends(runs_enabled)],
    responses=SYNC_RESPONSES,
)
Reader = Annotated[SyncPrincipal, Depends(sync_records_reader)]


@router.put("/feeds/{feed_id}", openapi_extra=json_body(FeedConfig))
async def configure_sync_feed(
    workspace_id: UUID,
    installation_id: UUID,
    feed_id: UUID,
    request: Request,
    principal: Manager,
    db: Database,
) -> dict:
    """Approve the fixed public schema and version a feed's opaque configuration."""
    exact_query(request)
    body, _size = await bounded_body(request, FeedConfig)
    return await configure_feed(db, workspace_id, installation_id, feed_id, principal, body)


@router.get("/records")
async def read_sync_records(
    workspace_id: UUID,
    installation_id: UUID,
    request: Request,
    principal: Reader,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    feed_id: UUID | None = None,
) -> dict:
    """Read only approved product/FAQ fields under current KMC data permissions."""
    exact_query(request, {"limit", "cursor", "feed_id"})
    return await project_records(
        db, workspace_id, installation_id, principal, limit=limit, cursor=cursor, feed_id=feed_id
    )


@router.get("/runs/{run_id}/diff")
async def read_sync_run_diff(
    workspace_id: UUID,
    installation_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Reader,
    db: Database,
    offset: Annotated[int, Query(ge=0, le=2 * MAX_RECORDS)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict:
    """Inspect a bounded prepared deletion preview, without worker credentials."""
    exact_query(request, {"offset", "limit"})
    return await human_diff(
        db, workspace_id, installation_id, run_id, principal, offset=offset, limit=limit
    )


@router.post("/runs/{run_id}/approvals", openapi_extra=json_body(ConfirmDiff))
async def approve_sync_run_diff(
    workspace_id: UUID,
    installation_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Manager,
    db: Database,
) -> dict:
    """Approve the exact finite destructive diff using current human editor rights."""
    exact_query(request)
    body, _size = await bounded_body(request, ConfirmDiff)
    return await approve_diff(db, workspace_id, installation_id, run_id, principal, body)
