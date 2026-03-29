"""Resource Schema Management API routes.

Issue #238: Schema Registry for field metadata.

Provides CRUD operations for resource schemas (field definitions).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser, WorkspaceOwner
from db.base import get_db
from models.auth import Context, User
from models.memory import Memory
from models.resource import ResourceSchema, ResourceToken
from utils.exceptions import AuthorizationError, NotFoundException, ValidationError
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
    user: APIKeyOrSessionUser,
    schema_version: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get resource schema (latest or specific version).

    Args:
        resource_id: Resource identifier
        schema_version: Optional schema version (default: latest)
        user: Current user
        db: Database session

    Returns:
        Schema with field definitions

    Example:
        GET /api/v1/resources/ec_products/schema
        GET /api/v1/resources/ec_products/schema?schema_version=2
    """
    logger.info("get_schema_request", resource_id=resource_id, schema_version=schema_version)

    # Build query
    query = select(ResourceSchema).where(ResourceSchema.resource_id == resource_id)

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

    # Get next schema version
    result = await db.execute(
        select(ResourceSchema.schema_version)
        .where(ResourceSchema.resource_id == resource_id)
        .order_by(ResourceSchema.schema_version.desc())
        .limit(1)
    )
    max_version = result.scalar()
    new_version = (max_version or 0) + 1

    # Create schema
    schema = ResourceSchema(
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
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Get resource change impact information.

    Issue #266: Shows the impact of creating/modifying a schema for this resource.
    Security Fix: Added workspace boundary check to prevent IDOR vulnerability.

    Args:
        resource_id: Resource identifier
        user: Current user
        db: Database session

    Returns:
        Impact information including token count, memory count, and current schema version

    Raises:
        NotFoundException: If resource_id doesn't exist
        AuthorizationError: If user doesn't have access to this resource

    Example:
        GET /api/v1/resources/ec_products/impact

        Response:
        {
            "resource_id": "ec_products",
            "token_count": 3,
            "memory_count": 1234,
            "current_schema_version": 2
        }
    """
    logger.info("get_resource_impact_request", resource_id=resource_id, user_id=user["user_id"])

    # Security: Verify resource_id belongs to user's workspace
    user_result = await db.execute(
        select(User.current_workspace_id).where(User.user_id == user["user_id"])
    )
    current_workspace_id = user_result.scalar_one_or_none()

    if not current_workspace_id:
        raise AuthorizationError("User must belong to an workspace")

    # Check if resource_id exists and belongs to user's workspace
    context_result = await db.execute(
        select(Context.id).where(
            Context.resource_id == resource_id, Context.workspace_id == current_workspace_id
        )
    )
    context = context_result.scalar_one_or_none()

    if not context:
        raise NotFoundException("resource", resource_id)

    # Performance: Get all stats in a single query using subqueries
    token_count_subq = (
        select(func.count(ResourceToken.id))
        .where(ResourceToken.resource_id == resource_id, ResourceToken.is_active == True)  # noqa: E712
        .scalar_subquery()
    )

    memory_count_subq = (
        select(func.count(Memory.id))
        .where(Memory.resource_id == resource_id, Memory.deleted_at.is_(None))
        .scalar_subquery()
    )

    schema_version_subq = (
        select(func.max(ResourceSchema.schema_version))
        .where(ResourceSchema.resource_id == resource_id)
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
