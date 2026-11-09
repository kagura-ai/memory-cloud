"""Owner routes that publish a resource as an anonymous public MCP endpoint (#2675).

``POST/GET/PATCH/DELETE /api/v1/resources/{resource_id}/public-mcp``. The
workspace owner (session or API key) publishes one resource context with
approved public text, reads it back, edits the text, or unpublishes it. The
address is a server-minted ``pmcp_`` id; a re-publish mints a new one, so an
unpublished address never serves again. Management works while the operator
switch is off; ``serving`` reports whether the endpoint answers right now.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from config.constants import RESOURCE_ID_MAX_LENGTH, RESOURCE_ID_PATTERN
from db.base import get_db
from mcp_server.public.publication import check_publishable, load_serving_endpoint
from models.api_base import TZAwareBaseModel
from models.public_mcp import (
    PUBLIC_DESCRIPTION_MAX_CHARS,
    PUBLIC_NAME_MAX_CHARS,
    PUBLIC_USAGE_GUIDE_MAX_CHARS,
    PublicMcpEndpoint,
)
from services.operation_history_writer import record_operation
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/resources", tags=["resources"])

PUBLIC_MCP_PATH_PREFIX = "/mcp/public/"

ResourceSlug = Path(
    ...,
    min_length=1,
    max_length=RESOURCE_ID_MAX_LENGTH,
    pattern=RESOURCE_ID_PATTERN,
    description="Resource slug (`setup_resource`'s resource_id).",
)


class PublicMcpEndpointCreate(BaseModel):
    """Approved public text for a new publication."""

    public_name: str = Field(
        ..., min_length=1, max_length=PUBLIC_NAME_MAX_CHARS, description="Approved public name"
    )
    public_description: str | None = Field(
        None, max_length=PUBLIC_DESCRIPTION_MAX_CHARS, description="Approved public description"
    )
    public_usage_guide: str | None = Field(
        None,
        max_length=PUBLIC_USAGE_GUIDE_MAX_CHARS,
        description="Approved public usage guide, returned by get_context_info",
    )


class PublicMcpEndpointUpdate(BaseModel):
    """Edit of the approved public text; omitted fields keep their value."""

    public_name: str | None = Field(None, min_length=1, max_length=PUBLIC_NAME_MAX_CHARS)
    public_description: str | None = Field(None, max_length=PUBLIC_DESCRIPTION_MAX_CHARS)
    public_usage_guide: str | None = Field(None, max_length=PUBLIC_USAGE_GUIDE_MAX_CHARS)


class PublicMcpEndpointResponse(TZAwareBaseModel):
    """A live public MCP endpoint of a resource."""

    public_id: str = Field(..., description="Opaque endpoint id (`pmcp_` + 22 base62)")
    endpoint_path: str = Field(
        ..., description="Path of the endpoint on this deployment (`/mcp/public/<public_id>`)"
    )
    resource_id: str
    context_id: UUID
    public_name: str
    public_description: str | None = None
    public_usage_guide: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    serving: bool = Field(
        ...,
        description=(
            "Whether the endpoint answers right now: the operator switch "
            "MCP_PUBLIC_ENDPOINTS_ENABLED is on and the context is still public "
            "and bound to the resource"
        ),
    )


async def _live_endpoint(
    db: AsyncSession, workspace_id: UUID, resource_id: str
) -> PublicMcpEndpoint | None:
    return (
        await db.execute(
            select(PublicMcpEndpoint).where(
                PublicMcpEndpoint.workspace_id == workspace_id,
                PublicMcpEndpoint.resource_id == resource_id,
                PublicMcpEndpoint.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _response(db: AsyncSession, row: PublicMcpEndpoint) -> PublicMcpEndpointResponse:
    serving = await load_serving_endpoint(db, row.public_id) is not None
    return PublicMcpEndpointResponse(
        public_id=row.public_id,
        endpoint_path=f"{PUBLIC_MCP_PATH_PREFIX}{row.public_id}",
        resource_id=row.resource_id,
        context_id=row.context_id,
        public_name=row.public_name,
        public_description=row.public_description,
        public_usage_guide=row.public_usage_guide,
        created_at=row.created_at,
        updated_at=row.updated_at,
        serving=serving,
    )


def _blank_to_none(value: str | None) -> str | None:
    return value if value and value.strip() else None


@router.post(
    "/{resource_id}/public-mcp",
    response_model=PublicMcpEndpointResponse,
    status_code=201,
)
async def publish_public_mcp_endpoint(
    body: PublicMcpEndpointCreate,
    owner: WorkspaceOwner,
    resource_id: str = ResourceSlug,
    db: AsyncSession = Depends(get_db),
) -> PublicMcpEndpointResponse:
    """Publish the resource's context as an anonymous read-only MCP endpoint.

    Preconditions: the resource exists in the workspace and its live context
    is public (``is_public=true``, not private). One live endpoint per
    context (409 otherwise). The public text is what anonymous clients see;
    nothing falls back to the context's internal summary or usage guide.
    """
    user_id, workspace_id = owner
    if not body.public_name.strip():
        raise ValidationError("public_name must not be blank", field="public_name")
    context, resource = await check_publishable(db, workspace_id, resource_id)
    if await _live_endpoint(db, workspace_id, resource_id) is not None:
        raise ConflictError("This resource already has a live public MCP endpoint.")
    row = PublicMcpEndpoint(
        workspace_id=workspace_id,
        context_id=context.id,
        resource_pk=resource.id,
        resource_id=resource_id,
        public_name=body.public_name.strip(),
        public_description=_blank_to_none(body.public_description),
        public_usage_guide=_blank_to_none(body.public_usage_guide),
        created_by=user_id,
        created_at=utcnow(),
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        # A concurrent publish won the partial unique index.
        await db.rollback()
        raise ConflictError("This resource already has a live public MCP endpoint.") from exc
    record_operation(
        db,
        operation="integration.created",
        actor_user_id=user_id,
        context=context,
        target_id=row.public_id,
        subject_user_id=row.created_by,
        changed_fields=["is_public"],
        event_key=f"public-mcp-published:{row.public_id}",
    )
    logger.info(
        "public_mcp_endpoint_published",
        user_id=user_id,
        workspace_id=str(workspace_id),
        resource_id=resource_id,
        endpoint_id=str(row.id),
    )
    return await _response(db, row)


@router.get("/{resource_id}/public-mcp", response_model=PublicMcpEndpointResponse)
async def get_public_mcp_endpoint(
    owner: WorkspaceOwner,
    resource_id: str = ResourceSlug,
    db: AsyncSession = Depends(get_db),
) -> PublicMcpEndpointResponse:
    """Return the resource's live public MCP endpoint (404 when none)."""
    _, workspace_id = owner
    row = await _live_endpoint(db, workspace_id, resource_id)
    if row is None:
        raise NotFoundException("Public MCP endpoint")
    return await _response(db, row)


@router.patch("/{resource_id}/public-mcp", response_model=PublicMcpEndpointResponse)
async def update_public_mcp_endpoint(
    body: PublicMcpEndpointUpdate,
    owner: WorkspaceOwner,
    resource_id: str = ResourceSlug,
    db: AsyncSession = Depends(get_db),
) -> PublicMcpEndpointResponse:
    """Edit the approved public text; the address stays the same.

    An empty string clears the description or usage guide; the name cannot
    be cleared.
    """
    user_id, workspace_id = owner
    row = await _live_endpoint(db, workspace_id, resource_id)
    if row is None:
        raise NotFoundException("Public MCP endpoint")
    fields = body.model_fields_set
    previous = (row.public_name, row.public_description, row.public_usage_guide)
    if "public_name" in fields:
        if body.public_name is None or not body.public_name.strip():
            raise ValidationError("public_name must not be blank", field="public_name")
        row.public_name = body.public_name.strip()
    if "public_description" in fields:
        row.public_description = _blank_to_none(body.public_description)
    if "public_usage_guide" in fields:
        row.public_usage_guide = _blank_to_none(body.public_usage_guide)
    changed_fields = [
        name
        for name, old, new in zip(
            ("name", "description", "usage_guide"),
            previous,
            (row.public_name, row.public_description, row.public_usage_guide),
            strict=True,
        )
        if old != new
    ]
    if changed_fields:
        row.updated_at = utcnow()
        record_operation(
            db,
            operation="integration.updated",
            actor_user_id=user_id,
            workspace_id=workspace_id,
            context_id=row.context_id,
            target_id=row.public_id,
            subject_user_id=row.created_by,
            changed_fields=changed_fields,
        )
    await db.flush()
    logger.info(
        "public_mcp_endpoint_updated",
        user_id=user_id,
        workspace_id=str(workspace_id),
        resource_id=resource_id,
        endpoint_id=str(row.id),
    )
    return await _response(db, row)


@router.delete("/{resource_id}/public-mcp", status_code=204)
async def unpublish_public_mcp_endpoint(
    owner: WorkspaceOwner,
    resource_id: str = ResourceSlug,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Unpublish: the address answers 404 from the next request on."""
    user_id, workspace_id = owner
    row = await _live_endpoint(db, workspace_id, resource_id)
    if row is None:
        raise NotFoundException("Public MCP endpoint")
    row.revoked_at = utcnow()
    row.revoked_by = user_id
    record_operation(
        db,
        operation="integration.deleted",
        actor_user_id=user_id,
        workspace_id=workspace_id,
        context_id=row.context_id,
        target_id=row.public_id,
        subject_user_id=row.created_by,
        changed_fields=["is_public"],
        event_key=f"public-mcp-unpublished:{row.public_id}",
    )
    await db.flush()
    logger.info(
        "public_mcp_endpoint_unpublished",
        user_id=user_id,
        workspace_id=str(workspace_id),
        resource_id=resource_id,
        endpoint_id=str(row.id),
    )
    return Response(status_code=204)
