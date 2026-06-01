"""Resource Token Management API routes.

Issue #242: Resource Token Management UI - Backend API endpoints.

Provides CRUD operations for resource tokens with owner-only access control.
Pattern: Based on api_keys.py with resource-specific adaptations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from auth.resource_tokens import ResourceTokenManager
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.resource import ResourceToken, WorkspaceConnector
from services.resource_lookup import resolve_resource_pk
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/resource-tokens", tags=["resource-tokens"])


# ============================================================================
# Dependency Injection
# ============================================================================


async def get_resource_token_manager(db: AsyncSession = Depends(get_db)) -> ResourceTokenManager:
    """Get ResourceTokenManager instance.

    Args:
        db: Database session

    Returns:
        ResourceTokenManager instance
    """
    return ResourceTokenManager(db)


# ============================================================================
# Pydantic Models
# ============================================================================


class ResourceTokenCreate(BaseModel):
    """Request model for creating a resource token."""

    resource_id: str = Field(
        ..., min_length=1, max_length=255, description="Resource identifier this token is scoped to"
    )
    description: str | None = Field(None, max_length=500, description="Human-readable description")
    quota_events_per_hour: int = Field(
        1000, ge=1, le=10000, description="Event ingestion quota per hour (default: 1000)"
    )


class ResourceTokenUpdate(BaseModel):
    """Request model for updating a resource token."""

    description: str | None = Field(None, max_length=500, description="Updated description")
    quota_events_per_hour: int | None = Field(
        None, ge=1, le=10000, description="Updated quota (1-10000)"
    )


class ResourceTokenResponse(TZAwareBaseModel):
    """Response model for resource token metadata (no plaintext)."""

    id: int = Field(..., description="Database ID")
    resource_id: str = Field(..., description="Resource identifier")
    description: str | None = Field(None, description="Human-readable description")
    quota_events_per_hour: int = Field(..., description="Event ingestion quota per hour")
    created_by: str | None = Field(None, description="User ID who created this token")
    created_at: datetime = Field(..., description="Creation timestamp")
    last_used_at: datetime | None = Field(None, description="Last usage timestamp")
    is_active: bool = Field(..., description="Whether token is active")
    status: Literal["active", "revoked"] = Field(..., description="Current status")

    model_config = {"from_attributes": True}


class PaginatedResourceTokensResponse(BaseModel):
    """Paginated response for resource tokens.

    Issue #264: Pagination support for large token lists.
    """

    tokens: list[ResourceTokenResponse] = Field(..., description="List of resource tokens")
    total: int = Field(..., description="Total number of tokens matching filter")
    limit: int = Field(..., description="Number of tokens per page")
    offset: int = Field(..., description="Starting offset")


class ResourceTokenCreateResponse(ResourceTokenResponse):
    """Response model for resource token creation (includes plaintext token).

    WARNING: The `token` field is shown ONLY once. Client must save it immediately.
    """

    token: str = Field(
        ...,
        description="Plaintext resource token (ONLY shown once - must be saved by client)",
    )


# ============================================================================
# Helper Functions
# ============================================================================


def _determine_status(is_active: bool) -> Literal["active", "revoked"]:
    """Determine resource token status.

    Args:
        is_active: Whether token is active

    Returns:
        Status string: "active" or "revoked"
    """
    return "active" if is_active else "revoked"


def _format_token_response(token: ResourceToken) -> ResourceTokenResponse:
    """Format ResourceToken object into ResourceTokenResponse.

    Args:
        token: ResourceToken ORM object

    Returns:
        Formatted ResourceTokenResponse model
    """
    status = _determine_status(token.is_active)

    return ResourceTokenResponse(
        id=token.id,
        resource_id=token.resource_id,
        description=token.description,
        quota_events_per_hour=token.quota_events_per_hour,
        created_by=token.created_by,
        created_at=token.created_at,
        last_used_at=token.last_used_at,
        is_active=token.is_active,
        status=status,
    )


# ============================================================================
# Routes
# ============================================================================


@router.get("", response_model=PaginatedResourceTokensResponse)
async def list_resource_tokens(
    owner: WorkspaceOwner,
    manager: ResourceTokenManager = Depends(get_resource_token_manager),
    db: AsyncSession = Depends(get_db),
    resource_id: str | None = Query(None, description="Filter by resource_id"),
    limit: int = Query(50, ge=1, le=100, description="Number of tokens per page (max 100)"),
    offset: int = Query(0, ge=0, description="Starting offset for pagination"),
) -> PaginatedResourceTokensResponse:
    """List resource tokens with pagination (optionally filtered by resource_id).

    Issue #242: Owner-only access.
    Issue #264: Added pagination support.
    Issue #59: Changed from APIKeyOrSessionUser to WorkspaceOwner.

    Args:
        resource_id: Optional filter by resource_id
        limit: Number of tokens per page (1-100, default 50)
        offset: Starting offset (default 0)
        owner: Workspace owner (user_id, workspace_id) from dependency
        manager: ResourceTokenManager instance

    Returns:
        Paginated response with tokens, total count, limit, and offset
    """
    try:
        user_id, current_workspace_id = owner
        logger.info(
            "list_resource_tokens_request",
            user_id=user_id,
            resource_id=resource_id,
            limit=limit,
            offset=offset,
        )

        # SECURITY: Workspace boundary check when filtering by resource_id
        # Issue #268/#270: Verify resource belongs to owner's workspace
        if resource_id is not None:
            from models.auth import Context

            # SECURITY: Verify resource_id belongs to current workspace
            # Issue #268: Prevent accessing tokens for resources in other workspaces
            context_result = await db.execute(
                select(Context.id).where(
                    and_(
                        Context.resource_id == resource_id,
                        Context.workspace_id == current_workspace_id,
                        Context.deleted_at.is_(None),
                    )
                )
            )
            context_exists = context_result.scalar_one_or_none()

            if not context_exists:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Resource ID '{resource_id}' not found in your workspace or you don't have access to it.",
                )

        # Performance: DB-level filtering and pagination (not in-memory)
        total = await manager.count_tokens(
            resource_id=resource_id, created_by=user_id, include_revoked=True
        )

        tokens = await manager.list_tokens(
            resource_id=resource_id,
            created_by=user_id,
            include_revoked=True,
            limit=limit,
            offset=offset,
        )

        return PaginatedResourceTokensResponse(
            tokens=[_format_token_response(token) for token in tokens],
            total=total,
            limit=limit,
            offset=offset,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_resource_tokens_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve resource tokens",
        ) from e


@router.post("", response_model=ResourceTokenCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_resource_token(
    data: ResourceTokenCreate,
    owner: WorkspaceOwner,
    manager: ResourceTokenManager = Depends(get_resource_token_manager),
    db: AsyncSession = Depends(get_db),
) -> ResourceTokenCreateResponse:
    """Create a new resource token.

    Issue #242: Owner-only access. Returns plaintext token ONLY once.
    Issue #276: Uses WorkspaceOwner dependency for DRY principle.

    Args:
        data: Token creation request
        owner: Workspace owner (user_id, workspace_id) from dependency
        manager: ResourceTokenManager instance
        db: Database session

    Returns:
        Token metadata + plaintext token (shown ONLY once)

    Raises:
        400: Invalid resource_id
        403: Not workspace owner
        500: Failed to create token
    """
    try:
        user_id, workspace_id = owner
        logger.info(
            "create_resource_token_request",
            user_id=user_id,
            resource_id=data.resource_id,
            quota=data.quota_events_per_hour,
        )

        # Check plan limits and active token count
        from sqlalchemy import func, select

        from config.plan_tiers import get_plan_tier
        from models.auth import Context, Workspace

        # SECURITY: Verify resource_id belongs to current workspace
        # Issue #268: Workspace boundary violation prevention
        context_result = await db.execute(
            select(Context.id).where(
                and_(
                    Context.resource_id == data.resource_id,
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                )
            )
        )
        context_exists = context_result.scalar_one_or_none()

        if not context_exists:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Resource ID '{data.resource_id}' not found in your workspace or you don't have access to it.",
            )

        if workspace_id:
            workspace_result = await db.execute(
                select(Workspace.plan_name).where(Workspace.id == workspace_id)
            )
            plan_name = workspace_result.scalar_one_or_none()

            if plan_name:
                plan = get_plan_tier(plan_name)

                # Check if plan supports resource tokens
                if plan.max_resource_tokens == 0:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Resource Tokens require PRO plan. Public contexts are not available on Free or Basic plans.",
                    )

                # Check active token count limit
                # Note: Race condition possible but low impact (concurrent creation rare)
                # Alternative: Use database constraint on token count (future improvement)
                #
                # Issue #858: exclude connector-owned tokens from this count.
                # The connector setup flow mints a resource token that bypasses
                # the max_resource_tokens gate on purpose (connectors are gated
                # by max_connectors seats instead). Counting it here would let it
                # eat a regular slot post-mint — an asymmetry that can prematurely
                # 403 a legitimate regular-token creation. The anti-join against
                # workspace_connectors (UNIQUE resource_pk, so no count inflation)
                # drops exactly the connector-owned tokens. A regular token with a
                # NULL resource_pk never matches the join condition, so it is
                # correctly still counted.
                active_count_result = await db.execute(
                    select(func.count(ResourceToken.id))
                    .outerjoin(
                        WorkspaceConnector,
                        WorkspaceConnector.resource_pk == ResourceToken.resource_pk,
                    )
                    .where(
                        and_(
                            ResourceToken.created_by == user_id,
                            ResourceToken.is_active == True,  # noqa: E712
                            WorkspaceConnector.id.is_(None),
                        )
                    )
                )
                active_count = active_count_result.scalar() or 0

                if active_count >= plan.max_resource_tokens:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Token limit reached. Your {plan_name.upper()} plan allows {plan.max_resource_tokens} active tokens. Please revoke unused tokens or upgrade your plan.",
                    )

        # Issue #390 Phase 2: resolve authoritative ``resource_pk`` + pass
        # ``workspace_id`` so the ResourceToken insert satisfies the
        # before_insert event listener invariant (models/resource.py).
        # The context-existence check above already confirmed the Resource
        # is bound to this workspace; if resolve_resource_pk still returns
        # None it indicates either a delete race between the two queries or
        # a data-integrity gap (Context exists without a backing Resource
        # row), NOT an authorization failure. Surface 409 CONFLICT with an
        # actionable hint so operators can distinguish "not authorized" from
        # "resource binding is inconsistent" in logs and error reports.
        resource_pk = await resolve_resource_pk(db, workspace_id, data.resource_id)
        if resource_pk is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Resource ID '{data.resource_id}' exists as a Context but has no "
                    "backing Resource entity row. This is either a delete race or a "
                    "data-integrity gap. Retry in a moment, or run setup_resource() "
                    "to rebind."
                ),
            )

        # Create token (returns plaintext + token object)
        plaintext_token, new_token = await manager.create_token(
            resource_id=data.resource_id,
            resource_pk=resource_pk,
            workspace_id=workspace_id,
            description=data.description,
            quota_events_per_hour=data.quota_events_per_hour,
            created_by=user_id,
        )

        # Commit transaction (Issue #242: Fix - tokens not persisted)
        try:
            await db.commit()
        except Exception as commit_error:
            # Rollback if commit fails (Code review C-6)
            await db.rollback()
            logger.error("token_creation_commit_failed", error=str(commit_error), user_id=user_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to save resource token. Please try again.",
            ) from commit_error

        # Refresh to get DB-generated fields
        await db.refresh(new_token)

        response_data = _format_token_response(new_token)
        return ResourceTokenCreateResponse(**response_data.model_dump(), token=plaintext_token)

    except ValueError as e:
        logger.warning("create_resource_token_validation_error", error=str(e), user_id=user_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except HTTPException:
        # Re-raise HTTP exceptions (plan check, etc.) without wrapping
        raise
    except Exception as e:
        logger.error("create_resource_token_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create resource token",
        ) from e


@router.patch("/{token_id}", response_model=ResourceTokenResponse)
async def update_resource_token(
    token_id: int,
    request: ResourceTokenUpdate,
    owner: WorkspaceOwner,
    db: AsyncSession = Depends(get_db),
) -> ResourceTokenResponse:
    """Update resource token description and/or quota.

    Issue #242: Allow updating token metadata without regenerating.
    Issue #276: Uses WorkspaceOwner dependency for DRY principle.

    Args:
        token_id: Token ID
        request: Update request
        owner: Workspace owner (user_id, workspace_id) from dependency
        db: Database session

    Returns:
        Updated token metadata

    Raises:
        403: Not workspace owner
        404: Token not found
    """
    try:
        user_id, current_workspace_id = owner

        # SECURITY: Get token and verify ownership
        from models.auth import Context

        # Verify ownership
        result = await db.execute(
            select(ResourceToken).where(
                and_(
                    ResourceToken.id == token_id,
                    ResourceToken.created_by == user_id,
                )
            )
        )
        token = result.scalar_one_or_none()

        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Resource token not found or not owned by you",
            )

        # SECURITY: Verify resource_id belongs to current workspace
        # Issue #268: Prevent updating tokens for resources in other workspaces
        context_result = await db.execute(
            select(Context.id).where(
                and_(
                    Context.resource_id == token.resource_id,
                    Context.workspace_id == current_workspace_id,
                    Context.deleted_at.is_(None),
                )
            )
        )
        context_exists = context_result.scalar_one_or_none()

        if not context_exists:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Resource token belongs to a different workspace.",
            )

        # Validate new quota doesn't exceed plan limits (Code review M-10)
        if request.quota_events_per_hour is not None:
            # Get user's plan
            from sqlalchemy import func as sql_func

            from config.plan_tiers import get_plan_tier
            from models.auth import Workspace

            workspace_id = current_workspace_id
            if workspace_id:
                workspace_result = await db.execute(
                    select(Workspace.plan_name).where(Workspace.id == workspace_id)
                )
                plan_name = workspace_result.scalar_one_or_none()

                if plan_name:
                    plan = get_plan_tier(plan_name)
                    max_total_quota = plan.max_resource_tokens * 10000

                    # Calculate quota used by OTHER tokens
                    other_tokens_result = await db.execute(
                        select(sql_func.sum(ResourceToken.quota_events_per_hour)).where(
                            and_(
                                ResourceToken.created_by == user_id,
                                ResourceToken.is_active == True,  # noqa: E712
                                ResourceToken.id != token_id,
                            )
                        )
                    )
                    used_by_others = other_tokens_result.scalar() or 0

                    # Check if new quota fits
                    if used_by_others + request.quota_events_per_hour > max_total_quota:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Quota limit exceeded. Total quota (including this update) would be {used_by_others + request.quota_events_per_hour}, but plan allows {max_total_quota}.",
                        )

        # Update fields
        if request.description is not None:
            token.description = request.description

        if request.quota_events_per_hour is not None:
            token.quota_events_per_hour = request.quota_events_per_hour

        await db.commit()
        await db.refresh(token)

        logger.info(
            "resource_token_updated",
            token_id=token_id,
            user_id=user_id,
        )

        return _format_token_response(token)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_resource_token_failed", error=str(e), token_id=token_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update resource token",
        ) from e


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoke_resource_token(
    token_id: int,
    owner: WorkspaceOwner,
    manager: ResourceTokenManager = Depends(get_resource_token_manager),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a resource token (soft delete).

    Issue #242: Owner-only access. Sets is_active=False (preserves audit trail).
    Issue #276: Uses WorkspaceOwner dependency for DRY principle.

    Args:
        token_id: Token ID to revoke
        owner: Workspace owner (user_id, workspace_id) from dependency
        manager: ResourceTokenManager instance
        db: Database session

    Raises:
        403: Not workspace owner
        404: Token not found
        500: Failed to revoke token
    """
    try:
        user_id, current_workspace_id = owner
        logger.info("revoke_resource_token_request", user_id=user_id, token_id=token_id)

        # SECURITY: Verify token exists and ownership
        from models.auth import Context

        # Verify token exists and ownership (security)
        result = await db.execute(
            select(ResourceToken).where(
                and_(
                    ResourceToken.id == token_id,
                    ResourceToken.created_by == user_id,  # Security: verify ownership
                )
            )
        )
        target_token = result.scalar_one_or_none()

        if not target_token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Resource token not found",
            )

        # SECURITY: Verify resource_id belongs to current workspace
        # Issue #268: Prevent revoking tokens for resources in other workspaces
        context_result = await db.execute(
            select(Context.id).where(
                and_(
                    Context.resource_id == target_token.resource_id,
                    Context.workspace_id == current_workspace_id,
                    Context.deleted_at.is_(None),
                )
            )
        )
        context_exists = context_result.scalar_one_or_none()

        if not context_exists:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Resource token belongs to a different workspace.",
            )

        # Revoke token (soft delete)
        await manager.revoke_token(token_id)
        await db.commit()

        logger.info(
            "resource_token_revoked",
            user_id=user_id,
            token_id=token_id,
            resource_id=target_token.resource_id,
        )

    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("revoke_resource_token_not_found", error=str(e), token_id=token_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e
    except Exception as e:
        logger.error("revoke_resource_token_failed", error=str(e), token_id=token_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke resource token",
        ) from e
