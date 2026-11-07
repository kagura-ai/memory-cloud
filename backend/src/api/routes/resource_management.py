"""Fixed-target operations; bearer grants cannot use the owner or public MCP routes."""

from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.management_route import BoundedManagementRoute
from api.routes.management_grants import metadata
from api.routes.resource_ingest import _schedule_indexer_for_resource
from api.routes.resource_tokens import _format_token_response
from auth.management_grants import ManagedResource, ManagementPrincipal
from auth.resource_tokens import ResourceTokenManager, count_regular_active_tokens_for_mint
from db.base import get_db
from models.management_grant import ManagementOperation
from models.management_schemas import (
    ManagedBatch,
    ManagedRevoke,
    ManagedToken,
    digest,
    validate_public_payload,
)
from models.resource import ResourceToken
from services.capacity_lock import ensure_not_capacity_locked
from services.plan_suspension import ensure_ingest_allowed
from services.resource_ingest_service import finalize_batch, persist_events, validate_events
from services.resource_quota_service import check_event_quota
from services.resource_token_policy import (
    authorize_resource_token_mint,
    check_workspace_quota_ceiling,
)
from utils.exceptions import RedisError

router = APIRouter(
    prefix="/resource-management", tags=["resource-management"], route_class=BoundedManagementRoute
)


async def previous(
    db: AsyncSession, auth: ManagementPrincipal, action: str, body: dict
) -> dict | None:
    """A retry never executes twice, nor recovers a one-time external token."""
    row = await db.get(ManagementOperation, (auth.grant.id, UUID(body["operation_id"])))
    if row is None:
        return None
    if row.action != action or row.request_hash != digest(body) or row.actor != auth.actor:
        raise HTTPException(409, "Operation identifier already used")
    return {**row.outcome, "replayed": True}


def record(
    db: AsyncSession, auth: ManagementPrincipal, action: str, body: dict, outcome: dict
) -> None:
    """Store bounded sanitized audit/outcome metadata in the operation transaction."""
    db.add(
        ManagementOperation(
            grant_id=auth.grant.id,
            operation_id=UUID(body["operation_id"]),
            action=action,
            actor=auth.actor,
            request_hash=digest(body),
            outcome=outcome,
        )
    )


@router.get("/capabilities")
async def capabilities(auth: ManagedResource, response: Response) -> dict:
    """Return the actual delegated authority, without exposing any secret."""
    response.headers["Cache-Control"] = "no-store"
    return {
        "contract": "resource-management/v1",
        **metadata(auth.grant),
        "resource_id": auth.resource.resource_id,
        "limits": {"max_batch": 100, "max_payload_bytes": 100000, "max_request_bytes": 1048576},
        "external_tokens": {
            "expires": False,
            "operation_scopes": False,
            "includes_delete": True,
            "independent_of_grant": True,
        },
    }


@router.get("/schema")
async def schema(auth: ManagedResource, response: Response) -> dict:
    """Read only the owner's explicitly approved field projection."""
    auth.require("schema:read")
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema_hash": auth.grant.schema_hash,
        "schema_version": auth.grant.schema_version,
        "fields": auth.fields,
    }


@router.get("/tokens")
async def tokens(
    auth: ManagedResource,
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List metadata only, always pinned to the immutable resource PK."""
    auth.require("tokens:read")
    response.headers["Cache-Control"] = "no-store"
    manager = ResourceTokenManager(db)
    rows = await manager.list_tokens(
        workspace_id=auth.grant.workspace_id,
        resource_pk=auth.resource.id,
        include_revoked=True,
        limit=limit,
        offset=offset,
    )
    total = await manager.count_tokens(
        workspace_id=auth.grant.workspace_id, resource_pk=auth.resource.id, include_revoked=True
    )
    return {
        "tokens": [_format_token_response(row).model_dump(mode="json") for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/tokens", status_code=201)
async def create_token(
    body: ManagedToken,
    auth: ManagedResource,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Issue an external token once, within grant and shared workspace caps."""
    auth.require("tokens:create")
    response.headers["Cache-Control"] = "no-store"
    data = body.model_dump(mode="json")
    if result := await previous(db, auth, "tokens:create", data):
        return result
    if body.quota_events_per_hour > auth.grant.quota_events_per_hour:
        raise HTTPException(403, "Delegated quota exceeded")
    pk = await authorize_resource_token_mint(db, auth.grant.workspace_id, auth.resource.resource_id)
    if pk != auth.resource.id:
        raise HTTPException(403, "Resource binding changed")
    manager = ResourceTokenManager(db)
    count = await manager.count_tokens(
        workspace_id=auth.grant.workspace_id, resource_pk=pk, include_revoked=False
    )
    if count >= auth.grant.max_active_tokens:
        raise HTTPException(403, "Delegated token cap reached")
    secret, token = await manager.create_token(
        resource_id=auth.resource.resource_id,
        resource_pk=pk,
        workspace_id=auth.grant.workspace_id,
        created_by=auth.grant.created_by,
        description=body.description,
        quota_events_per_hour=body.quota_events_per_hour,
    )
    await db.flush()
    await db.refresh(token)
    result = {
        "token_metadata": _format_token_response(token).model_dump(mode="json"),
        "replayed": False,
    }
    record(db, auth, "tokens:create", data, result)
    await db.commit()
    return {**result, "token": secret}


async def target_token(db: AsyncSession, auth: ManagementPrincipal, token_id: str) -> ResourceToken:
    token = (
        await db.execute(
            select(ResourceToken)
            .where(
                ResourceToken.public_id == token_id, ResourceToken.resource_pk == auth.resource.id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if token is None:
        raise HTTPException(404, "Token not found")
    return token


@router.patch("/tokens/{token_id}")
async def update_token(
    token_id: str,
    body: ManagedToken,
    auth: ManagedResource,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update only a bound token; limits cannot be raised beyond the delegation."""
    auth.require("tokens:update")
    response.headers["Cache-Control"] = "no-store"
    data = {**body.model_dump(mode="json"), "token_id": token_id}
    if result := await previous(db, auth, "tokens:update", data):
        return result
    if body.quota_events_per_hour > auth.grant.quota_events_per_hour:
        raise HTTPException(403, "Delegated quota exceeded")
    await count_regular_active_tokens_for_mint(db, auth.grant.workspace_id)
    token = await target_token(db, auth, token_id)
    if body.quota_events_per_hour > token.quota_events_per_hour:
        await check_workspace_quota_ceiling(
            db, token, auth.grant.workspace_id, body.quota_events_per_hour
        )
    token.description, token.quota_events_per_hour = body.description, body.quota_events_per_hour
    result = {"token_metadata": _format_token_response(token).model_dump(mode="json")}
    record(db, auth, "tokens:update", data, result)
    await db.commit()
    return result


@router.delete("/tokens/{token_id}")
async def revoke_token(
    token_id: str,
    body: ManagedRevoke,
    auth: ManagedResource,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Explicit revocation is separate from replacement creation."""
    auth.require("tokens:revoke")
    response.headers["Cache-Control"] = "no-store"
    data = {**body.model_dump(mode="json"), "token_id": token_id}
    if result := await previous(db, auth, "tokens:revoke", data):
        return result
    token = await target_token(db, auth, token_id)
    await ResourceTokenManager(db).revoke_token(token.id)
    result = {"revoked": True, "token_id": token_id}
    record(db, auth, "tokens:revoke", data, result)
    await db.commit()
    return result


@router.post("/events")
async def submit_events(
    body: ManagedBatch,
    auth: ManagedResource,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Validate again at commit time, persist through the shared ingestion service."""
    response.headers["Cache-Control"] = "no-store"
    for event in body.events:
        auth.require("events:" + event.op)
    if body.schema_hash != auth.grant.schema_hash:
        raise HTTPException(409, "Schema revision changed")
    try:
        for event in body.events:
            if event.payload is not None:
                validate_public_payload(event.payload, auth.fields)
    except ValueError as exc:
        raise HTTPException(422, "Payload does not match approved public fields") from exc
    data = body.model_dump(mode="json")
    if result := await previous(db, auth, "events", data):
        return result
    await ensure_ingest_allowed(db, auth.workspace, auth.resource.id)
    await ensure_not_capacity_locked(db, auth.workspace)
    events = []
    for event in body.events:
        item = event.model_dump(mode="json")
        item["idempotency_key"] = hashlib.sha256(
            (
                str(auth.grant.id) + ":" + str(auth.resource.id) + ":" + str(event.idempotency_key)
            ).encode()
        ).hexdigest()
        events.append(item)
    valid, errors = validate_events(events)
    if errors:
        raise HTTPException(422, "Invalid batch")
    try:
        await check_event_quota(
            auth.resource.resource_id,
            auth.grant.workspace_id,
            auth.grant.quota_events_per_hour,
            count=len(events),
            strict=True,
        )
    except RedisError as exc:
        raise HTTPException(503, "Quota reservation unavailable") from exc
    saved = await persist_events(
        db, resource_id=auth.resource.resource_id, resource_pk=auth.resource.id, events=valid
    )
    outcome = {
        "state": "accepted" if not saved.errors else "partial" if saved.created_ids else "failed",
        "accepted": len(saved.created_ids),
        "event_ids": saved.created_ids,
        "errors": [{"index": e.index, "code": e.kind} for e in saved.errors],
        "searchable": "unknown",
    }
    record(db, auth, "events", data, outcome)
    await finalize_batch(
        db,
        workspace_id=auth.grant.workspace_id,
        resource_id=auth.resource.resource_id,
        created_ids=saved.created_ids,
        schedule_indexer=_schedule_indexer_for_resource,
    )
    return outcome
