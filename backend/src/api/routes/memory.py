"""Memory API routes.

Implements remember(), recall(), forget(), reference() endpoints.
Issue #1 - Core Memory APIs
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from db.base import get_db
from models.memory import Memory
from models.schemas import (
    ExploreRequest,
    ExploreResponse,
    ForgetRequest,
    ForgetResponse,
    MemoryStatsResponse,
    RecallRequest,
    RecallResponse,
    ReferenceRequest,
    ReferenceResponse,
    RememberRequest,
    RememberResponse,
)
from services.context_service import ContextService
from services.memory_service import MemoryService
from services.permission_service import PermissionService
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["memory"])


# ============================================================================
# Dependency Injection
# ============================================================================


async def get_memory_service(db: AsyncSession = Depends(get_db)) -> MemoryService:
    """Get MemoryService instance.

    Args:
        db: Database session

    Returns:
        MemoryService instance
    """
    return MemoryService(db)


async def get_context_service(db: AsyncSession = Depends(get_db)) -> ContextService:
    """Get ContextService instance.

    Args:
        db: Database session

    Returns:
        ContextService instance
    """
    return ContextService(db)


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


# ============================================================================
# Memory API Endpoints
# ============================================================================


@router.post("/remember", response_model=RememberResponse)
async def remember(
    request: RememberRequest,
    user: APIKeyOrSessionUser,
    memory_service: MemoryServiceDep,
):
    """Store new memory with 3-layer architecture.

    Issue #1 specification:
    - Layer 1: summary (Embedding化、検索用)
    - Layer 2: context_summary (文脈説明)
    - Layer 3: content + details (完全詳細)

    Request:
        {
            "summary": "認証エラー修正。JWTトークン有効期限チェック追加。",
            "context_summary": "ユーザーからログイン失敗の報告があり...",
            "content": "auth.pyのverify_token関数にexpired_atの検証を追加",
            "details": {"code_diff": "...", "test_results": "..."},
            "type": "code",
            "importance": 0.8,
            "tags": ["python", "authentication"],
            "context": {"context_id": "my-context"}
        }

    Response:
        {
            "status": "success",
            "memory_id": "uuid",
            "scope": "working"
        }

    Example:
        POST /api/v1/memory/remember
        Authorization: Bearer <session_token>
    """
    logger.info(
        "remember_request",
        user_id=user["user_id"],
        type=request.type,
        importance=request.importance,
    )

    # Issue #82: Pass current context ID for context-based collection
    # Issue #146: Pass current workspace ID for workspace-scoped API keys
    # Issue #273: Use context_id from request.context dict (if provided)
    context_id = request.context.get("context_id") if request.context else None
    result = await memory_service.remember(
        request,
        user_id=user["user_id"],
        client=user.get("client", "web"),
        current_context_id=context_id,
        current_workspace_id=user.get("current_workspace_id"),  # NEW: Issue #146
    )

    return result


@router.post("/reference", response_model=ReferenceResponse)
async def reference(
    request: ReferenceRequest,
    user: APIKeyOrSessionUser,
    memory_service: MemoryServiceDep,
):
    """Get full memory details (Layer 3).

    Retrieves complete memory data including:
    - summary, context_summary, content
    - details (JSONB)
    - All metadata

    Request:
        {
            "memory_id": "uuid"
        }

    Response:
        {
            "memory_id": "uuid",
            "summary": "...",
            "context_summary": "...",
            "content": "...",
            "details": {...},
            "type": "code",
            "importance": 0.8,
            "tags": [...],
            "context": {...},
            "created_at": "2025-11-20T...",
            "client": "web"
        }

    Example:
        POST /api/v1/memory/reference
        Authorization: Bearer <session_token>
    """
    logger.info("reference_request", user_id=user["user_id"], memory_id=str(request.memory_id))

    result = await memory_service.reference(request.memory_id, user_id=user["user_id"])

    return result


@router.post("/recall", response_model=RecallResponse, response_model_exclude_none=True)
async def recall(
    request: RecallRequest,
    user: APIKeyOrSessionUser,
    memory_service: MemoryServiceDep,
):
    """Search memories with Hybrid Search.

    Issue #1 specification:
    - Semantic Search (OpenAI Embedding) 60%
    - BM25/Full-text (Qdrant) 40%
    - Cohere Reranking (optional)

    Note:
        Full implementation in Phase 2.3 (#17)
        Currently returns empty results

    Request:
        {
            "query": "認証エラーの解決方法",
            "k": 5,
            "use_rerank": false,
            "filters": {
                "context_id": "my-context",
                "scope": "persistent",
                "type": "code"
            }
        }

    Example:
        POST /api/v1/memory/recall
        Authorization: Bearer <session_token>
    """
    logger.info("recall_request", user_id=user["user_id"], query=request.query, k=request.k)

    # Issue #82: Pass current context ID for context-based collection
    # Issue #146: Pass current workspace ID for workspace-scoped API keys
    # Issue #246: current_context_id removed - use None
    result = await memory_service.recall(
        request,
        user_id=user["user_id"],
        current_context_id=None,  # Issue #246: current_context_id removed
        current_workspace_id=user.get("current_workspace_id"),  # NEW: Issue #146
    )

    return result


@router.post("/forget", response_model=ForgetResponse)
async def forget(
    request: ForgetRequest,
    user: APIKeyOrSessionUser,
    memory_service: MemoryServiceDep,
):
    """Delete memory.

    Request:
        {
            "memory_id": "uuid"
        }

    Response:
        {
            "status": "success",
            "deleted_count": 1,
            "memory_ids": ["uuid"]
        }

    Example:
        POST /api/v1/memory/forget
        Authorization: Bearer <session_token>
    """
    logger.info("forget_request", user_id=user["user_id"], memory_id=str(request.memory_id))

    # Issue #82: Pass current context ID for context-based collection
    # Issue #246: current_context_id removed - use None
    result = await memory_service.forget(
        request,
        user_id=user["user_id"],
        current_context_id=None,  # Issue #246: current_context_id removed
    )

    return result


@router.post("/explore", response_model=ExploreResponse)
async def explore(
    request: ExploreRequest,
    user: APIKeyOrSessionUser,
    memory_service: MemoryServiceDep,
):
    """Explore related memories via graph traversal.

    Neural Memory graph exploration using Activation Spreading.

    Request:
        {
            "memory_id": "uuid",
            "depth": 2,
            "relation_types": ["neural_association"],
            "min_weight": 0.5
        }

    Response:
        {
            "seed_memory": {...},
            "related_memories": [
                {
                    "memory_id": "uuid2",
                    "summary": "...",
                    "activation": 0.85,
                    "hop": 1,
                    "weight": 0.92,
                    "path": ["uuid", "uuid2"]
                }
            ],
            "metadata": {
                "total_activated": 15,
                "returned": 10
            }
        }

    Example:
        POST /api/v1/memory/explore
        Authorization: Bearer <session_token>
    """
    logger.info(
        "explore_request",
        user_id=user["user_id"],
        memory_id=str(request.memory_id),
        depth=request.depth,
    )

    result = await memory_service.explore(request, user_id=user["user_id"])

    return result


# ============================================================================
# Stats & List Endpoints (Issue #43)
# ============================================================================


class MemoryListItem(BaseModel):
    """Memory list item."""

    id: str
    summary: str
    type: str
    scope: str
    importance: float
    created_at: str
    updated_at: str


class MemoryListResponse(BaseModel):
    """Memory list response."""

    memories: list[MemoryListItem]
    total: int
    has_more: bool


@router.get("/stats", response_model=MemoryStatsResponse)
async def get_memory_stats(
    user: APIKeyOrSessionUser,
    context_id: UUID | None = Query(
        None, description="Optional context ID (defaults to current context)"
    ),
    memory_service: MemoryService = Depends(get_memory_service),
    context_service: ContextService = Depends(get_context_service),
    db: AsyncSession = Depends(get_db),
):
    """Get memory statistics for the authenticated user's context.

    Issue #82: Now context-scoped - returns stats for specified or current context.

    Args:
        user: Authenticated user
        context_id: Optional context ID (defaults to current context)
        memory_service: Memory service instance
        context_service: Context service instance

    Returns:
        Memory statistics for specified or current context including counts, types, and storage
    """
    # Extract user_id (supports both new and legacy session formats)
    if isinstance(user, dict):
        user_id = user.get("user_id") or user.get("sub")
    else:
        user_id = getattr(user, "user_id", None) or getattr(user, "sub", None)

    if not user_id:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id not found in authentication context",
        )

    # Single Collection Migration: Extract workspace_id and context_id
    target_context_id = context_id if context_id else None
    target_workspace_id = None
    is_shared = False

    if target_context_id:
        try:
            from sqlalchemy import select

            from models.auth import Context, Workspace
            from utils.exceptions import NotFoundException

            # SECURITY: Verify context belongs to current workspace (ALWAYS)
            # Issue #271 Code Review H-3: Make validation mandatory (not conditional)
            current_workspace_id = user.get("current_workspace_id")

            # Get context directly to verify workspace ownership
            context_result = await db.execute(
                select(Context).where(Context.id == target_context_id)
            )
            context_obj = context_result.scalar_one_or_none()

            if context_obj and current_workspace_id:
                # Verify context belongs to current workspace
                if context_obj.workspace_id != current_workspace_id:
                    from fastapi import HTTPException, status

                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="This context belongs to a different workspace. Please switch workspaces first.",
                    )
            elif context_obj and not current_workspace_id:
                # User has no current_workspace_id but accessing a specific context
                # This is suspicious - require workspace selection
                from fastapi import HTTPException, status

                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No workspace selected. Please select an workspace to view context statistics.",
                )

            context = await context_service.get_context(user_id, target_context_id)
            target_workspace_id = str(context.workspace_id)

            # Check if user is workspace owner
            workspace_result = await db.execute(
                select(Workspace).where(Workspace.id == user.get("current_workspace_id"))
            )
            workspace = workspace_result.scalar_one_or_none()
            is_workspace_owner = workspace and workspace.owner_user_id == user_id

            # Owner sees all memories (treat as shared), or context is actually shared
            is_shared = is_workspace_owner or not context.is_private
            logger.info(
                f"Context privacy check: context_id={target_context_id}, is_private={context.is_private}, is_workspace_owner={is_workspace_owner}, is_shared={is_shared}"
            )
        except NotFoundException:
            # Context not found - default to private (user_id filter)
            logger.warning(f"Context not found: {target_context_id}. Defaulting to private stats")
        except (ConnectionError, OSError) as e:
            # Database/network connectivity issues - critical, re-raise
            logger.error(f"Database connectivity issue while checking context privacy: {e}")
            raise
        except Exception as e:
            # Other unexpected errors - log and default to safe behavior
            logger.warning(
                f"Unexpected error checking context privacy: {e}. Defaulting to private stats"
            )

    # Call service layer (24h window for REST API)
    result = await memory_service.get_stats(
        user_id=user_id,
        workspace_id=target_workspace_id,
        context_id=str(target_context_id) if target_context_id else None,
        include_details=True,
        time_window_hours=24,
        is_shared_context=is_shared,
    )

    logger.info("memory_stats_retrieved", user_id=user_id, total=result.total_count)

    return result


@router.get("/list", response_model=MemoryListResponse)
async def list_memories(
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
    scope: str | None = Query(None, pattern="^(working|persistent)$"),
    type: str | None = Query(None),
    context_id: UUID | None = Query(
        None, description="Optional context ID to scope results to a single context"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List memories for the authenticated user.

    Args:
        user: Authenticated user
        db: Database session
        scope: Filter by scope (working or persistent)
        type: Filter by memory type
        context_id: Optional context ID. When present, results are restricted
            to that context and the caller must have workspace-read access to
            it (uniform 404 via ``PermissionService`` on denial / non-existence).
            The ``user_id`` filter is applied unchanged — ``context_id`` is
            additive, not a bypass.
        limit: Maximum number of memories to return
        offset: Pagination offset

    Returns:
        Paginated list of memories
    """
    try:
        # Extract user_id (supports both new and legacy session formats)
        # New format: {"user_id": "...", "sub": "...", ...}
        # Legacy format: {"sub": "...", ...} (backward compatibility)
        if isinstance(user, dict):
            user_id = user.get("user_id") or user.get("sub")
        else:
            user_id = getattr(user, "user_id", None) or getattr(user, "sub", None)

        if not user_id:
            raise ValueError("user_id or sub not found in user object")

        if context_id is not None:
            # Raises 404 on non-existent context, non-member, or private-context
            # non-creator (CWE-639 uniform disclosure). Matches the graph routes.
            await PermissionService(db).resolve_context_for_workspace_read(
                user_id=user_id, context_id=context_id
            )

        # Build query. Exclude soft-deleted rows — POST /forget sets
        # ``deleted_at`` rather than removing the row, and the list view
        # must not surface tombstones.
        query = select(Memory).where(
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
        )

        if scope:
            query = query.where(Memory.scope == scope)

        if type:
            query = query.where(Memory.type == type)

        if context_id is not None:
            query = query.where(Memory.context_id == context_id)

        # Get total count (with same filters as data query)
        count_query = select(func.count(Memory.id)).where(
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
        )
        if scope:
            count_query = count_query.where(Memory.scope == scope)
        if type:
            count_query = count_query.where(Memory.type == type)
        if context_id is not None:
            count_query = count_query.where(Memory.context_id == context_id)
        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

        # Get memories
        result = await db.execute(
            query.order_by(Memory.created_at.desc()).limit(limit).offset(offset)
        )
        memories = list(result.scalars().all())

        # Convert to response. Memory.created_at / updated_at are stored as
        # naive UTC datetimes (DateTime without timezone=True). Tag the
        # serialized form with "Z" so JS clients (which parse naive ISO as
        # local time) don't render JST-shifted relative timestamps.
        def _utc_iso(dt: Any) -> str:
            return dt.isoformat() + ("Z" if dt.tzinfo is None else "")

        memory_items = [
            MemoryListItem(
                id=str(m.id),
                summary=m.summary,
                type=m.type,
                scope=m.scope,
                importance=m.importance,
                created_at=_utc_iso(m.created_at),
                updated_at=_utc_iso(m.updated_at),
            )
            for m in memories
        ]

        has_more = offset + len(memories) < total

        logger.info("memory_list_retrieved", user_id=user_id, count=len(memories), total=total)

        return MemoryListResponse(memories=memory_items, total=total, has_more=has_more)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_memories_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list memories",
        ) from e


@router.get("/access-patterns")
async def get_access_patterns(
    user: APIKeyOrSessionUser,
    context_id: UUID | None = Query(
        None, description="Optional context ID (defaults to current context)"
    ),
    db: AsyncSession = Depends(get_db),
    context_service: ContextService = Depends(get_context_service),
    days: int = 30,
) -> dict[str, Any]:
    """Get memory access patterns and analytics for specified or current context.

    Issue #82: Now context-scoped - returns patterns for specified or current context.

    Returns most accessed memories, type distribution, and access timeline.

    Args:
        user: Authenticated user
        context_id: Optional context ID (defaults to current context)
        db: Database session
        context_service: Context service instance
        days: Number of days to analyze (default: 30)

    Returns:
        Access pattern statistics for specified or current context
    """
    try:
        user_id = user["user_id"]
        # Issue #246: current_context_id removed - use provided context_id or None
        target_context_id = context_id if context_id else None

        from datetime import timedelta

        from sqlalchemy import and_, desc, func

        # Single Collection Migration: Get workspace_id and context_id for filtering
        target_workspace_id = None
        if target_context_id:
            context = await context_service.get_context(user_id, target_context_id)
            target_workspace_id = context.workspace_id

        cutoff = utcnow() - timedelta(days=days)

        # Build base filter
        base_filter = [
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
        ]
        if target_workspace_id:
            base_filter.append(Memory.workspace_id == target_workspace_id)
        if target_context_id:
            base_filter.append(Memory.context_id == target_context_id)

        # Most accessed memories (TOP 10)
        most_accessed_result = await db.execute(
            select(Memory)
            .where(and_(*base_filter, Memory.last_used_at.isnot(None)))
            .order_by(desc(Memory.access_count))
            .limit(10)
        )
        most_accessed = list(most_accessed_result.scalars().all())

        # Type distribution
        type_dist_result = await db.execute(
            select(Memory.type, func.count(Memory.id))
            .where(and_(*base_filter))
            .group_by(Memory.type)
        )
        type_distribution = {row[0]: row[1] for row in type_dist_result.all()}

        # Recent access count
        recent_access_result = await db.execute(
            select(func.count(Memory.id)).where(and_(*base_filter, Memory.last_used_at >= cutoff))
        )
        recent_access_count = recent_access_result.scalar() or 0

        return {
            "most_accessed": [
                {
                    "memory_id": str(m.id),
                    "summary": m.summary,
                    "type": m.type,
                    "access_count": m.access_count,
                    "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
                }
                for m in most_accessed
            ],
            "type_distribution": type_distribution,
            "recent_access_count": recent_access_count,
            "analysis_days": days,
        }

    except Exception as e:
        logger.error("access_patterns_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve access patterns",
        ) from e
