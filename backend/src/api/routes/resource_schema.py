"""Resource Schema Management API routes.

Issue #238: Schema Registry for field metadata.

Provides CRUD operations for resource schemas (field definitions).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from db.base import get_db
from models.memory import Memory
from models.resource import ResourceSchema, ResourceToken
from services.permission_service import PermissionService
from services.resource_lookup import resolve_resource_pk
from utils.exceptions import NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/resources", tags=["resource-schema"])


# ============================================================================
# Pydantic Models
# ============================================================================


class FieldDefinition(BaseModel):
    """Field metadata definition."""

    name: str = Field(..., min_length=1, max_length=100, description="Field name in JSONB payload")
    type: str = Field(
        ...,
        pattern=r"^(text|number|boolean|date|array|object)$",
        description="Field data type",
    )
    description: str = Field(
        ..., min_length=1, max_length=500, description="Human-readable description"
    )
    classification: str = Field(
        "public",
        pattern=r"^(public|internal|pii|confidential)$",
        description="Data classification for access control",
    )
    index_hint: str = Field(
        "",
        max_length=100,
        description="Indexing hints: fulltext, vector, sort, facet, metadata",
    )
    unit: str | None = Field(None, max_length=50, description="Unit of measurement (e.g., JPY, kg)")
    enum_values: list[str] | None = Field(None, description="Allowed values for categorical fields")
    example: str | None = Field(None, max_length=200, description="Example value")
    required: bool = Field(False, description="Whether field is required")


class SchemaCreateRequest(BaseModel):
    """Request to create a new resource schema."""

    resource_id: str = Field(..., min_length=1, max_length=255, description="Resource identifier")
    field_definitions: list[FieldDefinition] = Field(
        ..., min_length=1, max_length=100, description="Field metadata (max 100 fields)"
    )


class SchemaResponse(BaseModel):
    """Response with resource schema."""

    resource_id: str
    schema_version: int
    field_definitions: list[FieldDefinition]
    created_at: str


class ResourceImpactResponse(BaseModel):
    """Response with resource change impact information.

    Issue #266: Schema change impact warnings.
    """

    resource_id: str
    token_count: int = Field(..., description="Number of active tokens using this resource")
    memory_count: int = Field(..., description="Number of existing memories from this resource")
    current_schema_version: int | None = Field(
        None, description="Current schema version (null if no schema exists)"
    )


# ============================================================================
# Schema API Endpoints
# ============================================================================


@router.get("/{resource_id}/schema", response_model=SchemaResponse)
async def get_schema(
    resource_id: str,
    owner: WorkspaceOwner,
    schema_version: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get resource schema (latest or specific version).

    Owner-only (#389): ``WorkspaceOwner`` rejects non-owners with 403;
    ``resolve_resource_by_slug`` returns 404 on cross-workspace probes.

    Example:
        GET /api/v1/resources/ec_products/schema
        GET /api/v1/resources/ec_products/schema?schema_version=2
    """
    user_id, _ = owner
    logger.info(
        "get_schema_request",
        resource_id=resource_id,
        schema_version=schema_version,
        user_id=user_id,
    )

    # Workspace boundary + cross-workspace 404 disclosure.
    # required_role="owner" is load-bearing: WorkspaceOwner only verifies
    # ownership of the caller's *current* workspace, not the workspace that
    # owns the slug. A user who is owner of workspace A AND member (or
    # admin) of workspace B could otherwise probe B's slug with this helper
    # at default required_role="member" and receive B's schema data. See
    # Copilot catch on PR #391.
    context = await PermissionService(db).resolve_resource_by_slug(
        user_id=user_id,
        resource_id=resource_id,
        required_role="owner",
    )

    # Issue #390 Phase 2: strict ``resource_pk`` filter on the satellite
    # table. ``ResourceSchema`` has no ``context_id`` column, so a slug-only
    # filter is not workspace-safe — soft-delete + slug reuse would surface
    # the prior workspace's schemas. Fail-safe to 404 when the Resource row
    # is absent (pre-a97 orphan or migration gap) rather than fall back to
    # slug filtering.
    resource_pk = await resolve_resource_pk(db, context.workspace_id, resource_id)
    if resource_pk is None:
        raise NotFoundException("Resource schema", resource_id)

    query = select(ResourceSchema).where(ResourceSchema.resource_pk == resource_pk)

    if schema_version:
        query = query.where(ResourceSchema.schema_version == schema_version)
    else:
        query = query.order_by(ResourceSchema.schema_version.desc())

    query = query.limit(1)

    # Execute
    result = await db.execute(query)
    schema = result.scalar_one_or_none()

    if not schema:
        raise NotFoundException("Resource schema", resource_id)

    return SchemaResponse(
        resource_id=schema.resource_id,
        schema_version=schema.schema_version,
        field_definitions=schema.field_definitions,
        created_at=schema.created_at.isoformat(),
    )


@router.post(
    "/{resource_id}/schema", response_model=SchemaResponse, status_code=status.HTTP_201_CREATED
)
async def create_schema(
    resource_id: str,
    request: SchemaCreateRequest,
    owner: WorkspaceOwner,
    db: AsyncSession = Depends(get_db),
):
    """Create a new resource schema version.

    Issue #276: Owner-only access via WorkspaceOwner dependency.

    Args:
        resource_id: Resource identifier
        request: Schema creation request
        owner: Workspace owner (user_id, workspace_id) from dependency
        db: Database session

    Returns:
        Created schema

    Raises:
        403: Not workspace owner
        400: Invalid resource_id or schema definition

    Notes:
        - Automatically increments schema_version
        - Only workspace owners can create schemas
        - Triggers rebuild for all contexts using this resource (TODO: Week 4)

    Example:
        POST /api/v1/resources/ec_products/schema
        Body:
        {
            "resource_id": "ec_products",
            "field_definitions": [
                {
                    "name": "product_name",
                    "type": "text",
                    "description": "商品名",
                    "classification": "public",
                    "index_hint": "fulltext+vector",
                    "required": true
                },
                {
                    "name": "price",
                    "type": "number",
                    "description": "価格（税込）",
                    "unit": "JPY",
                    "index_hint": "sort+facet"
                }
            ]
        }
    """
    user_id, workspace_id = owner
    logger.info(
        "create_schema_request",
        resource_id=resource_id,
        field_count=len(request.field_definitions),
        user_id=user_id,
    )

    # Validate resource_id matches
    if request.resource_id != resource_id:
        raise ValidationError("resource_id in body must match URL parameter")

    # Issue #390 Phase 2: resolve ``resource_pk`` so the writer populates
    # both columns atomically. The before_insert event listener on
    # ResourceSchema rejects inserts that set resource_id without
    # resource_pk — surfacing this Phase 2 contract at the model layer
    # rather than relying on every future writer to remember.
    resource_pk = await resolve_resource_pk(db, workspace_id, resource_id)
    if resource_pk is None:
        # Schema creation is owner-only and goes through WorkspaceOwner +
        # resolve_resource_by_slug upstream (added in the read-path hardening
        # for #390), but the POST endpoint here does not currently resolve
        # the slug — the route pre-dates #326. A missing Resource row at
        # this point means the caller supplied a slug that is not bound to
        # any live Context in their workspace; reject with 404 for uniform
        # disclosure (matches the GET side's cross-workspace probe contract).
        raise NotFoundException("Resource", resource_id)

    # Get next schema version (strict resource_pk filter — see get_schema).
    result = await db.execute(
        select(ResourceSchema.schema_version)
        .where(ResourceSchema.resource_pk == resource_pk)
        .order_by(ResourceSchema.schema_version.desc())
        .limit(1)
    )
    max_version = result.scalar()
    new_version = (max_version or 0) + 1

    # Create schema with both resource_pk (authoritative FK) and resource_id
    # (legacy mirror, kept for API read contracts until Phase C drops it).
    schema = ResourceSchema(
        resource_pk=resource_pk,
        resource_id=resource_id,
        schema_version=new_version,
        field_definitions=[f.model_dump() for f in request.field_definitions],
    )

    db.add(schema)
    await db.commit()
    await db.refresh(schema)

    logger.info(
        "schema_created",
        resource_id=resource_id,
        schema_version=new_version,
        field_count=len(request.field_definitions),
    )

    # TODO: Trigger rebuild for all contexts using this resource (Week 4)

    return SchemaResponse(
        resource_id=schema.resource_id,
        schema_version=schema.schema_version,
        field_definitions=schema.field_definitions,
        created_at=schema.created_at.isoformat(),
    )


@router.get("/{resource_id}/impact", response_model=ResourceImpactResponse)
async def get_resource_impact(
    resource_id: str,
    owner: WorkspaceOwner,
    db: AsyncSession = Depends(get_db),
):
    """Get resource change impact information.

    Issue #266: Shows the impact of creating/modifying a schema.
    Owner-only (#389): same two-layer gate as ``get_schema`` —
    ``WorkspaceOwner`` → 403 for non-owners, ``resolve_resource_by_slug`` →
    404 on cross-workspace probes. The prior manual workspace-boundary
    SELECT block was superseded by the helper.

    Example:
        GET /api/v1/resources/ec_products/impact
    """
    user_id, _ = owner
    logger.info("get_resource_impact_request", resource_id=resource_id, user_id=user_id)

    # Workspace boundary + cross-workspace 404 disclosure. See get_schema
    # for the required_role="owner" rationale (multi-workspace member case).
    context = await PermissionService(db).resolve_resource_by_slug(
        user_id=user_id,
        resource_id=resource_id,
        required_role="owner",
    )

    # Issue #390 Phase 2: resolve ``resource_pk`` so impact subqueries
    # scope by authoritative FK instead of by slug. At this point
    # ``resolve_resource_by_slug`` has already confirmed the Context
    # exists and the caller has owner access, so ``resource_pk is None``
    # means a data-integrity gap (Context persists without a backing
    # Resource entity row — setup_resource never ran or the row was
    # deleted). Returning 200 with zeroed counts would silently hide
    # this; raise 404 with an actionable hint instead.
    resource_pk = await resolve_resource_pk(db, context.workspace_id, resource_id)
    if resource_pk is None:
        logger.warning(
            "resource_entity_missing_on_impact",
            resource_id=resource_id,
            workspace_id=str(context.workspace_id),
            user_id=user_id,
        )
        raise NotFoundException("Resource", resource_id)

    # Performance: Get all stats in a single query using subqueries.
    # ResourceToken + ResourceSchema use strict ``resource_pk`` filter.
    # Memory still uses resource_id (slug) because its workspace_id column
    # scopes the query defensively — this is the pre-existing behavior and
    # not part of the #390 Phase A scope.
    token_count_subq = (
        select(func.count(ResourceToken.id))
        .where(ResourceToken.resource_pk == resource_pk, ResourceToken.is_active == True)  # noqa: E712
        .scalar_subquery()
    )

    memory_count_subq = (
        select(func.count(Memory.id))
        .where(
            Memory.resource_id == resource_id,
            Memory.workspace_id == context.workspace_id,
            Memory.deleted_at.is_(None),
        )
        .scalar_subquery()
    )

    schema_version_subq = (
        select(func.max(ResourceSchema.schema_version))
        .where(ResourceSchema.resource_pk == resource_pk)
        .scalar_subquery()
    )

    # Execute in single query
    result = await db.execute(
        select(
            token_count_subq.label("token_count"),
            memory_count_subq.label("memory_count"),
            schema_version_subq.label("schema_version"),
        )
    )
    row = result.one()

    token_count = row.token_count or 0
    memory_count = row.memory_count or 0
    current_schema_version = row.schema_version

    logger.info(
        "resource_impact_calculated",
        resource_id=resource_id,
        token_count=token_count,
        memory_count=memory_count,
        current_schema_version=current_schema_version,
    )

    return ResourceImpactResponse(
        resource_id=resource_id,
        token_count=token_count,
        memory_count=memory_count,
        current_schema_version=current_schema_version,
    )
