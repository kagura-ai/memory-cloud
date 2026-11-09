"""Owner-only provisioning, inspection and revocation of immutable grants."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.management_route import BoundedManagementRoute
from auth.dependencies import WorkspaceOwner
from auth.management_grants import current_binding
from config.settings import get_settings
from db.base import get_db
from models.auth import Workspace
from models.management_grant import ManagementGrant
from models.management_schemas import GrantCreate, digest
from models.resource import Resource, ResourceSchema
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import ConflictError, NotFoundException

router = APIRouter(
    prefix="/resource-management-grants",
    tags=["resource-management-grants"],
    route_class=BoundedManagementRoute,
)


def record_grant(db: AsyncSession, grant: Any, actor_user_id: str, operation: str) -> None:
    """Queue operation history for a grant's issue or revocation (#2739).

    The grant is a delegated credential: its id is the target and its
    delegated context sets the privacy boundary. One event per grant and
    operation (event key), so a replayed revocation adds nothing.
    """
    from services.operation_history_writer import record_operation

    record_operation(
        db,
        operation=operation,
        actor_user_id=actor_user_id,
        workspace_id=grant.workspace_id,
        context_id=grant.context_id,
        target_id=grant.id,
        changed_fields=["credential"],
        event_key=f"management-grant:{grant.id}:{operation}",
    )


def metadata(grant: ManagementGrant) -> dict:
    """Return only the immutable authority and its lifecycle metadata."""
    return {
        "id": str(grant.id),
        "deployment_id": grant.deployment_id,
        "workspace_id": str(grant.workspace_id),
        "resource_pk": str(grant.resource_pk),
        "context_id": str(grant.context_id),
        "endpoint_id": str(grant.endpoint_id),
        "operations": grant.operations,
        "approved_fields": grant.approved_fields,
        "schema_version": grant.schema_version,
        "schema_hash": grant.schema_hash,
        "quota_events_per_hour": grant.quota_events_per_hour,
        "max_active_tokens": grant.max_active_tokens,
        "expires_at": to_utc_iso(grant.expires_at),
        "revoked_at": to_utc_iso(grant.revoked_at),
    }


@router.post("", status_code=201)
async def create_grant(
    body: GrantCreate, owner: WorkspaceOwner, response: Response, db: AsyncSession = Depends(get_db)
) -> dict:
    """Explicitly delegate one approved target; token authority is separately acknowledged."""
    user_id, workspace_id = owner
    await db.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    resource = (
        await db.execute(
            select(Resource)
            .where(Resource.id == body.resource_pk, Resource.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if resource is None:
        raise NotFoundException("Resource")
    schema = (
        await db.execute(
            select(ResourceSchema)
            .where(ResourceSchema.resource_pk == resource.id)
            .order_by(ResourceSchema.schema_version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if schema is None:
        raise ConflictError("Approved schema required")
    secret = "kagura_mgmt_" + secrets.token_urlsafe(32)
    grant = ManagementGrant(
        id=uuid4(),  # known before the INSERT, for its history event
        workspace_id=workspace_id,
        resource_pk=resource.id,
        context_id=body.context_id,
        endpoint_id=body.endpoint_id,
        deployment_id=get_settings().deployment_id,
        key_hash=hashlib.sha256(secret.encode()).hexdigest(),
        created_by=user_id,
        operations=body.operations,
        approved_fields=body.approved_fields,
        schema_version=schema.schema_version,
        schema_hash=digest(schema.field_definitions),
        quota_events_per_hour=body.quota_events_per_hour,
        max_active_tokens=body.max_active_tokens,
        expires_at=utcnow() + timedelta(days=body.ttl_days),
    )
    await current_binding(db, grant)
    db.add(grant)
    record_grant(db, grant, user_id, "credential.issued")
    await db.commit()
    await db.refresh(grant)
    response.headers["Cache-Control"] = "no-store"
    return {**metadata(grant), "secret": secret}


@router.get("")
async def list_grants(
    owner: WorkspaceOwner, response: Response, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """List workspace grants without secret material."""
    response.headers["Cache-Control"] = "no-store"
    rows = (
        (
            await db.execute(
                select(ManagementGrant)
                .where(ManagementGrant.workspace_id == owner[1])
                .order_by(ManagementGrant.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [metadata(row) for row in rows]


@router.delete("/{grant_id}", status_code=204)
async def revoke_grant(
    grant_id: UUID, owner: WorkspaceOwner, db: AsyncSession = Depends(get_db)
) -> None:
    """Serialize revocation with in-flight operations; external tokens remain independent."""
    await db.execute(select(Workspace.id).where(Workspace.id == owner[1]).with_for_update())
    grant = (
        await db.execute(
            select(ManagementGrant)
            .where(ManagementGrant.id == grant_id, ManagementGrant.workspace_id == owner[1])
            .with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None:
        raise NotFoundException("Management grant")
    if grant.revoked_at is None:
        grant.revoked_at = utcnow()
        record_grant(db, grant, owner[0], "credential.revoked")
    await db.commit()
