"""Human-only feed controls and canonical data projection with explicit consent."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from api.routes.sync_feeds import Database, bounded_body, exact_query, json_body
from api.routes.sync_installations import SYNC_RESPONSES, Manager, SyncControlRoute
from auth.sync_identity import SyncPrincipal, sync_records_reader
from config.settings import get_settings
from models.sync_feed_schemas import (
    MAX_RECORDS,
    ConfirmDiff,
    FeedConfig,
    SchemaUpgrade,
    SchemaUpgradeResult,
)
from services.sync_contract import SCHEMA_V2_CAPABILITY, sync_configuration
from services.sync_feed_authority import runs_enabled
from services.sync_feed_human import approve_diff, configure_feed, human_diff, project_records
from services.sync_schema_upgrade import upgrade_schema
from utils.exceptions import NotFoundException

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


async def schema_upgrade_enabled() -> None:
    """#3055: the upgrade answers 404 before authentication while version 2 is not advertised."""
    if SCHEMA_V2_CAPABILITY not in sync_configuration(get_settings()).capabilities:
        raise NotFoundException("Synchronization schema upgrade")


@router.post(
    "/schema/upgrade",
    response_model=SchemaUpgradeResult,
    dependencies=[Depends(schema_upgrade_enabled)],
    openapi_extra=json_body(SchemaUpgrade),
)
async def upgrade_sync_schema(
    workspace_id: UUID,
    installation_id: UUID,
    request: Request,
    principal: Manager,
    db: Database,
) -> dict:
    """Upgrade the installation's record schema to version 2 (one-way).

    A current Context Editor with ``integrations:manage``. The body names the
    target version (2) and the stored pair and generation last read. Already
    on version 2: ``changed`` is false. An active run of the installation is
    ``409 sync_schema_upgrade_runs_active``; a changed snapshot, no stored
    schema or an unregistered one is ``409 RES-002``. The installation's resume
    candidates and preview are cleared; the generation, feeds and records are
    not changed.
    """
    exact_query(request)
    body, _size = await bounded_body(request, SchemaUpgrade)
    return await upgrade_schema(db, workspace_id, installation_id, principal, body)


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
    """Read only approved feed record fields under current KMC data permissions."""
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
