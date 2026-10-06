"""Resource Token Management API routes.

Issue #242: Resource Token Management UI - Backend API endpoints.

Provides CRUD operations for resource tokens with owner-only access control.
Pattern: Based on api_keys.py with resource-specific adaptations.

Population (#1919): every limit and every lookup here is the WORKSPACE's.
``max_resource_tokens`` is a field of the workspace's plan, #1863 made the
list / update / revoke routes workspace-scoped (an owner manages every token
of the workspace, whoever minted it), and the downgrade-eligibility read
counts per workspace — so the create-time count cap and the quota-raise
ceiling (``max_resource_tokens * 10000`` events/hour) are summed over the
same set: the workspace's active, non-connector tokens, attributed by
:func:`_workspace_tokens`. Because every token is at most 10000 events/hour
(the request model's bound), a workspace within its count cap is within
its ceiling, so creation never checks the sum.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from auth.resource_tokens import ResourceTokenManager
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.resource import Resource, ResourceToken, WorkspaceConnector
from services.resource_lookup import resolve_resource_pk
from utils.exceptions import (
    FeatureNotAvailableError,
    MemoryCloudException,
    QuotaExceededError,
)
from utils.logger import get_logger
from utils.public_id import PublicIdPrefix, ResourceTokenPublicId, public_id_pattern

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

    id: str = Field(
        ...,
        description="Public id of the token (`rtok_` + 22 base62 characters)",
        pattern=public_id_pattern(PublicIdPrefix.RESOURCE_TOKEN),
    )
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
        id=token.public_id,
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


def _workspace_tokens(workspace_id: UUID, *columns: Any) -> Select[*tuple[Any, ...]]:
    """``SELECT columns`` over the resource tokens of ``workspace_id`` (#268, #1863, #1919).

    "The workspace's" is judged by the ``resources`` row a token's
    ``resource_pk`` points at — not by a live ``contexts`` row (a token keeps
    authenticating ingest after its last context is soft-deleted, since
    ``verify_token`` joins ``Resource`` by ``resource_pk`` and never looks at
    contexts) and not by the token's shadow ``workspace_id`` column, which was
    never backfilled on some rows. A legacy token with ``resource_pk IS NULL``
    (it cannot authenticate — ``verify_token`` rejects it — but is listed so
    the owner can revoke it) falls back to its own ``workspace_id``. This is
    the single-token form of :func:`auth.resource_tokens.resource_token_scope`.

    Every lookup, count and sum in this module goes through this builder, so
    a token that one of them sees is a token all of them see: a token that
    counts toward the cap or the ceiling can always be fetched, updated and
    revoked (#1919), and a token of another workspace is a uniform miss.
    """
    return (
        select(*columns)
        .outerjoin(Resource, Resource.id == ResourceToken.resource_pk)
        .where(
            or_(
                Resource.workspace_id == workspace_id,
                and_(
                    ResourceToken.resource_pk.is_(None),
                    ResourceToken.workspace_id == workspace_id,
                ),
            )
        )
    )


def _workspace_regular_active_tokens(workspace_id: UUID, *columns: Any) -> Select[*tuple[Any, ...]]:
    """The population of the token cap and the quota ceiling (#858, #1919).

    :func:`_workspace_tokens` minus revoked tokens and minus connector-owned
    ones: the connector setup flow mints a resource token that bypasses the
    ``max_resource_tokens`` gate on purpose (connectors are gated by
    ``max_connectors`` seats), so counting it here would let it eat a regular
    slot post-mint and prematurely refuse a legitimate creation. The anti-join
    against ``workspace_connectors`` (UNIQUE ``resource_pk``, so no row
    inflation) drops exactly the connector-owned tokens; a regular token with
    a NULL ``resource_pk`` never matches the join and is still counted.
    """
    return (
        _workspace_tokens(workspace_id, *columns)
        .outerjoin(
            WorkspaceConnector,
            WorkspaceConnector.resource_pk == ResourceToken.resource_pk,
        )
        .where(
            ResourceToken.is_active == True,  # noqa: E712
            WorkspaceConnector.id.is_(None),
        )
    )


async def _resolve_workspace_token(
    db: AsyncSession, token_id: str, workspace_id: UUID
) -> ResourceToken | None:
    """The token ``token_id`` names, if it belongs to ``workspace_id`` (#1863, #1919).

    Same predicate as the cap and the ceiling (:func:`_workspace_tokens`), so
    a token with ``resource_pk`` set but a NULL shadow ``workspace_id`` — one
    the ceiling counts — is addressable. ``None`` for an unknown id and for
    another workspace's token alike: the route answers a uniform 404 so the
    token's existence is not disclosed.
    """
    result = await db.execute(
        _workspace_tokens(workspace_id, ResourceToken).where(ResourceToken.public_id == token_id)
    )
    return result.scalar_one_or_none()


async def _check_workspace_quota_ceiling(
    db: AsyncSession, token: ResourceToken, workspace_id: UUID, new_quota: int
) -> None:
    """Refuse a quota raise that takes the workspace over its plan ceiling (#1877).

    The ceiling is ``max_resource_tokens * 10000`` events/hour and the budget
    is the WORKSPACE's: every other active token in it counts, whoever minted
    it — the same population the create-time count cap is taken over
    (:func:`_workspace_regular_active_tokens`, #1919). The sum used to be over
    ``created_by == caller``, and the cap per creator, so two owners could
    mint their way over the ceiling and then no raise fitted.

    Connector-owned tokens are outside this budget, consistent with the
    create-time cap (#858): a connector is gated by ``max_connectors`` seats
    and its token never takes a ``max_resource_tokens`` slot, so it neither
    counts toward the sum nor is checked against it when it is the target
    (its quota stays bounded by the per-token maximum of the request model).

    Raises:
        HTTPException: 400 when the raise does not fit.
    """
    from config.plan_tiers import get_plan_tier
    from models.auth import Workspace

    if token.resource_pk is not None:
        connector_result = await db.execute(
            select(WorkspaceConnector.id).where(WorkspaceConnector.resource_pk == token.resource_pk)
        )
        if connector_result.scalar_one_or_none() is not None:
            return

    workspace_result = await db.execute(
        select(Workspace.plan_name).where(Workspace.id == workspace_id)
    )
    plan_name = workspace_result.scalar_one_or_none()
    if not plan_name:
        return

    max_total_quota = get_plan_tier(plan_name).max_resource_tokens * 10000

    # Quota used by the workspace's OTHER regular tokens.
    other_tokens_result = await db.execute(
        _workspace_regular_active_tokens(
            workspace_id, func.sum(ResourceToken.quota_events_per_hour)
        ).where(ResourceToken.id != token.id)
    )
    used_by_others = other_tokens_result.scalar() or 0

    if used_by_others + new_quota > max_total_quota:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Quota limit exceeded. Total quota (including this update) would be {used_by_others + new_quota}, but plan allows {max_total_quota}.",
        )


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
        # Issue #268/#270: Verify resource belongs to owner's workspace.
        # #1863: resolved through the ``resources`` row, not a live context,
        # so the tokens of a resource whose contexts were deleted stay listed.
        resource_pk: UUID | None = None
        if resource_id is not None:
            resource_pk = await resolve_resource_pk(db, current_workspace_id, resource_id)

            if resource_pk is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Resource ID '{resource_id}' not found in your workspace or you don't have access to it.",
                )

        # Performance: DB-level filtering and pagination (not in-memory).
        # #1863: the list is the workspace's — every token in it, whoever
        # minted it (the owner must be able to see and revoke all of them) —
        # and the slug filter is pinned to the resolved ``resource_pk`` so a
        # same-slug resource in another workspace never shows up here.
        total = await manager.count_tokens(
            include_revoked=True,
            workspace_id=current_workspace_id,
            resource_pk=resource_pk,
        )

        tokens = await manager.list_tokens(
            include_revoked=True,
            limit=limit,
            offset=offset,
            workspace_id=current_workspace_id,
            resource_pk=resource_pk,
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

    The token cap is the WORKSPACE's (#1919): the count of active,
    non-connector tokens of the workspace — whoever minted them — must be
    under the plan's ``max_resource_tokens``. It used to be counted per
    creator, while the quota ceiling on PATCH was summed per workspace, so
    two owners could each mint a full set and leave the workspace with no
    quota raise that fitted. The population is the ceiling's
    (:func:`_workspace_regular_active_tokens`); the sum itself is not checked
    here because count <= cap and quota <= 10000 per token already keep it
    at or under ``max_resource_tokens * 10000``.

    Args:
        data: Token creation request
        owner: Workspace owner (user_id, workspace_id) from dependency
        manager: ResourceTokenManager instance
        db: Database session

    Returns:
        Token metadata + plaintext token (shown ONLY once)

    Raises:
        400: Invalid resource_id
        403: Not workspace owner, plan without the feature, or the workspace
            is at its token cap (``QUOTA-001``)
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
        from config.plan_tiers import (
            get_plan_tier,
            has_feature,
            lowest_tier_with_limit,
            plan_display_name,
            quota_gate_details,
        )
        from models.auth import Context, Workspace

        # SECURITY: Verify the resource belongs to current workspace
        # Issue #268: Workspace boundary violation prevention. #1863: judged by
        # the ``resources`` row like list/update/revoke; minting additionally
        # needs a live context — a retired resource gets no new credentials —
        # and the message says which of the two is missing (the resource may
        # well be listed, its existing tokens being reachable while active).
        resource_pk = await resolve_resource_pk(db, workspace_id, data.resource_id)
        if resource_pk is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Resource ID '{data.resource_id}' not found in your workspace or you don't have access to it.",
            )
        context_result = await db.execute(
            select(Context.id).where(
                and_(
                    Context.resource_id == data.resource_id,
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                )
            )
        )
        if context_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Resource ID '{data.resource_id}' has no live context in your "
                    "workspace; restore or create a context bound to it before creating "
                    "tokens."
                ),
            )

        # ``WorkspaceOwner`` guarantees a workspace id; a missing plan row is a
        # data gap that must fail CLOSED (no plan → no feature), not skip the
        # gate the way the old ``if workspace_id: … if plan_name:`` nesting did.
        workspace_result = await db.execute(
            select(Workspace.plan_name).where(Workspace.id == workspace_id)
        )
        plan_name = workspace_result.scalar_one_or_none()

        # Issue #1551: "may create" is the feature flag (XL-only), NOT
        # ``max_resource_tokens == 0`` — M/L keep a positive cap so the
        # tokens they already hold stay editable and served. The count
        # check below remains the second gate for tiers with the feature.
        if not has_feature(plan_name or "", "resources"):
            raise FeatureNotAvailableError.for_feature(plan_name, "resources")
        plan = get_plan_tier(plan_name)

        # Check the workspace's active token count against the cap (#858
        # excludes connector-owned tokens, #1919 counts the workspace, not the
        # caller — see ``_workspace_regular_active_tokens``).
        # Note: Race condition possible but low impact (concurrent creation rare)
        # Alternative: Use database constraint on token count (future improvement)
        active_count_result = await db.execute(
            _workspace_regular_active_tokens(workspace_id, func.count(ResourceToken.id))
        )
        active_count = active_count_result.scalar() or 0

        if active_count >= plan.max_resource_tokens:
            # #1644 S5: the cap keeps its 403 — the status is what existing
            # clients branch on — but answers the documented ``QUOTA-001``
            # envelope instead of the non-semantic ``HTTP-403`` placeholder.
            # ``plan_name.upper()`` rendered a third tier vocabulary
            # ("PROMAX") beside the keys and the display names.
            raise QuotaExceededError(
                (
                    f"Token limit reached. Your {plan_display_name(plan_name)} plan allows "
                    f"{plan.max_resource_tokens} active tokens. "
                    "Please revoke unused tokens or upgrade your plan."
                ),
                status_code=status.HTTP_403_FORBIDDEN,
                **quota_gate_details(
                    plan_name,
                    "resource_tokens",
                    current=active_count,
                    limit=plan.max_resource_tokens,
                    required_plan=lowest_tier_with_limit(
                        "max_resource_tokens", plan.max_resource_tokens
                    ),
                    feature="resources",
                ),
            )

        # Issue #390 Phase 2: ``resource_pk`` (resolved above) + ``workspace_id``
        # make the ResourceToken insert satisfy the before_insert event
        # listener invariant (models/resource.py).
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
    except (HTTPException, MemoryCloudException):
        # Re-raise HTTP / canonical exceptions (plan check, etc.) without wrapping
        raise
    except Exception as e:
        logger.error("create_resource_token_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create resource token",
        ) from e


@router.patch("/{token_id}", response_model=ResourceTokenResponse)
async def update_resource_token(
    token_id: ResourceTokenPublicId,
    request: ResourceTokenUpdate,
    owner: WorkspaceOwner,
    db: AsyncSession = Depends(get_db),
) -> ResourceTokenResponse:
    """Update resource token description and/or quota.

    Issue #242: Allow updating token metadata without regenerating.
    Issue #276: Uses WorkspaceOwner dependency for DRY principle.

    The token is the WORKSPACE's to update (#1863): any owner may relabel any
    token of the workspace, whoever minted it. It is resolved with the same
    predicate the quota ceiling sums over (#1919, :func:`_workspace_tokens` —
    the ``resources`` row its ``resource_pk`` points at, or its own
    ``workspace_id`` for a legacy row without one), so a token the ceiling
    counts is always addressable; it used to be matched on the shadow
    ``workspace_id`` column alone and was a 404 when that was never
    backfilled. A quota raise is checked against the workspace's ceiling,
    ``max_resource_tokens * 10000`` events/hour over the workspace's active
    non-connector tokens; a lowering is never refused.

    Args:
        token_id: Public id of the token (``rtok_...``)
        request: Update request
        owner: Workspace owner (user_id, workspace_id) from dependency
        db: Database session

    Returns:
        Updated token metadata

    Raises:
        400: The quota raise does not fit the workspace's ceiling
        403: Not workspace owner
        404: Token not found (or not this workspace's — the same answer)
    """
    try:
        user_id, current_workspace_id = owner

        # SECURITY: the token must belong to the caller's workspace (#268,
        # #1863, #1919: judged by the ``resources`` row, not a live context
        # and not the shadow column; a token of another workspace is a
        # uniform 404 so its existence is not disclosed)
        token = await _resolve_workspace_token(db, token_id, current_workspace_id)

        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Resource token not found",
            )

        # Validate new quota doesn't exceed plan limits (Code review M-10)
        # #1877: only a RAISE can breach the ceiling, so a lowering (or an
        # unchanged value) is never checked — it used to be refused with 400
        # whenever the sum was already over the ceiling.
        if (
            request.quota_events_per_hour is not None
            and request.quota_events_per_hour > token.quota_events_per_hour
        ):
            await _check_workspace_quota_ceiling(
                db, token, current_workspace_id, request.quota_events_per_hour
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
            token_id=token.id,
            public_id=token_id,
            user_id=user_id,
        )

        return _format_token_response(token)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("update_resource_token_failed", error=str(e), public_id=token_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update resource token",
        ) from e


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoke_resource_token(
    token_id: ResourceTokenPublicId,
    owner: WorkspaceOwner,
    manager: ResourceTokenManager = Depends(get_resource_token_manager),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a resource token (soft delete).

    Issue #242: Owner-only access. Sets is_active=False (preserves audit trail).
    Issue #276: Uses WorkspaceOwner dependency for DRY principle.

    The token is the WORKSPACE's to revoke (#1863): any owner may revoke any
    token of the workspace, including one minted by a departed member or a
    connector. It is resolved like the update route and the quota ceiling
    (#1919, :func:`_workspace_tokens`): by the ``resources`` row its
    ``resource_pk`` points at — so a token whose contexts were deleted, or
    whose shadow ``workspace_id`` was never backfilled, can still be revoked
    while it still authenticates ingest.

    Args:
        token_id: Public id of the token to revoke (``rtok_...``)
        owner: Workspace owner (user_id, workspace_id) from dependency
        manager: ResourceTokenManager instance
        db: Database session

    Raises:
        403: Not workspace owner
        404: Token not found (or not this workspace's — the same answer)
        500: Failed to revoke token
    """
    try:
        user_id, current_workspace_id = owner
        logger.info("revoke_resource_token_request", user_id=user_id, public_id=token_id)

        # SECURITY: the token must belong to the caller's workspace (#268,
        # #1863, #1919: judged by the ``resources`` row, not a live context
        # and not the shadow column; a token of another workspace is a
        # uniform 404 so its existence is not disclosed)
        target_token = await _resolve_workspace_token(db, token_id, current_workspace_id)

        if not target_token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Resource token not found",
            )

        # Revoke token (soft delete)
        await manager.revoke_token(target_token.id)
        await db.commit()

        logger.info(
            "resource_token_revoked",
            user_id=user_id,
            token_id=target_token.id,
            public_id=token_id,
            resource_id=target_token.resource_id,
        )

    except HTTPException:
        raise
    except ValueError as e:
        # The manager's message names the integer PK — keep it in the log
        # and send the same uniform detail as the lookup miss above (#1008).
        logger.warning("revoke_resource_token_not_found", error=str(e), public_id=token_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource token not found",
        ) from e
    except Exception as e:
        logger.error("revoke_resource_token_failed", error=str(e), public_id=token_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke resource token",
        ) from e
