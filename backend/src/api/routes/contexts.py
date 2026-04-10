"""Context management routes.

Issue #82: Context-based multi-collection support.
Issue #252: CRUD operations use session-only auth (SessionUser).
Issue #150: Read-only analytics (memory-stats, duplicates) accept API key auth (APIKeyOrSessionUser).

Provides CRUD operations for contexts with collection management.
Each context maps to a separate Qdrant collection for memory isolation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser, SessionUser, get_current_user
from db.base import get_db
from services.context_service import ContextService
from utils.exceptions import NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/contexts", tags=["contexts"])


# ============================================================================
# Helper Functions
# ============================================================================


async def _handle_resource_id_duplicate_error(
    db: AsyncSession,
    resource_id: str,
    workspace_id: UUID,
    exclude_context_id: UUID | None = None,
) -> None:
    """Handle resource_id duplicate IntegrityError with descriptive message.

    Issue #276: Provide helpful error message showing which context uses the resource_id.

    Args:
        db: Database session
        resource_id: The duplicate resource_id
        workspace_id: Workspace to search in
        exclude_context_id: Context ID to exclude from search (for updates)

    Raises:
        HTTPException: 400 with descriptive error message
    """
    from models.auth import Context

    # Rollback to allow new query after IntegrityError
    await db.rollback()

    try:
        # Find which context is using this resource_id
        query = select(Context.id, Context.display_name, Context.name).where(
            and_(
                Context.resource_id == resource_id,
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )

        if exclude_context_id:
            query = query.where(Context.id != exclude_context_id)

        result = await db.execute(query)
        existing_ctx = result.one_or_none()

        if existing_ctx:
            ctx_id, display_name, name = existing_ctx
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Resource ID '{resource_id}' is already used by context '{display_name or name}' in this workspace. Please choose a different Resource ID.",
            )
    except HTTPException:
        raise
    except Exception as e:
        # Query failed - still show generic error
        logger.error(f"Failed to query duplicate resource_id: {e}")
        pass

    # Fallback error message
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Resource ID '{resource_id}' is already used in this workspace.",
    )


# ============================================================================
# Pydantic Models
# ============================================================================


class ContextCreate(BaseModel):
    """Request model for creating a context.

    Issue #146: Now requires embedding model selection (immutable after creation).
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9_-]+$",
        description="Context name (lowercase alphanumeric, hyphen, underscore)",
    )
    display_name: str | None = Field(
        None,
        max_length=200,
        description="Human-readable display name (optional, defaults to name)",
    )
    description: str | None = Field(
        None,
        max_length=500,
        description="Optional context description",
    )
    summary: str | None = Field(
        None,
        max_length=500,
        description="LLM-oriented context summary (200-500 chars)",
    )
    usage_guide: str | None = Field(
        None,
        max_length=2000,
        description="LLM-oriented memory usage guidelines",
    )
    embedding_model: str | None = Field(
        None,
        description="Embedding model for this context. Default: global EMBEDDING_MODEL setting. Immutable after creation.",
    )
    is_private: bool = Field(
        True,
        description="Privacy: TRUE=private (creator only), FALSE=shared (workspace members)",
    )


class ContextUpdate(BaseModel):
    """Request model for updating a context."""

    display_name: str | None = Field(
        None,
        max_length=200,
        description="Updated display name",
    )
    description: str | None = Field(
        None,
        max_length=500,
        description="Updated context description",
    )
    summary: str | None = Field(
        None,
        max_length=500,
        description="Updated context summary",
    )
    usage_guide: str | None = Field(
        None,
        max_length=2000,
        description="Updated usage guidelines",
    )
    is_private: bool | None = Field(
        None,
        description="Privacy flag (Migration 034: Shared → Private removes non-owner members)",
    )
    is_public: bool | None = Field(
        None,
        description="Public flag (Issue #238: Public REST API access)",
    )
    resource_id: str | None = Field(
        None,
        max_length=255,
        pattern=r"^[a-z0-9_-]+$",
        description="Resource ID for contexts (lowercase alphanumeric, underscore, and hyphen)",
    )
    is_locked: bool | None = Field(
        None,
        description="When true, prevents this context from being deleted until unlocked.",
    )


class ContextResponse(BaseModel):
    """Response model for context data."""

    id: UUID = Field(..., description="Context UUID")
    name: str = Field(..., description="Context name")
    display_name: str | None = Field(None, description="Human-readable display name")
    description: str | None = Field(None, description="Context description")
    summary: str | None = Field(None, description="LLM-oriented context summary")
    usage_guide: str | None = Field(None, description="LLM-oriented usage guidelines")
    is_default: bool = Field(..., description="Whether this is the default context")
    # Issue #246: is_current removed (context always explicit from Frontend URL)
    is_private: bool = Field(True, description="Privacy: TRUE=private, FALSE=shared")  # Issue #165
    is_public: bool = Field(
        False, description="Public: TRUE=external API access, FALSE=internal"
    )  # Issue #238
    resource_id: str | None = Field(
        None, description="Resource ID for public contexts"
    )  # Issue #238
    is_locked: bool = Field(False, description="When true, deletion is prevented until unlocked")
    created_by: str | None = Field(None, description="Creator user ID")  # Issue #165
    created_by_name: str | None = Field(None, description="Creator name")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime | None = Field(None, description="Last update timestamp")
    # Issue #217: Search config summary for context card display
    use_rerank: bool | None = Field(None, description="Reranking enabled (Basic+ only)")
    reranker_provider: str | None = Field(None, description="Reranker provider: voyage/cohere")
    # Embedding model info (Issue #49)
    embedding_model: str | None = Field(None, description="Embedding model used for this context")
    embedding_dimensions: int | None = Field(None, description="Embedding vector dimensions")
    # Context members count (workspace members with access + explicit context members)
    member_count: int | None = Field(
        None, description="Number of members with access to this context"
    )
    # Issue #187: Memory count and last activity for contexts table redesign
    memory_count: int = Field(default=0, description="Number of active memories in this context")
    last_activity_at: datetime | None = Field(
        None, description="Most recent memory activity (max of updated_at across memories)"
    )

    model_config = {"from_attributes": True}


class ContextListResponse(BaseModel):
    """Response model for context list."""

    contexts: list[ContextResponse] = Field(..., description="List of contexts")
    # Issue #246: current_context_id removed (context always explicit)
    total: int = Field(..., description="Total number of contexts")


class ContextStatsResponse(BaseModel):
    """Response model for context statistics.

    Single Collection Migration: Stats now from PostgreSQL (memory_count).
    """

    context_id: UUID = Field(..., description="Context UUID")
    context_name: str = Field(..., description="Context name")
    memory_count: int = Field(..., description="Number of memories in context")
    status: str = Field(..., description="Context status")


# ============================================================================
# Dependency Injection
# ============================================================================


async def get_context_service(db: AsyncSession = Depends(get_db)) -> ContextService:
    """Get ContextService instance.

    Args:
        db: Database session

    Returns:
        ContextService instance
    """
    return ContextService(db)


# ============================================================================
# Endpoints
# ============================================================================


@router.get("", response_model=ContextListResponse)
async def list_contexts(
    user: SessionUser,
    service: ContextService = Depends(get_context_service),
    db: AsyncSession = Depends(get_db),
) -> ContextListResponse:
    """List all contexts for current user.

    Returns:
        List of contexts (Issue #246: no current context indicator)
    """
    user_id = user.get("user_id")
    # Issue #246: current_context_id removed

    contexts_list = await service.list_contexts(user_id)

    # Get creator names for all contexts
    creator_ids = [c.created_by for c in contexts_list if c.created_by]
    creator_names = {}
    if creator_ids:
        from models.auth import User

        stmt = select(User).where(User.user_id.in_(creator_ids))
        result = await db.execute(stmt)
        users = result.scalars().all()
        creator_names: dict[str, str] = {u.user_id: u.name for u in users}

    # Issue #217: Get search configs for all contexts (single query)
    context_ids = [c.id for c in contexts_list]
    search_configs: dict[UUID, Any] = {}
    if context_ids:
        from models.config import ContextSearchConfig

        stmt = select(ContextSearchConfig).where(ContextSearchConfig.context_id.in_(context_ids))
        result = await db.execute(stmt)
        configs = result.scalars().all()
        search_configs = {c.context_id: c for c in configs}

    # Calculate member counts for all contexts (single query)
    member_counts: dict[UUID, int] = {}
    if context_ids and user.get("current_workspace_id"):
        from models.auth import WorkspaceMember

        workspace_id = user.get("current_workspace_id")
        # Get all workspace members for this workspace
        workspace_members_stmt = select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id
        )
        workspace_members_result = await db.execute(workspace_members_stmt)
        workspace_members = workspace_members_result.scalars().all()

        # Calculate count for each context
        for context in contexts_list:
            count = 0
            for om in workspace_members:
                if om.role in ("owner", "admin"):
                    # Owners/admins always have access
                    count += 1
                elif om.role in ("member", "viewer"):
                    # Check allowed_context_ids
                    if om.allowed_context_ids is None:
                        # No restriction - has access
                        count += 1
                    elif context.id in om.allowed_context_ids:
                        # Whitelisted - has access
                        count += 1
            member_counts[context.id] = count

    # Issue #187: Batch memory count + last activity (single query, no N+1)
    memory_stats: dict[UUID, tuple[int, datetime | None]] = {}
    if context_ids:
        from models.memory import Memory

        stats_stmt = (
            select(
                Memory.context_id,
                func.count(Memory.id).label("count"),
                func.max(func.coalesce(Memory.updated_at, Memory.created_at)).label(
                    "last_activity"
                ),
            )
            .where(
                Memory.context_id.in_(context_ids),
                Memory.deleted_at.is_(None),
            )
            .group_by(Memory.context_id)
        )
        stats_result = await db.execute(stats_stmt)
        memory_stats = {
            row.context_id: (row.count, row.last_activity) for row in stats_result.all()
        }

    context_responses = []
    for context in contexts_list:
        # Issue #165: Privacy filtering
        # - Private contexts: Only show to creator
        # - Shared contexts: Show to all workspace members
        if context.is_private and context.created_by != user_id:
            continue  # Skip private contexts from other users

        # Shared contexts (is_private=False) are visible to all workspace members
        # Private contexts by current user are visible

        # Issue #217: Get search config for this context
        search_config = search_configs.get(context.id)

        # Issue #187: Memory count and last activity
        ctx_stats = memory_stats.get(context.id, (0, None))

        context_responses.append(
            ContextResponse(
                id=context.id,
                name=context.name,
                display_name=context.display_name,
                description=context.description,
                summary=context.summary,
                usage_guide=context.usage_guide,
                is_default=context.is_default,
                # Issue #246: is_current removed
                is_private=context.is_private,  # Issue #165
                is_public=context.is_public,  # Issue #238
                resource_id=context.resource_id,  # Issue #238
                is_locked=context.is_locked,  # Issue #85
                created_by=context.created_by,  # Issue #165
                created_by_name=creator_names.get(context.created_by)
                if context.created_by
                else None,
                created_at=context.created_at,
                updated_at=context.updated_at,
                # Issue #217: Search config summary
                use_rerank=search_config.use_rerank if search_config else None,
                reranker_provider=search_config.reranker_provider if search_config else None,
                # Issue #49: Embedding model info
                embedding_model=search_config.embedding_model if search_config else None,
                embedding_dimensions=search_config.embedding_dimensions if search_config else None,
                # Member count
                member_count=member_counts.get(context.id, 0) if not context.is_private else None,
                # Issue #187: Memory stats
                memory_count=ctx_stats[0],
                last_activity_at=ctx_stats[1],
            )
        )

    return ContextListResponse(
        contexts=context_responses,
        # Issue #246: current_context_id removed
        total=len(context_responses),
    )


@router.post("", response_model=ContextResponse, status_code=status.HTTP_201_CREATED)
async def create_context(
    request: ContextCreate,
    user: SessionUser,
    service: ContextService = Depends(get_context_service),
) -> ContextResponse:
    """Create a new context.

    Issue #115 Phase B: Contexts are created under user's current workspace.

    Args:
        request: Context creation request

    Returns:
        Created context data

    Raises:
        400: If context name is invalid or already exists
        400: If user has no workspace
    """
    user_id = user.get("user_id")
    # Issue #246: current_context_id removed

    try:
        # Issue #115 Phase B: Get user's current workspace
        # Issue #146: No auto-creation, user must create workspace with OpenAI key first
        workspace_id = user.get("current_workspace_id")
        if not workspace_id:
            raise HTTPException(
                status_code=400, detail="No workspace found. Please create an workspace first."
            )

        # Issue #149: Check if workspace can create another context (Free plan: max 1 context)
        from services.quota_service import QuotaService

        quota_service = QuotaService(service.db)
        can_create, error = await quota_service.check_context_creation_allowed(
            workspace_id, raise_on_denied=True
        )

        # SECURITY: Check plan allows shared contexts
        # Issue #271 Code Review H-1: Use plan_tiers instead of hardcoded plan names
        if not request.is_private:
            from config.plan_tiers import get_plan_tier
            from models.auth import Workspace

            workspace_result = await service.db.execute(
                select(Workspace).where(Workspace.id == workspace_id)
            )
            workspace = workspace_result.scalar_one_or_none()

            if workspace:
                plan = get_plan_tier(workspace.plan_name)
                if not plan.allows_shared_contexts:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Shared contexts require Pro plan. Upgrade your plan to share contexts with your team.",
                    )

        # Validate embedding model if provided
        if request.embedding_model:
            from config.constants import EMBEDDING_MODEL_REGISTRY

            if request.embedding_model not in EMBEDDING_MODEL_REGISTRY:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown embedding model: {request.embedding_model}. "
                    f"Supported: {', '.join(EMBEDDING_MODEL_REGISTRY.keys())}",
                )

        context = await service.create_context(
            workspace_id=workspace_id,
            name=request.name,
            display_name=request.display_name,
            description=request.description,
            summary=request.summary,
            usage_guide=request.usage_guide,
            created_by=user_id,
            embedding_model=request.embedding_model,
            is_private=request.is_private,  # Issue #165: Privacy control
        )

        logger.info(
            "context_created_api",
            user_id=user_id,
            workspace_id=str(workspace_id),
            context_id=str(context.id),
            context_name=request.name,
            is_private=context.is_private,
        )

        # Issue #246: Auto-set current context logic removed

        # Fetch search config for response
        from models.config import ContextSearchConfig

        config_result = await service.db.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == context.id)
        )
        ctx_config = config_result.scalar_one_or_none()

        return ContextResponse(
            id=context.id,
            name=context.name,
            display_name=context.display_name,
            description=context.description,
            summary=context.summary,
            usage_guide=context.usage_guide,
            is_default=context.is_default,
            is_private=context.is_private,
            created_by=context.created_by,
            created_at=context.created_at,
            updated_at=context.updated_at,
            embedding_model=ctx_config.embedding_model if ctx_config else None,
            embedding_dimensions=ctx_config.embedding_dimensions if ctx_config else None,
        )

    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        # Issue #276: Catch IntegrityError for resource_id duplication
        from sqlalchemy.exc import IntegrityError

        if isinstance(e, IntegrityError) and (
            "unique_context_resource_id" in str(e)
            or "unique_workspace" in str(e)
            or "resource_id" in str(e)
        ):
            if request.resource_id and workspace_id:
                await _handle_resource_id_duplicate_error(
                    service.db, request.resource_id, workspace_id
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Resource ID conflict detected."
                ) from e
        raise


# Issue #246: Explicit 404 for deleted /current endpoint
@router.get("/current")
async def get_current_context_removed():
    """Issue #246: Endpoint removed - context must be explicit."""
    raise HTTPException(
        status_code=404, detail="Endpoint removed. Use GET /contexts to list all contexts."
    )


@router.get("/{context_id}", response_model=ContextResponse)
async def get_context(
    context_id: UUID,
    user: SessionUser,
    service: ContextService = Depends(get_context_service),
    db: AsyncSession = Depends(get_db),
) -> ContextResponse:
    """Get a specific context.

    Args:
        context_id: Context UUID

    Returns:
        Context data

    Raises:
        404: If project not found
    """
    user_id = user.get("user_id")
    # Issue #246: current_context_id removed

    try:
        context = await service.get_context(user_id, context_id)

        # Get creator name
        creator_name = None
        if context.created_by:
            from sqlalchemy import select

            from models.auth import User

            creator_result = await db.execute(
                select(User).where(User.user_id == context.created_by)
            )
            creator = creator_result.scalar_one_or_none()
            creator_name = creator.name if creator else None

        return ContextResponse(
            id=context.id,
            name=context.name,
            display_name=context.display_name,
            description=context.description,
            summary=context.summary,
            usage_guide=context.usage_guide,
            is_default=context.is_default,
            is_private=context.is_private,
            is_public=context.is_public,
            resource_id=context.resource_id,
            is_locked=context.is_locked,
            created_by=context.created_by,
            created_by_name=creator_name,
            created_at=context.created_at,
            updated_at=context.updated_at,
        )

    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.get("/{context_id}/stats", response_model=ContextStatsResponse)
async def get_context_stats(
    context_id: UUID,
    user: SessionUser,
    service: ContextService = Depends(get_context_service),
) -> ContextStatsResponse:
    """Get context statistics (memory count, storage).

    Args:
        context_id: Context UUID

    Returns:
        Context statistics

    Raises:
        404: If project not found
    """
    user_id = user.get("user_id")

    try:
        stats = await service.get_context_stats(user_id, context_id)

        return ContextStatsResponse(
            context_id=UUID(stats["context_id"]),
            context_name=stats["context_name"],
            memory_count=stats["memory_count"],
            status=stats["status"],
        )

    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.put("/{context_id}", response_model=ContextResponse)
async def update_context(
    context_id: UUID,
    request: ContextUpdate,
    user: SessionUser,
    service: ContextService = Depends(get_context_service),
    db: AsyncSession = Depends(get_db),
) -> ContextResponse:
    """Update a context's display_name, description and settings.

    Permission (Issue #165 Phase 3):
    - summary/usage_guide/is_private: Requires OWNER access (owner-only fields)
    - display_name/description: Requires EDITOR access

    Args:
        context_id: Context UUID
        request: Update request
        user: Authenticated user
        service: Context service
        db: Database session

    Returns:
        Updated context data

    Raises:
        403: If insufficient permissions
        404: If context not found
    """
    user_id = user.get("user_id")
    # Issue #246: current_context_id removed

    # DEBUG: Log received request data (Issue #238)
    logger.info(
        "update_context_request_received",
        context_id=str(context_id),
        is_public=request.is_public,
        resource_id=request.resource_id,
        is_private=request.is_private,
    )

    # Check if trying to update owner-only fields (Issue #165 Phase 3)
    from services.permission_service import PermissionService

    perm_service = PermissionService(db)
    owner_only_fields = [
        "summary",
        "usage_guide",
        "is_private",
        "is_public",
        "resource_id",
        "is_locked",
    ]  # Issue #238: is_public, resource_id; Issue #85: is_locked — owner-only
    is_updating_owner_fields = any(
        getattr(request, field, None) is not None for field in owner_only_fields
    )

    # Issue #272 M-3: Reuse context from permission check to avoid redundant DB query
    if is_updating_owner_fields:
        # Requires owner access
        existing_context = await perm_service.check_context_owner(user_id, context_id)
    else:
        # Editor can update description
        existing_context, _ = await perm_service.check_context_access(
            user_id, context_id, required_role="editor"
        )

    try:
        from sqlalchemy import select

        # Context already retrieved by permission check (no second query needed)

        # Save workspace_id for later use (before potential rollback)
        existing_workspace_id = existing_context.workspace_id if existing_context else None

        # Prevent Public→Private change if has resource_id (Issue #242)
        if request.is_private is not None:
            if existing_context and existing_context.resource_id and request.is_private:
                raise ValidationError(
                    "Cannot change to private: This context has a resource_id and is used by Resource Tokens. "
                    "Please revoke all tokens first or remove resource_id."
                )

            # SECURITY: Check plan allows shared contexts
            # Issue #271 Code Review H-1: Use plan_tiers instead of hardcoded plan names
            if not request.is_private and existing_context:
                from config.plan_tiers import get_plan_tier
                from models.auth import Workspace

                workspace = await db.get(Workspace, existing_context.workspace_id)

                if workspace:
                    plan = get_plan_tier(workspace.plan_name)
                    if not plan.allows_shared_contexts:
                        raise ValidationError(
                            "Shared contexts require Pro plan. Upgrade your plan to share contexts with your team."
                        )

        # SECURITY: Validate resource_id uniqueness within workspace
        # Issue #271 Code Review H-2: Rely on DB constraint instead of application-level check
        # Migration 055 created: UNIQUE INDEX unique_context_resource_id_per_workspace
        # Just let the constraint enforce uniqueness (simpler and no race condition)

        if request.resource_id is not None:
            old_resource_id = existing_context.resource_id if existing_context else None

            # If changing resource_id (not just setting it initially), revoke old tokens
            # ONLY revoke tokens created by current user (fix ownership bypass)
            if old_resource_id and old_resource_id != request.resource_id:
                from sqlalchemy import select

                from auth.resource_tokens import ResourceTokenManager
                from models.resource import ResourceToken

                token_manager = ResourceTokenManager(db)

                # Get tokens for old resource_id created by CURRENT USER only
                user_tokens_result = await db.execute(
                    select(ResourceToken).where(
                        and_(
                            ResourceToken.resource_id == old_resource_id,
                            ResourceToken.created_by == user_id,
                            ResourceToken.is_active == True,  # noqa: E712
                        )
                    )
                )
                old_tokens = list(user_tokens_result.scalars().all())

                if old_tokens:
                    for token in old_tokens:
                        await token_manager.revoke_token(token.id)

                    logger.info(
                        "auto_revoked_tokens_on_resource_id_change",
                        context_id=str(context_id),
                        old_resource_id=old_resource_id,
                        new_resource_id=request.resource_id,
                        count=len(old_tokens),
                    )

        context = await service.update_context(
            user_id=user_id,
            context_id=context_id,
            display_name=request.display_name,
            description=request.description,
            summary=request.summary,
            usage_guide=request.usage_guide,
            is_private=request.is_private,  # Migration 034
            is_public=request.is_public,  # Issue #238
            resource_id=request.resource_id,  # Issue #238
            is_locked=request.is_locked,  # Issue #85
        )

        logger.info(
            "context_updated_api",
            user_id=user_id,
            context_id=str(context_id),
        )

        return ContextResponse(
            id=context.id,
            name=context.name,
            display_name=context.display_name,
            description=context.description,
            summary=context.summary,
            usage_guide=context.usage_guide,
            is_default=context.is_default,
            # Issue #246: is_current removed
            is_locked=context.is_locked,
            is_private=context.is_private,
            is_public=context.is_public,
            resource_id=context.resource_id,
            created_by=context.created_by,
            created_at=context.created_at,
            updated_at=context.updated_at,
        )

    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        # Issue #271 Code Review H-2: Catch IntegrityError for resource_id duplication
        from sqlalchemy.exc import IntegrityError

        if isinstance(e, IntegrityError) and (
            "unique_context_resource_id" in str(e)
            or "unique_workspace" in str(e)
            or "resource_id" in str(e)
        ):
            if existing_workspace_id and request.resource_id:
                await _handle_resource_id_duplicate_error(
                    db, request.resource_id, existing_workspace_id, exclude_context_id=context_id
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Resource ID conflict detected."
                ) from e
        raise


# Issue #246: PUT /{context_id}/activate endpoint removed (context always explicit)


@router.delete("/{context_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_context(
    context_id: UUID,
    user: SessionUser,
    service: ContextService = Depends(get_context_service),
    db: AsyncSession = Depends(get_db),
):
    """Delete a context and its collection.

    Only context owner can delete their context.

    Args:
        context_id: Context UUID to delete

    Raises:
        400: If trying to delete default context
        403: If not context owner
        404: If context not found
    """
    user_id = user.get("user_id")
    # Issue #246: current_context_id removed (deletion check removed)

    try:
        # Check if user is context owner (creator only can delete)
        from services.permission_service import PermissionService

        perm_service = PermissionService(db)
        # Issue #272 M-3: Reuse context from permission check to avoid redundant DB query
        context = await perm_service.check_context_owner(user_id, context_id)

        # Issue #85: Block deletion if context is locked
        if context.is_locked:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Context is locked. Unlock it before deleting.",
            )

        # Auto-revoke related resource tokens (Issue #242)

        from auth.resource_tokens import ResourceTokenManager

        # Context already retrieved by permission check (no second query needed)

        # Note: context is guaranteed to exist (check_context_owner ensures it)
        if context.resource_id:
            token_manager = ResourceTokenManager(db)
            tokens = await token_manager.list_tokens(resource_id=context.resource_id)

            for token in tokens:
                await token_manager.revoke_token(token.id)

            if tokens:
                logger.info(
                    "auto_revoked_resource_tokens",
                    context_id=str(context_id),
                    resource_id=context.resource_id,
                    count=len(tokens),
                )

        await service.delete_context(user_id, context_id)

        logger.info(
            "context_deleted_api",
            user_id=user_id,
            context_id=str(context_id),
        )

    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    except Exception as e:
        from utils.exceptions import ConflictError

        if isinstance(e, ConflictError):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
        raise


# ============================================================================
# Context Member Management (Issue #115 Phase B-3)
# ============================================================================


class ContextMemberResponse(BaseModel):
    """Response model for context member."""

    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    role: str
    added_at: str | None = None  # None for workspace owners/admins with automatic access
    is_workspace_admin: bool = False  # True if access is via workspace role (owner/admin)

    class Config:
        from_attributes = True


class AddContextMemberRequest(BaseModel):
    """Request model for adding context member."""

    user_id: str = Field(..., min_length=1)
    role: str = Field(..., pattern=r"^(owner|editor|viewer)$")


class UpdateContextMemberRoleRequest(BaseModel):
    """Request model for updating context member role."""

    role: str = Field(..., pattern=r"^(owner|editor|viewer)$")


@router.get("/{context_id}/members", response_model=list[ContextMemberResponse])
async def list_context_members(
    context_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List all members who have access to context.

    Returns both explicit context members and workspace owners/admins
    (who have automatic access).

    Requires viewer access to context.
    """
    from models.auth import ContextMember, WorkspaceMember
    from services.permission_service import PermissionService

    user = await get_current_user(request)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #271 Code Review C-2: check_context_access already verifies workspace membership
    # Issue #272 M-3: Reuse context from check_context_access to avoid redundant DB query
    context, _ = await perm_service.check_context_access(
        user["user_id"], context_id, required_role="viewer"
    )

    # Get context for member list query
    from sqlalchemy import select

    # Context already retrieved by check_context_access (no second query needed)

    # Get explicit context members
    stmt = (
        select(ContextMember)
        .where(ContextMember.context_id == context_id)
        .order_by(ContextMember.role.desc(), ContextMember.created_at)
    )
    result = await db.execute(stmt)
    explicit_members = result.scalars().all()

    # Get all workspace members who have access to this context
    from models.auth import User

    # 1. Owners/Admins (automatic full access)
    # 2. Viewers with allowed_context_ids that includes this context (or null)
    # 3. Members with allowed_context_ids that includes this context (or null)
    org_stmt = select(WorkspaceMember).where(WorkspaceMember.workspace_id == context.workspace_id)
    workspace_result = await db.execute(org_stmt)
    all_workspace_members = workspace_result.scalars().all()

    # Filter members who have access to this context
    accessible_members = []
    for om in all_workspace_members:
        if om.role in ("owner", "admin"):
            # Owners/admins always have access
            accessible_members.append(om)
        elif om.role in ("member", "viewer"):
            # Check allowed_context_ids
            if om.allowed_context_ids is None:
                # No restriction - has access
                accessible_members.append(om)
            elif context_id in om.allowed_context_ids:
                # Whitelisted - has access
                accessible_members.append(om)

    # Get user info for all accessible members
    all_user_ids = list(
        {om.user_id for om in accessible_members} | {m.user_id for m in explicit_members}
    )
    user_stmt = select(User).where(User.user_id.in_(all_user_ids))
    user_result = await db.execute(user_stmt)
    users = {u.user_id: u for u in user_result.scalars().all()}

    # Build response
    response = []

    # Add all accessible workspace members
    for om in accessible_members:
        user_info = users.get(om.user_id)
        response.append(
            ContextMemberResponse(
                user_id=om.user_id,
                user_name=user_info.name if user_info else None,
                user_email=user_info.email if user_info else None,
                role=om.role,
                added_at=om.joined_at.isoformat() if om.joined_at else None,
                is_workspace_admin=om.role in ("owner", "admin"),
            )
        )

    return response


@router.post(
    "/{context_id}/members",
    response_model=ContextMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_context_member(
    context_id: UUID,
    body: AddContextMemberRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Add a member to context.

    Requires owner access to context (or workspace admin/owner).
    """
    from sqlalchemy import func, select

    from models.auth import ContextMember
    from services.permission_service import PermissionService

    user = await get_current_user(request)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #271 Code Review C-2: check_context_owner already verifies ownership
    # Issue #272 M-3: Reuse context from check_context_owner to avoid redundant DB query
    context = await perm_service.check_context_owner(user["user_id"], context_id)

    # Context already retrieved by check_context_owner (no second query needed)

    # Issue #234: Private contexts cannot have members added
    # Note: context is guaranteed to exist (check_context_owner ensures it)
    if context.is_private:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot add members to a private context. Change to Shared first.",
        )

    # Check if member already exists

    stmt = select(ContextMember).where(
        ContextMember.context_id == context_id,
        ContextMember.user_id == body.user_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"User {body.user_id} is already a context member",
        )

    # Create membership
    member = ContextMember(
        context_id=context_id,
        user_id=body.user_id,
        role=body.role,
        invited_by=user["user_id"],
        invited_at=func.now(),
    )

    db.add(member)
    await db.commit()
    await db.refresh(member)

    logger.info(f"Added context member: {body.user_id} to context {context_id}")

    return ContextMemberResponse(
        user_id=member.user_id,
        role=member.role,
        added_at=member.created_at.isoformat(),
    )


@router.put("/{context_id}/members/{user_id}", response_model=ContextMemberResponse)
async def update_context_member_role(
    context_id: UUID,
    user_id: str,
    body: UpdateContextMemberRoleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update context member's role.

    Requires owner access to context (or workspace admin/owner).
    """
    from sqlalchemy import func, select

    from models.auth import ContextMember
    from services.permission_service import PermissionService

    current_user = await get_current_user(request)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #271 Code Review C-2: check_context_owner already verifies ownership
    await perm_service.check_context_owner(current_user["user_id"], context_id)

    # Get member
    stmt = select(ContextMember).where(
        ContextMember.context_id == context_id,
        ContextMember.user_id == user_id,
    )
    result = await db.execute(stmt)
    member = result.scalar_one_or_none()

    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Member {user_id} not found in context",
        )

    # Update role
    member.role = body.role
    member.updated_at = func.now()

    await db.commit()
    await db.refresh(member)

    logger.info(f"Updated context member role: {user_id} -> {body.role}")

    return ContextMemberResponse(
        user_id=member.user_id,
        role=member.role,
        added_at=member.created_at.isoformat(),
    )


@router.delete(
    "/{context_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def remove_context_member(
    context_id: UUID,
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Remove member from context.

    Requires owner access to context (or workspace admin/owner).
    Cannot remove context owner.
    """
    from sqlalchemy import select

    from models.auth import ContextMember
    from services.permission_service import PermissionService

    current_user = await get_current_user(request)
    perm_service = PermissionService(db)

    # SECURITY: Workspace boundary check
    # Issue #271 Code Review C-2: check_context_owner already verifies ownership
    await perm_service.check_context_owner(current_user["user_id"], context_id)

    # Get member
    stmt = select(ContextMember).where(
        ContextMember.context_id == context_id,
        ContextMember.user_id == user_id,
    )
    result = await db.execute(stmt)
    member = result.scalar_one_or_none()

    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Member {user_id} not found in context",
        )

    # Cannot remove owner
    if member.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove context owner",
        )

    # Delete member
    await db.delete(member)
    await db.commit()

    logger.info(f"Removed context member: {user_id} from context {context_id}")


# ============================================================================
# Memory Usage Stats & Duplicate Detection (Issue #83)
# ============================================================================


class MemoryStatItem(BaseModel):
    """Per-memory usage statistics."""

    id: str
    summary: str
    type: str
    importance: float
    scope: str
    use_count: int
    access_count: int
    last_used_at: str | None
    embedding_status: str
    created_at: str


class MemoryUsageStatsResponse(BaseModel):
    """Response for per-memory stats endpoint."""

    memories: list[MemoryStatItem]
    total: int
    sort_by: str
    sort_order: str


VALID_SORT_FIELDS = {"use_count", "access_count", "importance", "created_at", "last_used_at"}
VALID_SORT_ORDERS = {"asc", "desc"}


@router.get("/{context_id}/memory-stats", response_model=MemoryUsageStatsResponse)
async def get_memory_usage_stats(
    context_id: UUID,
    user: APIKeyOrSessionUser,
    sort_by: str = Query("use_count", description="Sort field"),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> MemoryUsageStatsResponse:
    """Get per-memory recall statistics for a context.

    Issue #83: Memory usage stats for context cleanup workflows.
    Supports sorting by use_count, access_count, importance, created_at, last_used_at.
    """
    from sqlalchemy import asc as sa_asc
    from sqlalchemy import desc as sa_desc
    from sqlalchemy import func, select

    from models.memory import Memory
    from services.permission_service import PermissionService

    perm_service = PermissionService(db)
    await perm_service.check_context_access(user["user_id"], context_id)

    if sort_by not in VALID_SORT_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort_by. Must be one of: {', '.join(sorted(VALID_SORT_FIELDS))}",
        )
    if sort_order not in VALID_SORT_ORDERS:
        raise HTTPException(status_code=400, detail="Invalid sort_order. Must be 'asc' or 'desc'")

    sort_col = getattr(Memory, sort_by)
    order_fn = sa_desc if sort_order == "desc" else sa_asc

    # Total count
    count_stmt = select(func.count(Memory.id)).where(
        Memory.context_id == context_id, Memory.deleted_at.is_(None)
    )
    total = (await db.execute(count_stmt)).scalar() or 0

    # Paginated query
    stmt = (
        select(Memory)
        .where(Memory.context_id == context_id, Memory.deleted_at.is_(None))
        .order_by(order_fn(sort_col).nulls_last())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    memories = result.scalars().all()

    items = [
        MemoryStatItem(
            id=str(m.id),
            summary=m.summary[:200] if m.summary else "",
            type=m.type or "note",
            importance=float(m.importance) if m.importance is not None else 0.5,
            scope=m.scope or "persistent",
            use_count=m.use_count or 0,
            access_count=m.access_count or 0,
            last_used_at=m.last_used_at.isoformat() if m.last_used_at else None,
            embedding_status=m.embedding_status or "pending",
            created_at=m.created_at.isoformat() if m.created_at else "",
        )
        for m in memories
    ]

    return MemoryUsageStatsResponse(
        memories=items,
        total=total,
        sort_by=sort_by,
        sort_order=sort_order,
    )


class DuplicateMemoryInfo(BaseModel):
    """Memory info for duplicate pair display."""

    id: str
    summary: str
    type: str
    created_at: str


class DuplicatePair(BaseModel):
    """A pair of similar memories."""

    memory_a: DuplicateMemoryInfo
    memory_b: DuplicateMemoryInfo
    similarity: float


class DuplicatesResponse(BaseModel):
    """Response for duplicate detection endpoint."""

    pairs: list[DuplicatePair]
    total_pairs: int
    threshold: float
    memories_scanned: int


@router.get("/{context_id}/duplicates", response_model=DuplicatesResponse)
async def find_duplicates(
    context_id: UUID,
    user: APIKeyOrSessionUser,
    threshold: float = Query(0.90, ge=0.5, le=1.0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> DuplicatesResponse:
    """Find duplicate memory pairs using Qdrant vector similarity.

    Issue #83: Duplicate detection for context cleanup.
    Scans recent memories (max 200) and finds pairs above similarity threshold.
    """
    from sqlalchemy import select

    from db.qdrant import KAGURA_MEMORIES_COLLECTION, get_qdrant_client, search_memories_qdrant
    from models.memory import Memory
    from services.permission_service import PermissionService

    perm_service = PermissionService(db)
    await perm_service.check_context_access(user["user_id"], context_id)

    collection_name = KAGURA_MEMORIES_COLLECTION

    # Fetch recent memories (cap at 200 for performance)
    mem_stmt = (
        select(Memory)
        .where(
            Memory.context_id == context_id,
            Memory.deleted_at.is_(None),
            Memory.embedding_status == "success",
        )
        .order_by(Memory.created_at.desc())
        .limit(200)
    )
    mem_result = await db.execute(mem_stmt)
    memories = list(mem_result.scalars().all())

    if not memories:
        return DuplicatesResponse(pairs=[], total_pairs=0, threshold=threshold, memories_scanned=0)

    # Build ID→Memory lookup
    mem_map = {m.id: m for m in memories}
    memory_ids = set(mem_map.keys())

    # Retrieve existing vectors from Qdrant (no re-embedding needed)
    client = get_qdrant_client()
    point_ids = [str(m.id) for m in memories]
    vectors: dict[UUID, list[float]] = {}
    batch_size = 100
    for i in range(0, len(point_ids), batch_size):
        batch = point_ids[i : i + batch_size]
        points = await client.retrieve(
            collection_name=collection_name,
            ids=batch,
            with_vectors=True,
            with_payload=False,
        )
        for point in points:
            vec = point.vector
            if isinstance(vec, dict):
                vec = vec.get("dense", [])
            if vec:
                vectors[UUID(str(point.id))] = vec

    # Find similar pairs using retrieved vectors
    pairs: list[DuplicatePair] = []
    seen: set[tuple[UUID, UUID]] = set()
    workspace_id = user.get("current_workspace_id", "")

    for memory in memories:
        if len(pairs) >= limit:
            break
        vector = vectors.get(memory.id)
        if not vector:
            continue
        try:
            results = await search_memories_qdrant(
                user_id=user["user_id"],
                query_vector=vector,
                workspace_id=str(workspace_id),
                context_id=str(context_id),
                limit=5,
                collection_name=collection_name,
            )
            for hit in results:
                if hit["score"] < threshold:
                    continue
                hit_id = UUID(str(hit["id"]))
                if hit_id == memory.id or hit_id not in memory_ids:
                    continue
                a, b = sorted([memory.id, hit_id], key=str)
                pair_key = (a, b)
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                mem_a = mem_map[a]
                mem_b = mem_map[b]
                pairs.append(
                    DuplicatePair(
                        memory_a=DuplicateMemoryInfo(
                            id=str(a),
                            summary=mem_a.summary[:200] if mem_a.summary else "",
                            type=mem_a.type or "note",
                            created_at=mem_a.created_at.isoformat() if mem_a.created_at else "",
                        ),
                        memory_b=DuplicateMemoryInfo(
                            id=str(b),
                            summary=mem_b.summary[:200] if mem_b.summary else "",
                            type=mem_b.type or "note",
                            created_at=mem_b.created_at.isoformat() if mem_b.created_at else "",
                        ),
                        similarity=hit["score"],
                    )
                )
        except Exception as e:
            logger.warning("duplicate_scan_error", memory_id=str(memory.id), error=str(e))

    returned_pairs = pairs[:limit]
    return DuplicatesResponse(
        pairs=returned_pairs,
        total_pairs=len(returned_pairs),
        threshold=threshold,
        memories_scanned=len(memories),
    )
