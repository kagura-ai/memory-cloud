"""Public Search API routes.

Issue #238: Public REST API for external access to public contexts.

Provides search endpoints for public contexts with schema-aware responses.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_optional
from db.base import get_db
from db.redis import increment_counter
from models.auth import Context
from services.resource_lookup import get_latest_schema
from services.search_service import SearchService
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import AuthorizationError, NotFoundException, RateLimitError
from utils.logger import get_logger
from utils.usage_logger import log_usage

logger = get_logger(__name__)

router = APIRouter(prefix="/public", tags=["public-search"])


# ============================================================================
# Pydantic Models
# ============================================================================


class PublicSearchRequest(BaseModel):
    """Public search request."""

    query: str = Field(..., min_length=1, max_length=500, description="Search query")
    limit: int = Field(10, ge=1, le=100, description="Maximum results (default: 10, max: 100)")
    use_rerank: bool = Field(False, description="Enable reranking for better results")
    search_mode: str = Field(
        "hybrid",
        pattern="^(hybrid|semantic|keyword)$",
        description="Search mode: hybrid (default), semantic, keyword",
    )


class PublicSearchResult(BaseModel):
    """Single search result with schema-aware formatting."""

    memory_id: str
    content: str
    score: float
    metadata: dict
    highlighted: dict | None = None


class PublicSearchResponse(BaseModel):
    """Public search response."""

    status: str = "success"
    query: str
    results: list[PublicSearchResult]
    count: int
    context_id: str
    resource_id: str | None = None
    schema_version: int | None = None


# ============================================================================
# Rate Limiting
# ============================================================================


async def check_public_search_rate_limit(
    context_id: UUID,
    user_id: str | None,
    limit_per_minute: int = 50,
) -> None:
    """Check rate limit for public search API.

    Args:
        context_id: Context ID
        user_id: User ID (None for anonymous/public token)
        limit_per_minute: Rate limit (default: 50/min for public, unlimited for workspace members)

    Raises:
        RateLimitError: If rate limit exceeded
    """
    # Skip rate limit check for authenticated workspace members
    if user_id:
        logger.debug("rate_limit_skipped_for_workspace_member", user_id=user_id)
        return

    # Apply rate limit for anonymous/public token access
    redis_key = f"public_search:{context_id}:minute"

    try:
        current_count = await increment_counter(redis_key, ttl=60)

        if current_count > limit_per_minute:
            logger.warning(
                "public_search_rate_limit_exceeded",
                context_id=context_id,
                current=current_count,
                limit=limit_per_minute,
            )
            raise RateLimitError(
                message=f"Public search rate limit exceeded: {current_count}/{limit_per_minute} per minute",
                retry_after=60,
            )

        logger.debug(
            "public_search_rate_limit_checked",
            context_id=context_id,
            current=current_count,
            limit=limit_per_minute,
        )

    except RateLimitError:
        raise
    except Exception as e:
        # Redis errors should not block search (fail open)
        logger.error("redis_rate_limit_check_failed", error=str(e))


# ============================================================================
# Public Search Endpoint
# ============================================================================


@router.post("/{context_id}/search", response_model=PublicSearchResponse)
async def public_search(
    context_id: UUID,
    request: PublicSearchRequest,
    user: dict | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Search public context with schema-aware response formatting.

    Issue #238: Public REST API for external systems (EC inventory, docs, etc.).

    Authentication:
        - Workspace member session (authenticated access, no rate limit)
        - Anonymous access (rate limited to 50 req/min per context)

    Note:
        Public token-based authentication is planned for future implementation.
        Currently, this endpoint supports both authenticated workspace member sessions
        and anonymous access with rate limiting.

    Rate Limits:
        - Anonymous access: 50 requests/minute per context
        - Workspace members: No limit (uses normal workspace quota)

    Request:
        POST /api/v1/public/{context_id}/search
        Body:
        {
            "query": "商品名 価格",
            "limit": 10,
            "use_rerank": false
        }

    Response:
        {
            "status": "success",
            "query": "商品名 価格",
            "results": [
                {
                    "memory_id": "...",
                    "content": "商品名: ..., 価格: ...",
                    "score": 0.95,
                    "metadata": {...},
                    "highlighted": {...}
                }
            ],
            "count": 5,
            "context_id": "...",
            "resource_id": "ec_products",
            "schema_version": 1
        }

    Errors:
        - 403: Context is not public
        - 404: Context not found
        - 429: Rate limit exceeded (public access only)
    """
    start_time = utcnow()

    logger.info(
        "public_search_request",
        context_id=context_id,
        query=request.query,
        limit=request.limit,
        has_user=user is not None,
    )

    # 1. Get context and verify it's public
    context = await db.get(Context, context_id)
    if not context:
        raise NotFoundException("Context", str(context_id))

    if context.is_public is not True:
        raise AuthorizationError("This context is not public")

    # SECURITY: For authenticated users, verify workspace boundary
    # Issue #268: Workspace boundary violation prevention
    # Authenticated users can only search public contexts in their own workspace
    # Anonymous users can search any public context (rate limited)
    if user is not None:
        current_workspace_id = user.get("current_workspace_id")

        # Authenticated user must have workspace selected
        if not current_workspace_id:
            raise HTTPException(
                status_code=400, detail="No workspace selected. Please select an workspace first."
            )

        # Verify context belongs to user's current workspace
        if context.workspace_id != current_workspace_id:
            raise HTTPException(
                status_code=403,
                detail="This public context belongs to a different workspace. Please switch workspaces or use anonymous access.",
            )

    # 2. Check rate limit (skip for workspace members)
    request_user_id: str | None = user.get("user_id") if user else None
    await check_public_search_rate_limit(context_id, request_user_id)

    # 3. Perform search
    search_service = SearchService(db)

    # For public search, use context owner's user_id for API key resolution
    # Public contexts are searchable by anyone, but we need a user_id for embedding service
    # Use context owner's user_id
    try:
        # Get context owner's workspace
        from models.auth import Workspace

        workspace = await db.get(Workspace, context.workspace_id)
        if not workspace:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Context workspace not found",
            )

        # Use workspace owner's user_id for search (for API key resolution)
        owner_id = workspace.owner_user_id
        search_user_id: str = str(owner_id) if owner_id is not None else "system"

        # Single Collection Migration: Use workspace_id/context_id directly
        # Perform hybrid search
        results = await search_service.hybrid_search(
            query=request.query,
            user_id=search_user_id,
            workspace_id=str(context.workspace_id),
            context_id=str(context_id),
            k=request.limit,
            use_rerank=request.use_rerank and user is not None,  # Rerank only for workspace members
            search_mode=request.search_mode,
        )

        # 4. Load schema for response formatting (if resource-backed).
        schema = (
            await get_latest_schema(db, context.workspace_id, context.resource_id)
            if context.resource_id is not None
            else None
        )

        # 5. Format results with schema-aware metadata
        formatted_results = []
        for result in results:
            payload = result.get("payload", {})
            # Use summary as primary content, fallback to content field
            content = payload.get("summary", payload.get("content", ""))

            formatted_result = PublicSearchResult(
                memory_id=str(result["id"]),  # Correct field name
                content=content,
                score=result["score"],
                metadata=payload,  # Entire payload as metadata
                highlighted=None,  # TODO: Implement highlighting in future
            )
            formatted_results.append(formatted_result)

        # 6. Log usage
        log_user_id: str = str(user.get("user_id", "anonymous")) if user else "anonymous"
        response_time_ms = int((utcnow() - start_time).total_seconds() * 1000)

        await log_usage(
            db=db,
            user_id=log_user_id,
            endpoint=f"/api/v1/public/{context_id}/search",
            method="POST",
            status_code=200,
            response_time_ms=response_time_ms,
        )

        logger.info(
            "public_search_completed",
            context_id=context_id,
            result_count=len(formatted_results),
            has_schema=schema is not None,
            response_time_ms=response_time_ms,
        )

        # Extract values for type safety (SQLAlchemy Column types need explicit handling)
        ctx_resource_id = context.resource_id
        schema_ver: int | None = int(schema.schema_version) if schema else None  # type: ignore[arg-type]

        return PublicSearchResponse(
            status="success",
            query=request.query,
            results=formatted_results,
            count=len(formatted_results),
            context_id=str(context_id),
            resource_id=str(ctx_resource_id) if ctx_resource_id is not None else None,
            schema_version=schema_ver,
        )

    except Exception as e:
        # Log failed usage
        error_user_id: str = str(user.get("user_id", "anonymous")) if user else "anonymous"
        response_time_ms = int((utcnow() - start_time).total_seconds() * 1000)

        await log_usage(
            db=db,
            user_id=error_user_id,
            endpoint=f"/api/v1/public/{context_id}/search",
            method="POST",
            status_code=500,
            response_time_ms=response_time_ms,
        )

        logger.error(
            "public_search_failed",
            context_id=context_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {str(e)}",
        ) from e


# ============================================================================
# Public Context Info Endpoint
# ============================================================================


@router.get("/{context_id}/info")
async def get_public_context_info(
    context_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get public context metadata and schema information.

    Returns basic information about the public context including:
    - Context name and description
    - Resource schema (if resource-backed)
    - Statistics (memory count, last updated)

    Args:
        context_id: Context UUID
        db: Database session

    Returns:
        Public context metadata

    Errors:
        - 403: Context is not public
        - 404: Context not found
    """
    # Get context
    context = await db.get(Context, context_id)
    if not context:
        raise NotFoundException("Context", str(context_id))

    if context.is_public is not True:
        raise AuthorizationError("This context is not public")

    # Load schema if resource-backed.
    schema = (
        await get_latest_schema(db, context.workspace_id, context.resource_id)
        if context.resource_id is not None
        else None
    )

    return {
        "context_id": str(context_id),
        "name": context.name,
        "display_name": context.display_name,
        "description": context.description,
        "is_public": context.is_public,
        "resource_id": context.resource_id,
        "schema": {
            "version": schema.schema_version,
            "field_definitions": schema.field_definitions,
            "updated_at": to_utc_iso(schema.updated_at),
        }
        if schema
        else None,
        "created_at": to_utc_iso(context.created_at),
    }
