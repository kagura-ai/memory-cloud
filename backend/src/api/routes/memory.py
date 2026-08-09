"""Memory API routes.

Implements remember(), recall(), forget(), reference() endpoints.
Issue #1 - Core Memory APIs
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
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
from services.agent_binding_service import binding_memory_sql_predicate
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
            "scope": "working",
            "persistence": {
                "scope": "working",
                "committed": true,
                "promotes_via": "sleep_consolidation",
                "consolidation_archive_min_age_days": 30,
                "detail": "..."
            }
        }

    The memory is committed before this returns; ``scope`` selects the
    consolidation lifecycle, not whether it was stored. ``persistence``
    describes that lifecycle for this deployment and is null when the scope
    cannot be classified. Its age floor is scoped to consolidation and is not a
    retention guarantee — see ``services.persistence``.

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
            # Issue #963/#1281 item 2: confine a workspace-scoped key to its own
            # workspace (pure key scope; None for OAuth/session/global-key).
            key_workspace_id=user.get("api_key_workspace_id"),
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
    # Issue #1036: scope recall to the requested context. The endpoint used to
    # hardcode current_context_id=None (a leftover from #246), but
    # MemoryService.recall() still requires a context, so every recall 500'd.
    # Forward filters.context_id, mirroring how /remember resolves
    # request.context["context_id"]. No filter → None (the service guard then
    # rejects it, same as before — see test_recall_no_workspace).
    context_id = request.filters.get("context_id") if request.filters else None
    try:
        result = await memory_service.recall(
            request,
            user_id=user["user_id"],
            current_context_id=context_id,
            current_workspace_id=user.get("current_workspace_id"),  # NEW: Issue #146
        )
    except ValueError as e:
        # Mirror /remember: MemoryService.recall raises ValueError for bad
        # requests (missing context, or a non-UUID context_id that fails to
        # parse downstream). Map to 422 rather than letting it surface as 500.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

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
        {"context_id": "<context-uuid>", "cap": 100}

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
            # Issue #963/#1281 item 2: pure key scope (None unless workspace-scoped key).
            key_workspace_id=user.get("api_key_workspace_id"),
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
        # Issue #963/#1281 item 2: pure key scope. Inert while current_context_id
        # is None (the isolation helper short-circuits), wired for symmetry so the
        # confinement holds the moment forget regains a declared context.
        key_workspace_id=user.get("api_key_workspace_id"),
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


class MemoryListItemLocation(BaseModel):
    """WHERE-axis coordinates of a list item (#1334), sourced from the
    ``location_lat`` / ``location_lon`` generated columns."""

    lat: float
    lon: float


class MemoryListItem(BaseModel):
    """Memory list item."""

    id: str
    summary: str
    type: str
    scope: str
    importance: float
    created_at: str
    updated_at: str
    location: MemoryListItemLocation | None = None


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
    db: AsyncSession = Depends(get_db),
):
    """Get memory statistics for the authenticated user's context.

    Issue #82: Now context-scoped - returns stats for specified or current context.

    Args:
        user: Authenticated user
        context_id: Optional context ID (defaults to current context)
        memory_service: Memory service instance

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
        from models.auth import Workspace

        # SECURITY (#1011 / #383): resolve through the workspace-read chokepoint
        # so a cross-workspace probe yields a uniform 404 (context_not_found)
        # instead of a 403 that confirms the context exists in another workspace
        # (CWE-639 / OWASP A01). Mirrors GET /memory/list below and the graph
        # routes; a member of the context's OWNING workspace can read its stats
        # regardless of which workspace is currently active.
        context = await PermissionService(db).resolve_context_for_workspace_read(
            user_id=user_id,
            context_id=target_context_id,
            key_workspace_id=user.get("api_key_workspace_id"),
        )
        target_workspace_id = str(context.workspace_id)

        # Workspace owner sees all memories (treated as shared); otherwise a
        # non-private (shared) context is visible to every member. Anchored to
        # the context's OWNING workspace, not the caller's current workspace.
        workspace_result = await db.execute(
            select(Workspace).where(Workspace.id == context.workspace_id)
        )
        workspace = workspace_result.scalar_one_or_none()
        is_workspace_owner = bool(workspace and workspace.owner_user_id == user_id)
        is_shared = is_workspace_owner or not context.is_private
        logger.info(
            f"Context privacy check: context_id={target_context_id}, "
            f"is_private={context.is_private}, is_workspace_owner={is_workspace_owner}, "
            f"is_shared={is_shared}"
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


def _list_q_pattern(q: str | None) -> tuple[str | None, str | None]:
    """Normalize the ``q`` substring filter into (normalized, LIKE pattern).

    Strips surrounding whitespace and treats empty / whitespace-only values as
    "no filter", so a caller bound to an empty input box doesn't pin results to
    rows whose summary literally contains spaces.

    SQL LIKE wildcards in the user's input are escaped so a literal ``%`` or
    ``_`` matches as a character: without it ``q="50%"`` matches every summary
    containing "50" followed by anything, and ``q="_"`` matches every
    single-character row. Backslash is escaped first, or it would double-escape
    the escape character itself.

    Returns:
        ``(normalized, pattern)``. ``normalized`` is for logging (presence and
        length only — search strings can carry PII); ``pattern`` goes to
        ``Memory.summary.ilike()`` with a single backslash as the ``escape``
        character, matching what this function inserts. Both are ``None`` when
        there is no filter.
    """
    normalized = (q or "").strip() or None
    if not normalized:
        return None, None
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return normalized, f"%{escaped}%"


def _list_tags(tags: list[str] | None) -> list[str] | None:
    """Normalize the tag filter (#618) to the write-path caps.

    Drops blank / over-length (>64 char) entries, de-dups order-preserving, and
    caps at 100 tags, so an oversized query string can never reach the DB. An
    all-blank list collapses to ``None`` — a stray ``?tags=`` must not pin
    results to the empty set.
    """
    if not tags:
        return None
    cleaned = list(dict.fromkeys(s for t in tags if (s := t.strip()) and len(s) <= 64))[:100]
    return cleaned or None


def _list_tag_filter(tags_normalized: list[str] | None, tags_match: str) -> Any | None:
    """ANY (#618, PG ``&&`` overlap) or ALL (#830, PG ``@>`` contains) tag match.

    NULL-tags rows never match either operator, so they are excluded in both
    modes.
    """
    if tags_normalized is None:
        return None
    if tags_match == "all":
        return Memory.tags.contains(tags_normalized)
    return Memory.tags.overlap(tags_normalized)


def _list_window_filters(trigger_from: str | None, trigger_until: str | None) -> list[Any]:
    """Time Memory (#877) window-overlap predicates.

    A stored window [trigger_from, trigger_until] overlaps the query window
    [qfrom, quntil] iff ``trigger_from <= quntil AND trigger_until >= qfrom``.
    The columns are TEXT fixed-width ISO, so string comparison IS chronological
    comparison — but only if the caller's bounds are the same fixed-width form.
    ``parse_query_bound`` re-normalizes them (and resolves the ``now``
    shortcut); a malformed bound is a 422, not silently wrong results.

    ``isinstance(str)`` rather than ``is not None`` skips the unset ``Query()``
    FieldInfo sentinel that direct-call unit tests pass.

    Raises:
        HTTPException: 422 when a bound is not a parseable window bound.
    """
    from utils.time_trigger import TriggerValidationError, parse_query_bound

    try:
        qfrom = parse_query_bound(trigger_from) if isinstance(trigger_from, str) else None
        quntil = parse_query_bound(trigger_until) if isinstance(trigger_until, str) else None
    except TriggerValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    filters: list[Any] = []
    if qfrom is not None:
        filters.append(Memory.trigger_until >= qfrom)
    if quntil is not None:
        filters.append(Memory.trigger_from <= quntil)
    return filters


def _list_geo_filters(
    lat_min: float | None,
    lat_max: float | None,
    lon_min: float | None,
    lon_max: float | None,
) -> list[Any]:
    """WHERE-axis bbox predicates (#1334). Bounds may be one-sided.

    ``isinstance`` rather than ``is not None`` skips the unset ``Query()``
    FieldInfo sentinel that direct-call unit tests pass.
    """
    filters: list[Any] = []
    if isinstance(lat_min, int | float):
        filters.append(Memory.location_lat >= lat_min)
    if isinstance(lat_max, int | float):
        filters.append(Memory.location_lat <= lat_max)

    lon_lo = lon_min if isinstance(lon_min, int | float) else None
    lon_hi = lon_max if isinstance(lon_max, int | float) else None
    if lon_lo is not None and lon_hi is not None and lon_lo > lon_hi:
        # Antimeridian-crossing box (map viewport panned across ±180°): the
        # wrapped range is the union of the two edge ranges, same two-ranges-ORed
        # convention as utils.geo_location.bbox_lon_ranges (#1331). Without this
        # branch the AND of the two bounds is unsatisfiable and a valid viewport
        # silently returns empty.
        filters.append(or_(Memory.location_lon >= lon_lo, Memory.location_lon <= lon_hi))
    else:
        if lon_lo is not None:
            filters.append(Memory.location_lon >= lon_lo)
        if lon_hi is not None:
            filters.append(Memory.location_lon <= lon_hi)

    if filters:
        # Pair-completeness: a bbox-filtered result must contain only plottable
        # rows. A half-populated pair (raw-SQL writer artifact where the e69
        # regex guard NULLed one coordinate) would match a one-sided bound yet
        # serialize location=None — require both columns so filter and
        # serialization semantics agree (mirrors services/geo_memory.py's
        # explicit IS NOT NULL; it also keeps the query eligible for the partial
        # index, whose predicate covers lat and deleted_at only).
        filters.append(Memory.location_lat.is_not(None))
        filters.append(Memory.location_lon.is_not(None))
    return filters


def _list_memory_item(m: Memory) -> MemoryListItem:
    """Serialize one row for the list response.

    ``created_at`` / ``updated_at`` are stored naive UTC (DateTime without
    ``timezone=True``), so the serialized form is tagged ``Z`` — JS clients
    parse naive ISO as local time and would render JST-shifted timestamps.
    ``updated_at`` is nullable (set explicitly by edit paths; #1317 removed the
    column's ``onupdate``), so fresh rows fall back to ``created_at`` — the
    frontend renders both as the row "updated" timestamp.

    ``location`` (#1334) surfaces the generated columns so the UI can plot pins.
    A half-populated pair serializes as ``None`` rather than a partial point.
    """
    return MemoryListItem(
        id=str(m.id),
        summary=m.summary,
        type=m.type,
        scope=m.scope,
        importance=m.importance,
        created_at=to_utc_iso(m.created_at) or "",
        updated_at=to_utc_iso(m.updated_at or m.created_at) or "",
        location=(
            MemoryListItemLocation(lat=m.location_lat, lon=m.location_lon)
            if m.location_lat is not None and m.location_lon is not None
            else None
        ),
    )


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
    lat_min: float | None = Query(
        None,
        ge=-90,
        le=90,
        description="WHERE-axis (#1334) bbox lower latitude bound (degrees). "
        "Any bbox bound restricts results to memories with a complete "
        "location (generated columns from details.location); bounds may be "
        "one-sided.",
    ),
    lat_max: float | None = Query(None, ge=-90, le=90, description="Bbox upper latitude bound."),
    lon_min: float | None = Query(
        None,
        ge=-180,
        le=180,
        description="Bbox lower longitude bound. lon_min > lon_max selects the "
        "antimeridian-crossing (±180°) box: lon >= lon_min OR lon <= lon_max.",
    ),
    lon_max: float | None = Query(
        None,
        ge=-180,
        le=180,
        description="Bbox upper longitude bound (see lon_min for the antimeridian-crossing form).",
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
                user_id=user_id,
                context_id=context_id,
                key_workspace_id=user.get("api_key_workspace_id"),
            )
            owner_filter = user_id if context.is_private else None

        q_normalized, q_pattern = _list_q_pattern(q)
        tags_normalized = _list_tags(tags)

        # #1301: agent-binding read filter, SQL form (P0-2 context default-deny
        # + per-memory type/source subtraction). None for non-agent credentials
        # and shadow-mode scopes.
        binding_predicate = await binding_memory_sql_predicate(db)

        # ONE predicate list, applied to BOTH the page query and the count.
        # #1456: these used to be two hand-maintained sequences of ``if x:
        # query = query.where(...)`` / ``if x: count_query = ...``. They agreed,
        # but only by hand — and ``total`` must never act as an existence oracle
        # over rows the page filter excludes, nor pagination go inexact. Sharing
        # the list makes that structural instead of a review obligation (the
        # tag / window / geo blocks were already shared for exactly this reason;
        # this finishes the job).
        #
        # Soft-deleted rows are excluded: POST /forget sets ``deleted_at``
        # rather than removing the row, and the list must not surface tombstones.
        filters: list[Any] = [Memory.deleted_at.is_(None)]
        if binding_predicate is not None:
            filters.append(binding_predicate)
        if owner_filter is not None:
            filters.append(Memory.user_id == owner_filter)
        if scope:
            filters.append(Memory.scope == scope)
        if type:
            filters.append(Memory.type == type)
        if context_id is not None:
            filters.append(Memory.context_id == context_id)
        if q_pattern is not None:
            filters.append(Memory.summary.ilike(q_pattern, escape="\\"))
        tag_filter = _list_tag_filter(tags_normalized, tags_match)
        if tag_filter is not None:
            filters.append(tag_filter)
        filters.extend(_list_window_filters(trigger_from, trigger_until))
        filters.extend(_list_geo_filters(lat_min, lat_max, lon_min, lon_max))

        count_result = await db.execute(select(func.count(Memory.id)).where(*filters))
        total = count_result.scalar() or 0

        # order_by=trigger_from (#877) sorts ascending for upcoming-first Time
        # Memory listing; default stays created_at desc.
        order_clause = (
            Memory.trigger_from.asc() if order_by == "trigger_from" else Memory.created_at.desc()
        )
        result = await db.execute(
            select(Memory).where(*filters).order_by(order_clause).limit(limit).offset(offset)
        )
        memories = list(result.scalars().all())
        memory_items = [_list_memory_item(m) for m in memories]

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
    days: int = 30,
) -> dict[str, Any]:
    """Get memory access patterns and analytics for specified or current context.

    Issue #82: Now context-scoped - returns patterns for specified or current context.

    Returns most accessed memories, type distribution, and access timeline.

    Args:
        user: Authenticated user
        context_id: Optional context ID (defaults to current context)
        db: Database session
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
            # SECURITY (#1011 / #383 / #963): resolve via the shared
            # workspace-read chokepoint for uniform-404 disclosure AND API-key
            # workspace confinement — parity with /memory/stats and /memory/list.
            context = await PermissionService(db).resolve_context_for_workspace_read(
                user_id=user_id,
                context_id=target_context_id,
                key_workspace_id=user.get("api_key_workspace_id"),
            )
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

        # #1301: agent-binding read filter (context default-deny + type/source
        # subtraction) on the most-accessed page AND every aggregate — a
        # grouped count over denied rows is an existence oracle. None for
        # non-agent credentials and shadow-mode scopes.
        binding_predicate = await binding_memory_sql_predicate(db)
        if binding_predicate is not None:
            base_filter.append(binding_predicate)

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

    except (MemoryCloudException, HTTPException):
        # Uniform-404 authz denials (#1011) and explicit HTTP errors must
        # surface as-is, not be masked as a generic 500.
        raise
    except Exception as e:
        logger.error("access_patterns_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve access patterns",
        ) from e
