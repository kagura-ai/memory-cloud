"""What an observation credential may read, resolved from its row only (#2679).

Every observability endpoint goes through ``scoped_resources`` (the resources
of the bound workspace, or the one bound resource) and, for a per-resource
read, ``resolve_scoped_resource``. A slug outside the scope and a slug that
does not exist answer the same 404 (``resource_not_found``), so a credential
cannot probe another workspace's or a sibling resource's slugs.

``publication`` describes the resource's live public MCP endpoint row
(#2675), with ``serving`` from ``load_serving_endpoint`` — the same gate the
anonymous transport runs on every request (switch, unpublished, context no
longer public, slug moved, workspace deleted).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.observation_credentials import ObservationScope
from mcp_server.public.publication import load_serving_endpoint, serving_paused
from models.auth import Context
from models.public_mcp import PublicMcpEndpoint
from models.resource import Resource
from utils.exceptions import ObservabilityResourceNotFoundError, ValidationError

#: The most resources one list response carries (no pagination; documented).
OBSERVABILITY_RESOURCES_MAX = 500


@dataclass(frozen=True)
class ScopedResource:
    """One resource the credential may read, with its live context and endpoint."""

    resource_pk: UUID
    resource_id: str
    name: str | None
    created_at: datetime
    workspace_id: UUID
    context_id: UUID | None
    endpoint: PublicMcpEndpoint | None


async def _live_endpoints(
    db: AsyncSession, workspace_id: UUID, resource_pks: list[UUID]
) -> dict[UUID, PublicMcpEndpoint]:
    """The newest unrevoked endpoint row per resource."""
    if not resource_pks:
        return {}
    rows = (
        (
            await db.execute(
                select(PublicMcpEndpoint)
                .where(
                    PublicMcpEndpoint.workspace_id == workspace_id,
                    PublicMcpEndpoint.resource_pk.in_(resource_pks),
                    PublicMcpEndpoint.revoked_at.is_(None),
                )
                .order_by(PublicMcpEndpoint.created_at.desc(), PublicMcpEndpoint.id)
            )
        )
        .scalars()
        .all()
    )
    live: dict[UUID, PublicMcpEndpoint] = {}
    for row in rows:
        live.setdefault(row.resource_pk, row)
    return live


async def scoped_resources(
    db: AsyncSession,
    scope: ObservationScope,
    *,
    resource_id: str | None = None,
    limit: int = OBSERVABILITY_RESOURCES_MAX,
) -> tuple[list[ScopedResource], bool]:
    """The resources in the credential's scope, by slug.

    Args:
        db: Session.
        scope: The verified credential's scope.
        resource_id: Only this slug (still within the scope).
        limit: At most this many rows.

    Returns:
        ``(resources, truncated)`` — ``truncated`` when more than ``limit`` exist.
    """
    stmt = (
        select(Resource, Context.id)
        .outerjoin(
            Context,
            (Context.workspace_id == Resource.workspace_id)
            & (Context.resource_id == Resource.resource_id)
            & Context.deleted_at.is_(None),
        )
        .where(Resource.workspace_id == scope.workspace_id)
        .order_by(Resource.resource_id, Resource.id)
        .limit(limit + 1)
    )
    if scope.resource_pk is not None:
        stmt = stmt.where(Resource.id == scope.resource_pk)
    if resource_id is not None:
        stmt = stmt.where(Resource.resource_id == resource_id)
    rows = (await db.execute(stmt)).all()
    truncated = len(rows) > limit
    rows = rows[:limit]
    endpoints = await _live_endpoints(db, scope.workspace_id, [r[0].id for r in rows])
    return (
        [
            ScopedResource(
                resource_pk=res.id,
                resource_id=res.resource_id,
                name=res.name,
                created_at=res.created_at,
                workspace_id=res.workspace_id,
                context_id=context_id,
                endpoint=endpoints.get(res.id),
            )
            for res, context_id in rows
        ],
        truncated,
    )


async def resolve_scoped_resource(
    db: AsyncSession, scope: ObservationScope, resource_id: str | None
) -> ScopedResource:
    """The one resource a per-resource read is about.

    A resource-scoped credential may omit ``resource_id`` (its binding); a
    workspace-scoped one must name it.

    Raises:
        ValidationError: A workspace-scoped credential named no resource (422).
        ObservabilityResourceNotFoundError: The slug is not in the scope, or
            does not exist (one 404 for both).
    """
    if resource_id is None and scope.resource_pk is None:
        raise ValidationError(
            "resource_id is required with a workspace-scoped credential", field="resource_id"
        )
    found, _ = await scoped_resources(db, scope, resource_id=resource_id, limit=1)
    if not found:
        raise ObservabilityResourceNotFoundError()
    return found[0]


async def publication_block(db: AsyncSession, resource: ScopedResource) -> dict | None:
    """The resource's live publication, or ``None`` when it has none.

    ``serving`` is whether the anonymous endpoint answers right now: the
    publication gate (``load_serving_endpoint``) passes and neither a plan
    pause nor a capacity lock stops it (``serving_paused``, the transport's
    503).
    """
    endpoint = resource.endpoint
    if endpoint is None:
        return None
    served = await load_serving_endpoint(db, endpoint.public_id)
    return {
        "endpoint_id": endpoint.id,
        "public_id": endpoint.public_id,
        "public_name": endpoint.public_name,
        "published_at": endpoint.created_at,
        "updated_at": endpoint.updated_at,
        "serving": served is not None and not await serving_paused(db, served),
    }
