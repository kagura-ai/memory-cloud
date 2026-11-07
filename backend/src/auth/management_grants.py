"""A separate principal, never promoted to WorkspaceOwner or an observation key."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.resource_tokens import count_regular_active_tokens_for_mint
from config.plan_tiers import has_feature
from config.settings import get_settings
from db.base import get_db
from mcp_server.public.publication import public_endpoints_enabled
from models.auth import Context, Workspace, WorkspaceMember
from models.management_grant import ManagementGrant
from models.management_schemas import digest, public_fields
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource, ResourceSchema, WorkspaceConnector
from utils.datetime import utcnow
from utils.exceptions import AuthenticationError, AuthorizationError, BadRequestError, ConflictError


@dataclass
class ManagementPrincipal:
    grant: ManagementGrant
    resource: Resource
    workspace: Workspace
    fields: list[dict]
    actor: str

    def require(self, operation: str) -> None:
        """Refuse an unapproved operation before work is performed."""
        if operation not in self.grant.operations:
            raise AuthorizationError("Operation not delegated")


async def current_binding(
    db: AsyncSession, grant: ManagementGrant
) -> tuple[Resource, Workspace, list[dict]]:
    """Lock current authority and identities through the operation's commit."""
    workspace = (
        await db.execute(
            select(Workspace).where(Workspace.id == grant.workspace_id).with_for_update()
        )
    ).scalar_one_or_none()
    if (
        workspace is None
        or workspace.deleted_at is not None
        or not has_feature(workspace.plan_name or "", "resources")
    ):
        raise AuthorizationError("Resource management unavailable")
    member = (
        await db.execute(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == grant.workspace_id,
                WorkspaceMember.user_id == grant.created_by,
            )
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if member is None or member.role != "owner":
        raise AuthorizationError("Delegating owner permission changed")
    resource = (
        await db.execute(
            select(Resource)
            .where(Resource.id == grant.resource_pk, Resource.workspace_id == grant.workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    context = (
        await db.execute(
            select(Context)
            .where(
                Context.id == grant.context_id,
                Context.workspace_id == grant.workspace_id,
                Context.deleted_at.is_(None),
            )
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    endpoint = (
        await db.execute(
            select(PublicMcpEndpoint)
            .where(
                PublicMcpEndpoint.id == grant.endpoint_id, PublicMcpEndpoint.revoked_at.is_(None)
            )
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if (
        not public_endpoints_enabled()
        or resource is None
        or context is None
        or endpoint is None
        or context.is_private
        or not context.is_public
        or context.resource_id != resource.resource_id
        or endpoint.resource_pk != resource.id
        or endpoint.context_id != context.id
        or endpoint.workspace_id != workspace.id
        or endpoint.resource_id != resource.resource_id
    ):
        raise AuthorizationError("Resource binding changed")
    if (
        await db.execute(
            select(WorkspaceConnector.id).where(WorkspaceConnector.resource_pk == resource.id)
        )
    ).first():
        raise AuthorizationError("Connector resources cannot be delegated")
    schema = (
        await db.execute(
            select(ResourceSchema)
            .where(ResourceSchema.resource_pk == resource.id)
            .order_by(ResourceSchema.schema_version.desc())
            .limit(1)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if (
        schema is None
        or schema.schema_version != grant.schema_version
        or digest(schema.field_definitions) != grant.schema_hash
    ):
        raise ConflictError("Schema approval changed; provision a new grant")
    try:
        fields = public_fields(schema.field_definitions, grant.approved_fields)
    except ValueError as exc:
        raise ConflictError("Public field approval changed") from exc
    return resource, workspace, fields


async def management_principal(
    request: Request, db: AsyncSession = Depends(get_db)
) -> ManagementPrincipal:
    """Authenticate only this router, serializing revocation with every operation."""
    authorization = request.headers.get("authorization", "")
    if not re.fullmatch(r"Bearer kagura_mgmt_[A-Za-z0-9_-]{43}", authorization):
        raise AuthenticationError("Invalid management credential")
    key_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
    identity = (
        await db.execute(
            select(ManagementGrant.id, ManagementGrant.workspace_id).where(
                ManagementGrant.key_hash == key_hash
            )
        )
    ).first()
    if identity is None:
        raise AuthenticationError("Invalid management credential")
    # The existing owner mint path takes this advisory lock before inserting
    # a Resource FK. Take it before our resource row lock to avoid inversion.
    await count_regular_active_tokens_for_mint(db, identity.workspace_id)
    # One lock order for provisioning, request and revocation. Read back the grant
    # after the workspace lock; a revocation that committed while we waited wins.
    await db.execute(
        select(Workspace.id).where(Workspace.id == identity.workspace_id).with_for_update()
    )
    grant = (
        await db.execute(
            select(ManagementGrant)
            .where(ManagementGrant.id == identity.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        grant is None
        or grant.revoked_at is not None
        or grant.expires_at <= utcnow()
        or grant.deployment_id != get_settings().deployment_id
    ):
        raise AuthenticationError("Invalid management credential")
    resource, workspace, fields = await current_binding(db, grant)
    actor = request.headers.get("x-management-actor", "")
    if request.method != "GET" and not re.fullmatch(r"[a-f0-9]{64}", actor):
        raise BadRequestError("Service-attributed actor required")
    return ManagementPrincipal(grant, resource, workspace, fields, actor)


ManagedResource = Annotated[ManagementPrincipal, Depends(management_principal)]
