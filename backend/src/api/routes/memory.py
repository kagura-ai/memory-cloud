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
    LoadPinnedRequest,
    LoadPinnedResponse,
    MemoryStatsResponse,
    PatchMemoryRequest,
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
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import MemoryCloudException
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
    try:
        result = await memory_service.remember(
            request,
            user_id=user["user_id"],
            client=user.get("client", "web"),
            current_context_id=context_id,
            current_workspace_id=user.get("current_workspace_id"),  # NEW: Issue #146
        )
    except ValueError as e:
        # MemoryService raises ValueError as its "bad request" signal (e.g. an
        # invalid type="time" details.trigger). Map it to 422 rather than
        # letting it surface as an unhandled 500.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

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


@router.post("/pinned", response_model=LoadPinnedResponse)
async def load_pinned(
    request: LoadPinnedRequest,
    user: APIKeyOrSessionUser,
    memory_service: MemoryServiceDep,
):
    """Deterministically load a context's always-delivery memories (Issue #886).

    The deterministic counterpart to ``/recall``: returns the complete,
    unranked, ordered ``delivery_mode='always'`` set for the given context —
    no semantic ranking, no rerank. Items carry L1 + L2 only (summary +
    context_summary); fetch full content via ``/reference``. Bounded by
    ``settings.pinned_load_cap`` (or the request ``cap``); when the pinned set
    exceeds the cap, ``truncated`` is true and ``total_available`` reports the
    real count — never a silent truncation.

    Request:
        {"context_id": "my-context", "cap": 100}

    Example:
        POST /api/v1/memory/pinned
        Authorization: Bearer <session_token>
    """
    logger.info("load_pinned_request", user_id=user["user_id"], context_id=request.context_id)

    # Parse context_id to a UUID up front: a non-UUID string would otherwise
    # reach `Context.id == <str>` and raise a DB DataError, which the global
    # SQLAlchemyError handler maps to a misleading 503. Reject it as a 422 here.
    context_uuid: UUID | None = None
    if request.context_id is not None:
        try:
            context_uuid = UUID(request.context_id)
        except (ValueError, AttributeError) as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"context_id must be a valid UUID: {request.context_id!r}",
            ) from e

    try:
        return await memory_service.load_pinned(
            user_id=user["user_id"],
            current_context_id=context_uuid,
            current_workspace_id=user.get("current_workspace_id"),
            cap=request.cap,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e


@router.patch("/{memory_id}", response_model=ReferenceResponse)
async def patch_memory(
    memory_id: UUID,
    request: PatchMemoryRequest,
    user: APIKeyOrSessionUser,
    memory_service: MemoryServiceDep,
):
    """Partial update of a memory by UUID (Issue #439).

    Accepts any subset of ``summary``/``content``/``type``/``importance``/
    ``tags``/``details``. Omitted fields preserve their current value;
    ``tags`` follows replace-all semantics.

    Status codes:
        200: Updated successfully. Body is the full ``ReferenceResponse``.
        404: Memory does not exist OR the caller lacks access (existence
             is intentionally not leaked).
        410: Memory was soft-deleted; the tombstone exists but the resource
             is gone. Distinguishing 410 from 404 lets clients stop retrying.
        422: Request body validation failed (empty patch, importance out of
             range, summary < 10 chars, etc.).
        429: The patched memory's post-patch size exceeds ``MAX_CONTENT_SIZE``
             (1MB) — same envelope as ``remember`` / ``update_memory``.

    Permission: same envelope as ``forget`` — ``PermissionService.
    can_access_memory`` (workspace member for shared, creator for private).

    Embedding regeneration: triggered only when ``summary`` or ``content``
    changes. Runs on the same async-task pipeline as the ``remember`` path
    (``process_pending_embedding``); this PATCH does not block on embedding
    completion. Neural edges anchored on this memory are invalidated when
    re-embed fires.

    Example:
        PATCH /api/v1/memory/550e8400-e29b-41d4-a716-446655440000
        Authorization: Bearer <session_token>
        Content-Type: application/json

        {"importance": 0.9, "tags": ["python", "auth"]}
    """
    logger.info(
        "patch_memory_request",
        user_id=user["user_id"],
        memory_id=str(memory_id),
        fields=sorted(request.model_fields_set),
    )

    try:
        return await memory_service.patch_memory(
            memory_id=memory_id,
            request=request,
            user_id=user["user_id"],
        )
    except ValueError as e:
        # MemoryService raises ValueError as its "bad request" signal (e.g. a
        # PATCH flipping type to "time" without a valid details.trigger). Map it
        # to 422 — same as the remember route — rather than an unhandled 500.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e


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
    q: str | None = Query(
        None,
        max_length=200,
        description="Optional case-insensitive substring filter on memory.summary. "
        "Whitespace-only values are treated as None.",
    ),
    tags: list[str] | None = Query(
        None,
        description="Filter to memories having ANY of these tags (exact match). "
        "Repeat the param to pass several: ?tags=a&tags=b. Combined with other "
        "filters (e.g. q) by AND. Blank / whitespace-only entries are ignored.",
    ),
    tags_match: str = Query(
        "any",
        pattern="^(any|all)$",
        description="How to combine `tags`: `any` (default — memory has at least "
        "one of the tags, PG array overlap; preserves #618 behavior) or `all` "
        "(memory holds every given tag, PG array contains — #830 drill-down).",
    ),
    trigger_from: str | None = Query(
        None,
        description="Time Memory (#877) window lower bound (naive ISO, e.g. "
        "2026-07-01T00:00:00). Selects type='time' memories whose stored "
        "[trigger_from, trigger_until] window overlaps the query window. "
        "Pass 'now' here to get upcoming items.",
    ),
    trigger_until: str | None = Query(
        None,
        description="Time Memory (#877) window upper bound (naive ISO). Omit for "
        "an open-ended (future) window.",
    ),
    order_by: str = Query(
        "created_at",
        pattern="^(created_at|trigger_from)$",
        description="Sort key. 'created_at' (default, desc) or 'trigger_from' "
        "(asc) for upcoming-first Time Memory listing (#877).",
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
        context_id: Optional context ID. When present, the caller must have
            workspace-read access (uniform 404 via ``PermissionService`` on
            denial / non-existence). For shared contexts every member sees
            every memory regardless of original creator; for private contexts
            only the creator sees their own. Mirrors the graph routes'
            ``owner_filter = user_id if context.is_private else None`` pattern
            in ``api/routes/graph.py``.
        q: Optional case-insensitive substring filter applied to
            ``Memory.summary`` via SQL ``ILIKE``. Surrounding whitespace is
            stripped, and whitespace-only / empty values are normalized to
            ``None`` (no filter). Independent of ``owner_filter`` — works
            for both private (creator-only) and shared (all-members)
            contexts.
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

        # `owner_filter` is `user_id` for private contexts (creator-only) or
        # the unscoped "my memories" view (no context_id), and `None` for
        # shared contexts (all workspace members see every memory).
        owner_filter: str | None = user_id
        if context_id is not None:
            # Raises 404 on non-existent context, non-member, or private-context
            # non-creator (CWE-639 uniform disclosure). Matches the graph routes.
            context = await PermissionService(db).resolve_context_for_workspace_read(
                user_id=user_id, context_id=context_id
            )
            owner_filter = user_id if context.is_private else None

        # Normalize the substring filter: strip surrounding whitespace and
        # treat empty / whitespace-only values as "no filter" so callers
        # that bind an empty input box don't accidentally pin results to
        # rows whose summary literally contains spaces. Independent of
        # ``owner_filter`` — applies uniformly to both private and shared
        # context paths.
        q_normalized = (q or "").strip() or None

        # Escape SQL LIKE wildcards in user input so a literal ``%`` or
        # ``_`` is matched as a character, not as a wildcard. Without this,
        # ``q="50%"`` matches every summary containing "50" followed by
        # anything, and ``q="_"`` matches every single-character row.
        # Escape backslash first to avoid double-escaping the escape itself.
        q_pattern: str | None = None
        if q_normalized:
            q_escaped = q_normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            q_pattern = f"%{q_escaped}%"

        # Normalize the tag filter (#618): strip blanks, drop whitespace-only
        # entries, de-dup (order-preserving). An all-blank list collapses to
        # None so a stray ``?tags=`` doesn't pin results to the empty set.
        tags_normalized: list[str] | None = None
        if tags:
            # Bound the filter to the write-path caps (<=64 chars/tag, <=100
            # tags): drop blank / over-length entries, de-dup (order-preserving),
            # then cap — so an oversized query string can't reach the DB.
            cleaned = list(dict.fromkeys(s for t in tags if (s := t.strip()) and len(s) <= 64))[
                :100
            ]
            tags_normalized = cleaned or None

        # Build query. Exclude soft-deleted rows — POST /forget sets
        # ``deleted_at`` rather than removing the row, and the list view
        # must not surface tombstones.
        query = select(Memory).where(Memory.deleted_at.is_(None))
        if owner_filter is not None:
            query = query.where(Memory.user_id == owner_filter)

        if scope:
            query = query.where(Memory.scope == scope)

        if type:
            query = query.where(Memory.type == type)

        if context_id is not None:
            query = query.where(Memory.context_id == context_id)

        if q_pattern is not None:
            query = query.where(Memory.summary.ilike(q_pattern, escape="\\"))

        # Tag filter (#618 ANY / #830 ALL). Built once and applied to BOTH the
        # data and count queries so the total always reflects the same filter
        # (the two used to carry duplicate predicates — easy to let drift).
        # NULL-tags rows never match either operator, so they're excluded in
        # both modes.
        tag_filter = None
        if tags_normalized is not None:
            if tags_match == "all":
                # ALL-match (#830): row's tags contain every requested tag
                # (PG array contains ``@>``).
                tag_filter = Memory.tags.contains(tags_normalized)
            else:
                # ANY-match (default, #618): row has at least one of the
                # requested tags (PG array overlap ``&&``).
                tag_filter = Memory.tags.overlap(tags_normalized)
        if tag_filter is not None:
            query = query.where(tag_filter)

        # Time Memory (#877) window overlap, built once and applied to BOTH
        # queries (same anti-drift rationale as tag_filter above). A stored
        # window [trigger_from, trigger_until] overlaps the query window
        # [qfrom, quntil] iff trigger_from <= quntil AND trigger_until >= qfrom.
        # The columns are TEXT fixed-width ISO, so string comparison ==
        # chronological comparison — but ONLY if the caller's bounds are the
        # same fixed-width form. parse_query_bound re-normalizes them (and
        # resolves the 'now' shortcut); a malformed bound is a 422, not silent
        # wrong results. isinstance(str) (not `is not None`) skips the unset
        # Query() FieldInfo sentinel that direct-call unit tests pass.
        from utils.time_trigger import TriggerValidationError, parse_query_bound

        try:
            qfrom = parse_query_bound(trigger_from) if isinstance(trigger_from, str) else None
            quntil = parse_query_bound(trigger_until) if isinstance(trigger_until, str) else None
        except TriggerValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            ) from e

        window_filters = []
        if qfrom is not None:
            window_filters.append(Memory.trigger_until >= qfrom)
        if quntil is not None:
            window_filters.append(Memory.trigger_from <= quntil)
        for wf in window_filters:
            query = query.where(wf)

        # Get total count (with same filters as data query)
        count_query = select(func.count(Memory.id)).where(Memory.deleted_at.is_(None))
        if owner_filter is not None:
            count_query = count_query.where(Memory.user_id == owner_filter)
        if scope:
            count_query = count_query.where(Memory.scope == scope)
        if type:
            count_query = count_query.where(Memory.type == type)
        if context_id is not None:
            count_query = count_query.where(Memory.context_id == context_id)
        if q_pattern is not None:
            count_query = count_query.where(Memory.summary.ilike(q_pattern, escape="\\"))
        if tag_filter is not None:
            count_query = count_query.where(tag_filter)
        for wf in window_filters:
            count_query = count_query.where(wf)
        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

        # Get memories. order_by=trigger_from (#877) sorts ascending for
        # upcoming-first Time Memory listing; default stays created_at desc.
        order_clause = (
            Memory.trigger_from.asc() if order_by == "trigger_from" else Memory.created_at.desc()
        )
        result = await db.execute(query.order_by(order_clause).limit(limit).offset(offset))
        memories = list(result.scalars().all())

        # Convert to response. Memory.created_at / updated_at are stored as
        # naive UTC datetimes (DateTime without timezone=True). Tag the
        # serialized form with "Z" so JS clients (which parse naive ISO as
        # local time) don't render JST-shifted relative timestamps.
        # ``updated_at`` is nullable (only set onupdate), so fall back to
        # ``created_at`` for fresh rows that haven't been touched since
        # insert. The frontend renders both as the row "updated" timestamp,
        # so created_at is the correct fallback rather than null/empty.
        memory_items = [
            MemoryListItem(
                id=str(m.id),
                summary=m.summary,
                type=m.type,
                scope=m.scope,
                importance=m.importance,
                created_at=to_utc_iso(m.created_at) or "",
                updated_at=to_utc_iso(m.updated_at or m.created_at) or "",
            )
            for m in memories
        ]

        has_more = offset + len(memories) < total

        # Log presence + length of `q` rather than the value — user search
        # strings can carry PII or secrets the operator did not intend to
        # persist in log aggregation.
        logger.info(
            "memory_list_retrieved",
            user_id=user_id,
            count=len(memories),
            total=total,
            q_present=q_normalized is not None,
            q_len=len(q_normalized) if q_normalized else 0,
            tag_filter_count=len(tags_normalized) if tags_normalized else 0,
        )

        return MemoryListResponse(memories=memory_items, total=total, has_more=has_more)

    except (HTTPException, MemoryCloudException):
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
                    "last_used_at": to_utc_iso(m.last_used_at),
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
