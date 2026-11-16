"""Explicit human control of a retained workspace-owned synchronization target."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from api.routes.sync_feeds import Database, bounded_body, exact_query, json_body
from api.routes.sync_installations import SYNC_RESPONSES, Manager, Reader, SyncControlRoute
from models.sync_feed_schemas import Hash, StrictBody
from models.sync_mode_access_schemas import SyncModeAccess
from services.sync_management import control_mode, management_enabled, read_mode_access
from services.sync_resume import confirm_resume, reconnect, resume_diff, resume_preview

router = APIRouter(
    prefix="/workspaces/{workspace_id}/sync-installations/{installation_id}/management",
    tags=["workspace-sync"],
    route_class=SyncControlRoute,
    dependencies=[Depends(management_enabled)],
    responses=SYNC_RESPONSES,
)


class ModeCommand(StrictBody):
    """One immutable logical control operation."""

    operation_id: UUID


class ModeExpectation(StrictBody):
    """Exact public management snapshot confirmed by the requesting human."""

    mode: Literal["feed", "stopping", "manual"]
    epoch: Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
    generation: Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
    stop_event_id: Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]


class GuardedModeCommand(ModeCommand):
    """An optional atomic precondition; omission/null retain legacy semantics."""

    expected: ModeExpectation | None = None


class ResumeCommand(ModeCommand):
    """Consent names the exact expiring preview, never arbitrary records or targets."""

    preview_id: UUID
    diff_hash: Hash


class ReconnectCommand(ModeCommand):
    """Fresh current-owner consent for the same retained installation."""

    authorization_id: UUID


@router.post("/reconnect", openapi_extra=json_body(ReconnectCommand))
async def reconnect_sync_management(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> dict:
    """Reauthorize a retained target; leave content in manual mode until explicit resume."""
    exact_query(request)
    body, _ = await bounded_body(request, ReconnectCommand)
    return await reconnect(
        db, workspace_id, installation_id, principal, body.operation_id, body.authorization_id
    )


@router.get("/access", response_model=SyncModeAccess)
async def management_access(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Reader, db: Database
) -> SyncModeAccess:
    """Inspect capacity-safe lifecycle capabilities with ordinary human login."""
    exact_query(request)
    return await read_mode_access(db, workspace_id, installation_id, principal)


@router.get("")
async def management_status(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Reader, db: Database
) -> dict:
    """Inspect mode after normal workspace/context authorization, even after expiry."""
    exact_query(request)
    return await control_mode(db, workspace_id, installation_id, principal)


@router.post("/stop", openapi_extra=json_body(GuardedModeCommand))
async def stop_sync_management(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> dict:
    """Fence new work atomically; retain the editing lock until drain completes."""
    exact_query(request)
    body, _ = await bounded_body(request, GuardedModeCommand)
    return await control_mode(
        db,
        workspace_id,
        installation_id,
        principal,
        body.operation_id,
        expected=body.expected.model_dump(mode="json") if body.expected is not None else None,
    )


@router.post("/finalize", openapi_extra=json_body(GuardedModeCommand))
async def finalize_sync_management(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> dict:
    """Unlock only with exclusive writer exclusion and successful canonical indexing."""
    exact_query(request)
    body, _ = await bounded_body(request, GuardedModeCommand)
    return await control_mode(
        db,
        workspace_id,
        installation_id,
        principal,
        body.operation_id,
        finalize=True,
        expected=body.expected.model_dump(mode="json") if body.expected is not None else None,
    )


@router.post("/resume/preview")
async def preview_sync_resume(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> dict:
    """Obtain finite consent for strict source candidates versus current manual data."""
    exact_query(request)
    return await resume_preview(db, workspace_id, installation_id, principal)


@router.get("/resume/diff")
async def read_sync_resume_diff(
    workspace_id: UUID,
    installation_id: UUID,
    request: Request,
    principal: Manager,
    db: Database,
    preview_id: UUID,
    offset: Annotated[int, Query(ge=0, le=20000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict:
    """Page only the exact human's still-current resume snapshot."""
    exact_query(request, {"preview_id", "offset", "limit"})
    return await resume_diff(
        db, workspace_id, installation_id, principal, preview_id, offset, limit
    )


@router.post("/resume/confirm", openapi_extra=json_body(ResumeCommand))
async def confirm_sync_resume(
    workspace_id: UUID, installation_id: UUID, request: Request, principal: Manager, db: Database
) -> dict:
    """Confirm current normal authority and paid rights at the atomic publication commit."""
    exact_query(request)
    body, _ = await bounded_body(request, ResumeCommand)
    return await confirm_resume(
        db,
        workspace_id,
        installation_id,
        principal,
        body.operation_id,
        body.preview_id,
        body.diff_hash,
    )
