"""Memory service for business logic.

Orchestrates memory operations across PostgreSQL, Qdrant, and Redis.
Implements remember(), recall(), forget(), reference() APIs.
Issue #82: Context-based multi-collection support.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import math
import statistics
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.retention import should_promote_to_persistent
from db.qdrant import (
    add_memory_to_qdrant,
    delete_memory_from_qdrant,
    update_memory_payload_in_qdrant,
)
from models.auth import CONTEXT_TRUST_TIER_TRUSTED, Context
from models.memory import (
    DELIVERY_MODE_ALWAYS,
    EDGE_ORIGIN_DECLARED,
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    SOURCE_TYPE_CONNECTOR,
    SOURCE_TYPE_MANUAL,
    Memory,
)
from models.schemas import (
    ExploreHint,
    ExploreRequest,
    ExploreResponse,
    ForgetRequest,
    ForgetResponse,
    LinkedMemoryRef,
    LoadPinnedResponse,
    MemoryResponse,
    MemoryStatsResponse,
    PatchMemoryRequest,
    PinnedMemoryItem,
    RecallConfidence,
    RecallRequest,
    RecallResponse,
    ReferenceResponse,
    RelatedMemoryResponse,
    RelatedTagItem,
    RememberRequest,
    RememberResponse,
    UpdateMemoryRequest,
    UpdateMemoryResponse,
)
from repositories.memory import MemoryRepository
from services.context_routing import resolve_collection_name
from services.context_service import ContextService
from services.embedding_service import EmbeddingService
from services.query_router import classify_query
from services.recall_selection import (
    RecallSelectionConfig,
    RecallSelectionPlan,
    plan_recall_selection,
)
from services.search_service import SearchService
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import (
    MemoryGoneError,
    NotFoundException,
    QuotaExceededError,
    ValidationError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Issue #886: upper bound for the always-load cap, matching the REST schema's
# ``LoadPinnedRequest.cap`` le=1000. The MCP path sends the raw arg with no
# Pydantic validation, so the service clamps defensively (see _clamp_pinned_cap).
_PINNED_LOAD_CAP_MAX = 1000

# Issue #1052: recall-confidence thresholds for absence detection.
#
# The primary signal is PROMINENCE = (top_cosine - mean_background_cosine) /
# mean_background_cosine over the RAW (pre-normalization) semantic cosines of the
# candidate pool. Prominence is a ratio, so it is invariant to the multiplicative
# scale differences between embedding models — sidestepping the cross-model
# miscalibration that #1047 (deliberately) avoided with a fixed absolute cutoff.
#
# Calibrated against a live 398-memory benchmark (real embeddings, 2026-06-20):
#   PRESENT (on-topic) queries: prominence 0.58–0.95, top_cosine 0.89–0.96
#   ABSENT  (off-topic) queries: prominence 0.08–0.21, top_cosine 0.52–0.65
# Thresholds sit inside the empirical gap so neither class straddles a boundary.
# These are module constants (not per-context config) for now; a per-context
# rolling baseline (percentile) is the tracked next step if a model needs it.
_CONF_HIGH_PROMINENCE = 0.40
_CONF_MODERATE_PROMINENCE = 0.25
_CONF_LOW_PROMINENCE = 0.12
# A top cosine this high is a near-duplicate match for essentially any
# cosine-normalized model; floor it to at least "moderate" even when the pool is
# tightly clustered (low-spread model → small prominence) so a strong absolute
# match is never reported as absent.
_CONF_STRONG_ABS_COSINE = 0.85
# Absolute-cosine guards. Prominence is a ratio, so a weak top over an even
# weaker background can still inflate it; these floors keep a low-absolute hit
# out of high/moderate. They also serve as the bands when the background gives no
# usable relative frame (mean ≤ _CONF_BG_EPS — e.g. a model that drives unrelated
# content toward ~0 / negative cosine, where the ratio is meaningless and the raw
# cosine is the only signal). Deliberately permissive so a real match on a
# low-baseline model (e.g. text-embedding-3-small) still qualifies.
_CONF_ABS_MODERATE_COSINE = 0.50
_CONF_ABS_LOW_COSINE = 0.30
_CONF_BG_EPS = 1e-3


def _log_embedding_task_result(task: asyncio.Task, memory_id: str) -> None:
    """Done-callback for `process_pending_embedding` tasks (memory write paths).

    Promotes asyncio task failures into structured `error` log events so they
    surface in observability instead of being lost as unhandled task exceptions.
    Cancellation is logged at warn level — typically a shutdown signal.

    Used by every memory write path that fires `process_pending_embedding` as a
    fire-and-forget task: `remember`, `_update_in_place`, and `patch_memory`.
    Bind via `functools.partial(_log_embedding_task_result, memory_id=...)` so
    the callback receives a stable id even after the surrounding scope unwinds.
    """
    if task.cancelled():
        logger.warning("embedding_task_cancelled", memory_id=memory_id)
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "embedding_task_exception",
            memory_id=memory_id,
            error=str(exc),
            exc_info=exc,
        )


class MemoryService:
    """Memory service for core memory operations.

    Issue #82: Supports context-based multi-collection.
    """

    def __init__(self, db: AsyncSession):
        """Initialize memory service.

        Args:
            db: Database session
        """
        self.db = db
        self.memory_repo = MemoryRepository(db)
        self.embedding_service = EmbeddingService(db)
        self.search_service = SearchService(db)
        self.context_service = ContextService(db)

    async def _get_context_isolation_params(
        self,
        user_id: str,
        context_id: UUID | None,
        *,
        access: str = "read",
        key_workspace_id: UUID | None = None,
        operation: str | None = None,
    ) -> tuple[Context | None, str | None, str | None]:
        """Extract workspace_id and context_id for 3-level isolation (performance optimization).

        Single Collection Migration: Helper to avoid duplicate context fetches.

        Issue #1275 (RFC-0002 P0-2): this resolves the *declared* context for
        the remember (write) / load_pinned (read) / forget-by-context (write)
        paths — routes that do not pass through
        ``PermissionService.resolve_context_for_workspace_read``. Apply the
        same subtractive agent-binding gate here so agent-bound REST/MCP
        requests cannot write into (or read from) a binding-denied context via
        these paths. Callers on a WRITE path MUST pass ``access="write"``.
        No-op for non-agent credentials; deny raises the uniform
        ``NotFoundException("Context")`` matching ``get_context``.

        Issue #963 / #1281 item 2: also confine a workspace-scoped API key to
        its own workspace here. ``context_service.get_context`` authorizes on
        membership in the context's owning workspace — necessary but not
        sufficient for a workspace-scoped key whose holder is *also* a member of
        another workspace (they could otherwise pass a foreign ``context_id`` and,
        for remember, have the row stamped into that foreign workspace). REST
        passes ``key_workspace_id=user["api_key_workspace_id"]`` (the PURE key
        scope — None unless a workspace-scoped key was used); mismatch raises the
        same uniform ``NotFoundException``. Mirrors the MCP-path
        ``_resolve_context`` confinement. No-op for OAuth/session/global-key
        callers (scope None), so it never over-confines them.

        Args:
            user_id: User ID
            context_id: Context ID
            access: ``"read"`` (default) or ``"write"`` — the binding gate.
            key_workspace_id: Pure API-key workspace scope for #963 confinement,
                or ``None`` to skip it.
            operation: MAE operation vocabulary value threaded into the
                binding gate for #1286 deny capture, or ``None`` (log-only).

        Returns:
            Tuple of (context_object, workspace_id_str, context_id_str)

        Raises:
            NotFoundException: If context not found, key-workspace-confined, or
                the binding denies it.
        """
        if not context_id:
            return None, None, None

        context = await self.context_service.get_context(user_id, context_id)

        if key_workspace_id is not None and context.workspace_id != key_workspace_id:
            raise NotFoundException("Context", str(context_id))

        from services.agent_binding_service import agent_binding_permits

        if not await agent_binding_permits(
            self.db, context_id, access, operation=operation, user_id=user_id
        ):
            raise NotFoundException("Context", str(context_id))

        return context, str(context.workspace_id), str(context_id)

    @staticmethod
    def _apply_pin_on_write(memory: Memory) -> None:
        """Pin a memory to persistent when delivery_mode='always' (Issue #886).

        Centralizes the pin-on-write rule shared by ``remember`` and
        ``_update_in_place`` (and any future write path), mirroring how
        ``_apply_time_trigger`` centralizes the Time Memory invariant — so the
        two sites cannot drift. Idempotent and matches ``promote_to_persistent``
        semantics (scope='persistent' + promoted_at stamped); a memory already
        persistent keeps its original promoted_at.
        """
        if memory.delivery_mode == DELIVERY_MODE_ALWAYS and memory.scope != "persistent":
            memory.scope = "persistent"
            memory.promoted_at = utcnow()

    @staticmethod
    def _clamp_pinned_cap(cap: int | str | None, default: int) -> int:
        """Coerce + bound the always-load cap (Issue #886).

        The REST path validates ``cap`` via Pydantic (int, 1..1000), but the MCP
        path forwards the raw tool arg, so the service is the shared chokepoint
        that must defend the LIMIT: ``None`` → default; otherwise coerce to int
        and clamp to [1, _PINNED_LOAD_CAP_MAX]. Clamping the lower bound to 1 is
        load-bearing — a 0 would emit ``LIMIT 0`` (empty set + a false
        truncated=true) and a negative would emit ``LIMIT -1`` (no cap at all,
        defeating the safety valve).
        """
        if cap is None:
            return default
        try:
            cap_int = int(cap)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"cap must be an integer, got {cap!r}") from exc
        return max(1, min(cap_int, _PINNED_LOAD_CAP_MAX))

    @staticmethod
    def _apply_time_trigger(memory_type: str | None, details: dict | None) -> dict | None:
        """Validate + normalize a Time Memory trigger window (Issue #877).

        When ``memory_type == "time"``, ``details.trigger`` must carry caller-
        resolved Y/M/D[/H/M] components; this derives the canonical ``from``/
        ``until`` window so the generated ``trigger_from``/``trigger_until``
        columns index it. For any other type, ``details`` is returned unchanged.

        Centralized here so every write path (remember, _update_in_place,
        patch_memory) enforces the same invariant — a ``type="time"`` row that
        skips normalization would store NULL trigger columns and become
        invisible to recall_upcoming and the trigger_from window filter.

        Raises:
            ValueError: if a time memory has no trigger or an invalid one (the
                established "bad request" signal inside MemoryService).
        """
        if memory_type != "time":
            return details

        from utils.time_trigger import TriggerValidationError, normalize_trigger

        trigger = (details or {}).get("trigger")
        if trigger is None:
            raise ValueError("type='time' requires details.trigger with at least {'year': ...}")
        try:
            normalized = normalize_trigger(trigger)
        except TriggerValidationError as exc:
            raise ValueError(f"invalid details.trigger: {exc}") from exc
        return {**(details or {}), "trigger": normalized}

    @staticmethod
    def _apply_location(details: dict | None) -> dict | None:
        """Validate + normalize ``details.location`` (WHERE axis, #1331).

        ``_apply_time_trigger``'s sibling, with one deliberate difference:
        the gate is ORTHOGONAL — it fires on the ``location`` key's presence,
        not on a memory type (any type may carry a place). Centralized so
        every write path that accepts caller-supplied details (remember,
        _update_in_place, patch_memory) enforces the same contract — an
        unvalidated location would store NULL generated columns and be
        invisible to recall_nearby, the time axis' identical trap.

        Callers apply this ONLY to caller-supplied details: an update/patch
        that does not touch ``details`` must not 422 on a legacy row whose
        stored ``location`` shape predates the contract (such rows simply
        keep NULL generated columns).

        Raises:
            ValueError: malformed details.location (the established "bad
                request" signal inside MemoryService).
        """
        from utils.geo_location import LocationValidationError, normalize_location

        try:
            return normalize_location(details)
        except LocationValidationError as exc:
            raise ValueError(f"invalid details.location: {exc}") from exc

    async def remember(
        self,
        request: RememberRequest,
        user_id: str,
        client: str = "unknown",
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,  # NEW: Workspace ID (Issue #146)
        key_workspace_id: UUID | None = None,  # Issue #963/#1281: pure key scope
    ) -> RememberResponse:
        """Store new memory.

        Issue #1 specification:
        - Layer 1: summary (Embedding化)
        Issue #146: Workspace-scoped API keys support
        - Layer 2: context_summary
        - Layer 3: content + details
        - Qdrant + PostgreSQL dual storage

        Issue #82: Context-based multi-collection support.

        Args:
            request: Remember request
            user_id: User ID
            client: Client name
            current_context_id: Current context UUID (Issue #82)

        Returns:
            RememberResponse with memory_id and scope

        Example:
            >>> result = await service.remember(request, "kiyota", "claude-sonnet-4")
            >>> result.memory_id
            UUID('...')
            >>> result.scope
            'working'
        """
        # Issue #149: Check quota before creating memory
        # Single Collection Migration: Memory count only (storage size removed)
        if current_workspace_id:
            from services.quota_service import QuotaService

            quota_service = QuotaService(self.db)

            # Check memory quota (count-based only)
            can_create, error = await quota_service.check_memory_quota(
                current_workspace_id, raise_on_exceeded=True
            )

        # Single Collection Migration: Extract isolation params (optimized).
        # Issue #1275: remember is a WRITE — gate the declared context against
        # the agent binding (no-op for non-agent credentials).
        context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id,
            current_context_id,
            access="write",
            key_workspace_id=key_workspace_id,
            operation="remember",  # #1286 (P0-5): deny-capture audit identity
        )

        # Validate required parameters
        if not workspace_id_str or not context_id_str:
            raise ValueError("remember() requires current_context_id")

        # Issue #273: Validate content size to prevent DoS attacks
        # Without storage quota, we enforce per-memory size limit
        from config.constants import MAX_CONTENT_SIZE

        content_size = (
            len(request.summary or "")
            + len(request.context_summary or "")
            + len(request.content or "")
            + len(str(request.details or ""))
        )
        if content_size > MAX_CONTENT_SIZE:
            from utils.exceptions import QuotaExceededError

            raise QuotaExceededError(
                f"Memory size {content_size:,} bytes exceeds limit {MAX_CONTENT_SIZE:,} bytes (1MB). "
                f"Please reduce content size or split into multiple memories."
            )

        # Create memory ID first
        memory_id = uuid4()

        # ============================================================================
        # BUG FIX #122-3: Transaction integrity for PostgreSQL/Qdrant operations
        # ============================================================================
        # Problem: Previous implementation was:
        #   1. Generate embedding
        #   2. Save to Qdrant
        #   3. Save to PostgreSQL
        #   4. Commit
        # If step 2 failed silently, memory existed in PostgreSQL but not in Qdrant,
        # making it unreachable via recall() but accessible via reference().
        #
        # Solution: Use embedding_status to track state and ensure atomicity:
        #   1. Save to PostgreSQL with embedding_status='pending'
        #   2. Generate embedding
        #   3. Save to Qdrant
        #   4. Update embedding_status='success'
        #   5. Commit
        # On any failure, rollback and set embedding_status='failed' with error.
        # ============================================================================

        # Issue #163: Normalize searchable text fields for consistent search
        # NFKC: "ｶﾀｶﾅ" (half-width) → "カタカナ" (full-width)
        # NFC: か + ゛ → が (composed form)
        # Only summary and context_summary are normalized (used in fulltext search)
        # content and details are kept as-is since they store full original data
        from utils.text import normalize_for_search

        normalized_summary = normalize_for_search(request.summary)
        normalized_context_summary = normalize_for_search(request.context_summary)

        # Time Memory (Issue #877): when type="time", the caller (assistant)
        # supplies resolved Y/M/D[/H/M] components in details.trigger. Validate
        # them and derive the [from, until] window written back into details so
        # the generated columns trigger_from / trigger_until can index it. No LLM
        # here — the caller already resolved any relative phrasing ("来週")
        # against its own clock. Centralized in _apply_time_trigger so the
        # update/patch paths enforce the same invariant (see those methods).
        request.details = self._apply_time_trigger(request.type, request.details)
        # WHERE axis (#1331): validate/normalize details.location (orthogonal
        # gate — fires on key presence for any type).
        request.details = self._apply_location(request.details)

        # Create memory entity first with pending status
        memory = Memory(
            id=memory_id,
            user_id=user_id,
            workspace_id=UUID(workspace_id_str)
            if workspace_id_str
            else None,  # Migration 063: 3-level isolation
            context_id=UUID(context_id_str)
            if context_id_str
            else None,  # Migration 063: 3-level isolation
            summary=normalized_summary,  # Issue #163: Normalized for search
            context_summary=normalized_context_summary,  # Issue #163: Normalized for search
            content=request.content,  # Keep original
            details=request.details,  # Keep original
            type=request.type,
            importance=request.importance,
            tags=request.tags,
            context=request.context,
            scope="working",
            delivery_mode=request.delivery_mode,  # Issue #886
            client=client,
            summary_embedding_id=memory_id,  # Same as memory_id
            embedding_status="pending",  # Issue #122: Track embedding state
            source_uri=request.source_uri,
            # Issue #887: server-authoritative provenance. The request schema
            # only permits user-origin values (file/url/vault/api/manual) — the
            # reserved 'connector' value cannot be client-set, so a caller on the
            # remember path can never forge external-ingestion provenance.
            # NULL coerces to 'manual' to satisfy the NOT NULL column.
            source_type=request.source_type or SOURCE_TYPE_MANUAL,
        )
        # Issue #886: pin-on-write. delivery_mode='always' pins straight to
        # persistent (so it is exempt from sleep consolidation — which only acts
        # on scope='working' — without waiting for a consolidation pass).
        self._apply_pin_on_write(memory)

        try:
            # Save to PostgreSQL first (with pending status)
            await self.memory_repo.create(memory)
            await self.db.commit()

            logger.info(
                "memory_created_pending",
                memory_id=str(memory_id),
                user_id=user_id,
                type=request.type,
            )

            # Issue #215: Create declared_link edges (best-effort, after commit)
            await self._create_declared_links(
                memory_id=memory_id,
                request=request,
                user_id=user_id,
                workspace_id=workspace_id_str,
                context_id=context_id_str,
            )

            task = asyncio.create_task(process_pending_embedding(memory_id))
            task.add_done_callback(
                functools.partial(_log_embedding_task_result, memory_id=str(memory_id))
            )

            # #1278/#1281 item 7: audit the write (no-op unless verified agent).
            from services.memory_access_event_writer import emit_memory_access_event

            await emit_memory_access_event(
                operation="remember",
                outcome="success",
                workspace_id=UUID(workspace_id_str),
                user_id=user_id,
                context_id=UUID(context_id_str),
                memory_id=memory_id,
            )

            return RememberResponse(memory_id=memory_id, scope=memory.scope)

        except Exception as e:
            await self.db.rollback()
            logger.error(
                "memory_creation_failed",
                memory_id=str(memory_id),
                user_id=user_id,
                error=str(e),
            )
            raise

    async def update_memory(
        self,
        request: UpdateMemoryRequest,
        user_id: str,
        client: str = "unknown",
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
    ) -> UpdateMemoryResponse:
        """Update existing memory in-place or upsert by external_id.

        Issue #80: Two modes:
        1. In-place (memory_id): preserves ID, graph edges, created_at
        2. Upsert (external_id): forget + remember internally

        Args:
            request: Update request
            user_id: User ID
            client: Client name
            current_context_id: Context UUID
            current_workspace_id: Workspace UUID

        Returns:
            UpdateMemoryResponse
        """
        if request.external_id:
            return await self._upsert_by_external_id(
                request, user_id, client, current_context_id, current_workspace_id
            )

        return await self._update_in_place(
            request, user_id, current_context_id, current_workspace_id
        )

    async def _update_in_place(
        self,
        request: UpdateMemoryRequest,
        user_id: str,
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
    ) -> UpdateMemoryResponse:
        """In-place update by memory_id."""
        from config.constants import MAX_CONTENT_SIZE
        from services.permission_service import PermissionService
        from utils.text import normalize_for_search

        # #1316: repo.get() excludes soft-deleted rows by default, so a
        # tombstoned memory is uniformly "not found" here.
        memory = await self.memory_repo.get(request.memory_id)
        if not memory:
            raise NotFoundException("Memory", str(request.memory_id))

        # Permission check
        from services.permission_service import CallerId, MemoryAuthorId

        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(memory.user_id),
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
            access="write",  # #1275: WRITE path — can_read-only binding must not permit mutation
            operation="update",  # #1286 (P0-5): deny-capture audit identity
            memory_id=request.memory_id,
        )
        if not can_access:
            raise NotFoundException("Memory", str(request.memory_id))

        # Pre-compute normalized values (avoid double normalization)
        normalized_summary = (
            normalize_for_search(request.summary) if request.summary is not None else None
        )
        normalized_ctx_summary = (
            normalize_for_search(request.context_summary)
            if request.context_summary is not None
            else None
        )

        # Determine what changed (BM25 tokens depend on summary, context_summary, content)
        needs_reembed = False
        if normalized_summary is not None and normalized_summary != memory.summary:
            needs_reembed = True
        if normalized_ctx_summary is not None and normalized_ctx_summary != memory.context_summary:
            needs_reembed = True
        if request.content is not None and request.content != memory.content:
            needs_reembed = True

        # Validate content size
        content_size = (
            len(request.summary or memory.summary or "")
            + len(request.context_summary or memory.context_summary or "")
            + len(request.content or memory.content or "")
            + len(str(request.details or memory.details or ""))
        )
        if content_size > MAX_CONTENT_SIZE:
            from utils.exceptions import QuotaExceededError

            raise QuotaExceededError(
                f"Memory size {content_size:,} bytes exceeds limit {MAX_CONTENT_SIZE:,} bytes (1MB)."
            )

        # Apply field updates (only non-None fields)
        if normalized_summary is not None:
            memory.summary = normalized_summary
        if normalized_ctx_summary is not None:
            memory.context_summary = normalized_ctx_summary
        if request.content is not None:
            memory.content = request.content
        # Time Memory (#877): normalize the trigger against the *effective* type
        # and details after this update, so changing details.trigger (or flipping
        # type to "time") on an existing memory re-derives the from/until window
        # the generated columns index — the same invariant remember() enforces.
        effective_type = request.type if request.type is not None else memory.type
        effective_details = request.details if request.details is not None else memory.details
        effective_details = self._apply_time_trigger(effective_type, effective_details)
        # WHERE axis (#1331): only caller-supplied details are validated — an
        # importance-only update must not 422 on a legacy row whose stored
        # location predates the contract (its generated columns are NULL).
        if request.details is not None:
            effective_details = self._apply_location(effective_details)
        if request.details is not None or effective_type == "time":
            memory.details = effective_details
        if request.type is not None:
            memory.type = request.type
        if request.importance is not None:
            memory.importance = request.importance
        if request.tags is not None:
            memory.tags = request.tags
        if request.context is not None:
            memory.context = request.context
        # Issue #886: in-place delivery_mode change. Setting 'always' pins to
        # persistent via the shared helper (mirrors remember's pin-on-write);
        # other modes leave scope untouched so unpinning ('always' → 'on_recall')
        # keeps the memory persistent — delivery_mode controls loading, scope
        # controls lifecycle.
        if request.delivery_mode is not None:
            memory.delivery_mode = request.delivery_mode
            self._apply_pin_on_write(memory)

        memory.updated_at = utcnow()

        if needs_reembed:
            # Async embedding via create_task (same pattern as remember)
            memory.embedding_status = "pending"
            await self.db.flush()
            await self.db.commit()

            task = asyncio.create_task(process_pending_embedding(memory.id))
            task.add_done_callback(
                functools.partial(_log_embedding_task_result, memory_id=str(memory.id))
            )
        else:
            # Metadata-only update: patch Qdrant payload without re-embedding
            payload_updates: dict = {}
            if request.tags is not None:
                payload_updates["tags"] = request.tags
            if request.importance is not None:
                payload_updates["importance"] = request.importance
            if request.type is not None:
                payload_updates["type"] = request.type
            if normalized_ctx_summary is not None:
                payload_updates["context_summary"] = normalized_ctx_summary

            if payload_updates:
                # Sync updated_at to Qdrant for date range filtering (Issue #78)
                payload_updates["updated_at"] = to_utc_iso(utcnow())
                collection = await resolve_collection_name(self.db, memory.context_id)
                await update_memory_payload_in_qdrant(
                    memory_id=memory.id,
                    payload_updates=payload_updates,
                    collection_name=collection,
                )

            await self.db.commit()

        logger.info(
            "memory_updated",
            memory_id=str(memory.id),
            user_id=user_id,
            re_embedded=needs_reembed,
        )

        # #1286 item 2 (P0-5): audit the write (no-op unless verified agent).
        from services.memory_access_event_writer import emit_memory_access_event

        await emit_memory_access_event(
            operation="update",
            outcome="success",
            workspace_id=memory.workspace_id,
            user_id=user_id,
            context_id=memory.context_id,
            memory_id=memory.id,
        )

        return UpdateMemoryResponse(
            memory_id=memory.id,
            operation="updated",
            re_embedded=needs_reembed,
            scope=memory.scope,
        )

    async def patch_memory(
        self,
        memory_id: UUID,
        request: PatchMemoryRequest,
        user_id: str,
    ) -> ReferenceResponse:
        """Partial update of a memory by UUID (Issue #439).

        Mirrors `_update_in_place` but with four #439-specific behaviors:

        - Soft-deleted memories raise ``MemoryGoneError`` so the route can
          return 410 (vs `_update_in_place`'s 404 NotFound, which hides the
          tombstone). Distinguishing 410 from 404 lets clients detect a
          known-but-deleted memory and stop retrying.
        - Permission denial returns 404 (not silent 200, unlike forget) so
          UUID existence is not leaked.
        - When ``summary`` or ``content`` changes, neural edges anchored on
          this memory are invalidated (forget's pattern). Edge weights were
          computed against the previous embedding; the next sleep run rebuilds
          them against the new vector.
        - Qdrant payload-only updates run AFTER the PG commit and only emit a
          structured error log on failure (drift visibility, no rollback). The
          re-embed path keeps the existing async-task pattern, so qdrant
          errors there continue to be surfaced via `_log_embedding_task_result`.

        Field-presence semantics: ``request.model_fields_set`` (the set of
        fields the client EXPLICITLY sent, including those set to ``None``)
        distinguishes "field omitted" from "field explicitly null". This
        matters for ``details`` — sending ``{"details": null}`` clears the
        existing JSON, while omitting ``details`` preserves it. ``tags``
        follows the same omit/clear contract (None/missing = preserve,
        [] = clear, [...] = replace), enforced by the schema's null-reject
        validator. ``model_fields_set`` is preferred over
        ``model_dump(exclude_unset=True)`` because the latter deep-serializes
        the full request body (including ``details`` JSON) just to extract
        a key set — wasteful for our use case.

        Returns the updated memory as ``ReferenceResponse`` (full detail). The
        response is constructed inline from the in-scope ORM object to avoid
        a second permission check + extra commit round-trip that delegating
        to ``self.reference()`` would incur (and PATCH should not bump
        access-stats — that semantic only fits read paths).
        """
        from services.permission_service import (
            CallerId,
            MemoryAuthorId,
            PermissionService,
        )
        from utils.text import normalize_for_search

        # #1316: patch is the ONE by-id path that needs tombstones — its #439
        # contract returns 410 MemoryGoneError to authorized callers below.
        memory = await self.memory_repo.get(memory_id, include_deleted=True)
        if not memory:
            raise NotFoundException("Memory", str(memory_id))

        # Permission check BEFORE soft-delete check: a non-member must not be
        # able to distinguish a soft-deleted memory (would-be 410) from a
        # never-existed UUID (404). Returning 410 first would let an attacker
        # who guesses (or harvests) a UUID confirm "this memory was once
        # real" — meaningful for GDPR-style "we used to have a record about
        # you" leaks. 410 is reserved for authorized callers who need to
        # distinguish tombstones from never-existed.
        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(memory.user_id),
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
            access="write",  # #1275: WRITE path — can_read-only binding must not permit mutation
            operation="update",  # #1286 (P0-5): deny-capture audit identity
            memory_id=memory_id,
        )
        if not can_access:
            raise NotFoundException("Memory", str(memory_id))

        if memory.deleted_at is not None:
            raise MemoryGoneError("Memory", str(memory_id))

        # `model_fields_set` is the set of field names the client EXPLICITLY
        # sent (including those set to None), so `{"details": null}` puts
        # "details" in the set and a body that simply omits `details` does
        # not. Cheaper than `model_dump(exclude_unset=True)` for large
        # `details` payloads — no deep serialization, just a name set.
        provided_fields = request.model_fields_set

        normalized_summary = (
            normalize_for_search(request.summary) if "summary" in provided_fields else None
        )

        needs_reembed = False
        if normalized_summary is not None and normalized_summary != memory.summary:
            needs_reembed = True
        if "content" in provided_fields and request.content != memory.content:
            needs_reembed = True

        # Skip the size guard on metadata-only patches: `tags`/`importance`/`type`
        # cannot move the row across the byte limit, so the four `len()` calls
        # are pure waste on the most common PATCH shape.
        if {"summary", "content", "details"} & provided_fields:
            from config.constants import MAX_CONTENT_SIZE

            # Compute the post-patch size from the would-be values. Use
            # explicit `is None` rather than truthy fallback so empty-but-
            # provided values like `details = {}` count as themselves
            # (`len("{}") = 2`) instead of being collapsed to 0.
            next_summary = normalized_summary if "summary" in provided_fields else memory.summary
            next_content = request.content if "content" in provided_fields else memory.content
            next_details = request.details if "details" in provided_fields else memory.details

            content_size = (
                len(next_summary if next_summary is not None else "")
                + len(memory.context_summary if memory.context_summary is not None else "")
                + len(next_content if next_content is not None else "")
                + len(str(next_details) if next_details is not None else "")
            )
            if content_size > MAX_CONTENT_SIZE:
                raise QuotaExceededError(
                    f"Memory size {content_size:,} bytes exceeds limit "
                    f"{MAX_CONTENT_SIZE:,} bytes (1MB)."
                )

        if normalized_summary is not None:
            memory.summary = normalized_summary
        # content / type / importance are NOT NULL on the Memory model; silently
        # skip an explicit null in the payload (Mapped[str|float] rejects None).
        if "content" in provided_fields and request.content is not None:
            memory.content = request.content
        if "type" in provided_fields and request.type is not None:
            memory.type = request.type
        if "importance" in provided_fields and request.importance is not None:
            memory.importance = request.importance
        if "tags" in provided_fields:
            memory.tags = request.tags
        # Time Memory (#877): normalize the trigger against the effective type/
        # details after this patch (mirrors remember/_update_in_place), so a
        # PATCH that touches details.trigger or flips type to "time" still
        # populates the generated trigger_from/trigger_until columns.
        effective_type = (
            request.type
            if ("type" in provided_fields and request.type is not None)
            else memory.type
        )
        effective_details = request.details if "details" in provided_fields else memory.details
        effective_details = self._apply_time_trigger(effective_type, effective_details)
        # WHERE axis (#1331): only caller-supplied details are validated
        # (mirrors _update_in_place — untouched legacy rows must not 422).
        if "details" in provided_fields:
            effective_details = self._apply_location(effective_details)
        if "details" in provided_fields or effective_type == "time":
            # Explicit null clears the column; non-null replaces it. A
            # type="time" patch always (re)writes the normalized details.
            memory.details = effective_details

        memory.updated_at = utcnow()

        memory_workspace_id = str(memory.workspace_id) if memory.workspace_id else None
        memory_context_id = str(memory.context_id) if memory.context_id else None

        if needs_reembed:
            memory.embedding_status = "pending"
            await self.db.flush()
            await self.db.commit()

            task = asyncio.create_task(process_pending_embedding(memory.id))
            task.add_done_callback(
                functools.partial(_log_embedding_task_result, memory_id=str(memory.id))
            )

            # Invalidate neural edges. Sleep run rebuilds against the new
            # vector. NULL workspace/context skips invalidation but emits a
            # warning so operators can spot pre-Migration-063 rows that drift
            # silently.
            #
            # `NeuralEdgeRepository.delete_node_edges` issues a SQL DELETE via
            # `self.db.execute(...)` but does NOT commit internally — without
            # an explicit commit here the DELETE would never persist (the
            # memory commit above closed the prior transaction; the DELETE
            # lands in a fresh implicit transaction that is discarded on
            # session close). Best-effort: commit on success, rollback on
            # failure so the (incomplete) DELETE doesn't leak into the next
            # statement on this session.
            if memory_workspace_id and memory_context_id:
                from repositories.neural_edge import NeuralEdgeRepository

                edge_repo = NeuralEdgeRepository(self.db)
                try:
                    edges_deleted = await edge_repo.delete_node_edges(
                        user_id=user_id,
                        node_id=memory.id,
                        workspace_id=memory_workspace_id,
                        context_id=memory_context_id,
                    )
                    await self.db.commit()
                    if edges_deleted > 0:
                        logger.info(
                            "memory_patch_edges_invalidated",
                            memory_id=str(memory.id),
                            edges_deleted=edges_deleted,
                            user_id=user_id,
                        )
                except Exception as exc:  # noqa: BLE001
                    await self.db.rollback()
                    logger.error(
                        "memory_patch_edge_invalidation_failed",
                        memory_id=str(memory.id),
                        user_id=user_id,
                        error=str(exc),
                        exc_info=exc,
                    )
            else:
                logger.warning(
                    "memory_patch_edges_invalidation_skipped",
                    memory_id=str(memory.id),
                    user_id=user_id,
                    reason="missing_workspace_or_context_id",
                )
        else:
            # Metadata-only path: PG commit first, qdrant after. Qdrant
            # failure is logged (not raised) for drift visibility — the PG
            # write stays durable. Forget uses single-commit-at-end; this
            # deviation is by Issue #439's design.
            await self.db.flush()
            await self.db.commit()

            payload_updates: dict[str, object] = {}
            if "tags" in provided_fields:
                payload_updates["tags"] = request.tags
            # Mirror the PG-side None-guard above: explicit-null for
            # NOT-NULL columns (importance/type) is silently skipped on
            # both sides to keep PG ↔ Qdrant payloads consistent.
            if "importance" in provided_fields and request.importance is not None:
                payload_updates["importance"] = request.importance
            if "type" in provided_fields and request.type is not None:
                payload_updates["type"] = request.type

            if payload_updates:
                payload_updates["updated_at"] = to_utc_iso(utcnow())
                try:
                    collection = await resolve_collection_name(self.db, memory.context_id)
                    await update_memory_payload_in_qdrant(
                        memory_id=memory.id,
                        payload_updates=payload_updates,
                        collection_name=collection,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "memory_patch_qdrant_payload_failed",
                        memory_id=str(memory.id),
                        user_id=user_id,
                        error=str(exc),
                        exc_info=exc,
                    )

        logger.info(
            "memory_patched",
            memory_id=str(memory.id),
            user_id=user_id,
            re_embedded=needs_reembed,
        )

        # #1286 item 2 (P0-5): audit the write (no-op unless verified agent).
        # PATCH is the REST-side update surface (#439); MCP's is
        # `_update_in_place` — both emit operation="update" so the audit
        # vocabulary stays unified across surfaces (#1291/#1292 parity).
        from services.memory_access_event_writer import emit_memory_access_event

        await emit_memory_access_event(
            operation="update",
            outcome="success",
            workspace_id=memory.workspace_id,
            user_id=user_id,
            context_id=memory.context_id,
            memory_id=memory.id,
        )

        # Build the response inline from the in-scope ORM row. Delegating to
        # ``self.reference(...)`` here would re-fetch the row, re-run the
        # permission check, bump access-stats (a write — wrong for PATCH),
        # and issue a second commit. We still want the declared_link refs,
        # which `_fetch_declared_link_refs` provides without those side
        # effects. NULL workspace_id/context_id rows skip the link fetch and
        # return empty link arrays (the helper's signature requires non-null
        # FKs).
        if memory.workspace_id and memory.context_id:
            (
                outgoing_links,
                outgoing_has_more,
                incoming_links,
                incoming_has_more,
            ) = await self._fetch_declared_link_refs(
                memory_id=memory.id,
                workspace_id=memory.workspace_id,
                context_id=memory.context_id,
            )
        else:
            outgoing_links = []
            outgoing_has_more = False
            incoming_links = []
            incoming_has_more = False

        return ReferenceResponse(
            memory_id=memory.id,
            summary=memory.summary,
            context_summary=memory.context_summary,
            content=memory.content,
            details=memory.details,
            type=memory.type,
            scope=memory.scope,
            importance=memory.importance,
            tags=memory.tags or [],
            context=memory.context,
            created_at=memory.created_at,
            updated_at=memory.updated_at or memory.created_at,
            client=memory.client,
            source_uri=memory.source_uri,
            source_type=memory.source_type,
            outgoing_links=outgoing_links,
            outgoing_has_more=outgoing_has_more,
            incoming_links=incoming_links,
            incoming_has_more=incoming_has_more,
        )

    async def _upsert_by_external_id(
        self,
        request: UpdateMemoryRequest,
        user_id: str,
        client: str = "unknown",
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
    ) -> UpdateMemoryResponse:
        """Upsert by external_id: remember new, then forget old if exists."""
        existing = await self.memory_repo.get_by_resource_id(
            resource_id=request.external_id,
            context_id=current_context_id,
            user_id=user_id,
        )

        # Build details with resource_id preserved (copy to avoid mutating request)
        details = {**(request.details or {}), "resource_id": request.external_id}

        # Create new memory first (before deleting old — prevents data loss on failure)
        remember_request = RememberRequest(
            summary=request.summary,
            context_summary=request.context_summary,
            content=request.content,
            details=details,
            type=request.type,
            importance=request.importance if request.importance is not None else 0.5,
            tags=request.tags or [],
            context=request.context,
        )

        # #1286 (P0-5) audit note: the upsert path intentionally emits NO
        # operation="update" event — the inner remember()/forget() calls each
        # emit their own row, which describes exactly what happened
        # physically (a new row created, the old row soft-deleted).
        result = await self.remember(
            remember_request,
            user_id=user_id,
            client=client,
            current_context_id=current_context_id,
            current_workspace_id=current_workspace_id,
        )

        # Only forget old memory after new one is successfully created
        operation = "created"
        if existing:
            await self.forget(
                ForgetRequest(memory_id=existing.id),
                user_id=user_id,
                current_context_id=current_context_id,
            )
            operation = "replaced"

        return UpdateMemoryResponse(
            memory_id=result.memory_id,
            operation=operation,
            re_embedded=True,
            scope=result.scope,
        )

    async def reference(self, memory_id: UUID, user_id: str) -> ReferenceResponse:
        """Get full memory details (Layer 3).

        Args:
            memory_id: Memory UUID
            user_id: User ID (for access control)

        Returns:
            ReferenceResponse with full details

        Raises:
            NotFoundException: If memory not found
        """
        # #1316: repo.get() excludes soft-deleted rows by default — a forgotten
        # memory must not be readable by direct-id fetch during the retention
        # window, and a tombstone is indistinguishable from never-existed.
        memory = await self.memory_repo.get(memory_id)

        if not memory:
            raise NotFoundException("Memory", str(memory_id))

        # Issue #XXX: Team collaboration - verify access permission
        from services.permission_service import CallerId, MemoryAuthorId, PermissionService

        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(memory.user_id),
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
            operation="reference",  # #1286 (P0-5): deny-capture audit identity
            memory_id=memory_id,
            memory_type=memory.type,  # #1299: per-memory type/source filter
            memory_source_type=memory.source_type,
        )

        if not can_access:
            raise NotFoundException("Memory", str(memory_id))

        # Snapshot ``updated_at`` before bumping access stats. #1317 removed
        # the column's ``onupdate=func.now()``, so ``update_access_stats`` no
        # longer touches ``updated_at`` at all — the DB value is now correct
        # by construction. The snapshot is retained as a cheap guard against
        # any future in-session dirtying, and reading it here keeps the
        # semantics explicit: an access bump is not a meaningful edit, so the
        # dialog's "Updated At" reflects the last real change, not "now".
        snapshot_updated_at = memory.updated_at or memory.created_at

        # Update access stats. reference() is the canonical *adoption* signal
        # (#1046): the agent deliberately fetched Layer-3 detail, so this bumps
        # reference_count in addition to access_count. Surfacing call sites
        # (recall return, explore spread) below leave count_as_adoption False.
        await self.memory_repo.update_access_stats(memory_id, client="api", count_as_adoption=True)
        await self.db.commit()

        logger.info("memory_referenced", memory_id=str(memory_id), user_id=user_id)

        # #1278/#1281 item 7: audit the Layer-3 adoption read (no-op unless
        # verified agent identity).
        from services.memory_access_event_writer import emit_memory_access_event

        await emit_memory_access_event(
            operation="reference",
            outcome="success",
            workspace_id=memory.workspace_id,
            user_id=user_id,
            context_id=memory.context_id,
            memory_id=memory_id,
        )

        # Issue #440: Fetch declared_link references for the dialog References
        # section. The edge invariant (`_validate_edge_context_invariant` in
        # repositories/neural_edge.py) guarantees both endpoints share the same
        # (workspace_id, context_id) as the source memory, so the access check
        # above already covers them — no per-edge permission re-check needed.
        # The bulk re-scope below is defense-in-depth, mirroring the pattern in
        # routes/graph.py:284-291: it filters out soft-deleted or invariant-
        # violating rows even before AC enforcement is universal.
        (
            outgoing_links,
            outgoing_has_more,
            incoming_links,
            incoming_has_more,
        ) = await self._fetch_declared_link_refs(
            memory_id=memory.id,
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
        )

        return ReferenceResponse(
            memory_id=memory.id,
            summary=memory.summary,
            context_summary=memory.context_summary,
            content=memory.content,
            details=memory.details,
            type=memory.type,
            scope=memory.scope,
            importance=memory.importance,
            tags=memory.tags or [],
            context=memory.context,
            created_at=memory.created_at,
            updated_at=snapshot_updated_at,
            client=memory.client,
            source_uri=memory.source_uri,
            source_type=memory.source_type,
            outgoing_links=outgoing_links,
            outgoing_has_more=outgoing_has_more,
            incoming_links=incoming_links,
            incoming_has_more=incoming_has_more,
        )

    async def _fetch_declared_link_refs(
        self,
        memory_id: UUID,
        workspace_id: UUID,
        context_id: UUID,
    ) -> tuple[list[LinkedMemoryRef], bool, list[LinkedMemoryRef], bool]:
        """Fetch declared_link edges and resolve them to LinkedMemoryRef list.

        Issue #440. Limit 50 each; setting limit=51 lets us detect "more
        available" without paginating. The destination Memory bulk-fetch is
        re-scoped to (workspace_id, context_id, deleted_at IS NULL) as
        defense-in-depth — orphaned/soft-deleted/cross-context edges are
        silently dropped from the response.

        Edge fetches run sequentially: SQLAlchemy AsyncSession forbids
        concurrent operations on the same session, so ``asyncio.gather``
        on two ``self.db.execute`` calls raises ``InvalidRequestError``.
        Two sequential round-trips are cheap (≤100 rows each) and the
        correctness gain dominates.
        """
        from repositories.neural_edge import NeuralEdgeRepository

        edge_repo = NeuralEdgeRepository(self.db)
        cap = 50

        # Issue #741: discriminator pivoted from edge_type='declared_link' to
        # origin='declared'. The post-#741 schema merges edge_type into
        # ``neural_association`` and tracks user-asserted semantics on the
        # ``origin`` column.
        out_edges = await edge_repo.get_outgoing_edges(
            user_id=None,
            src_id=memory_id,
            limit=cap + 1,
            workspace_id=str(workspace_id),
            context_id=str(context_id),
            origin=EDGE_ORIGIN_DECLARED,
        )
        in_edges = await edge_repo.get_incoming_edges(
            user_id=None,
            dst_id=memory_id,
            limit=cap + 1,
            workspace_id=str(workspace_id),
            context_id=str(context_id),
            origin=EDGE_ORIGIN_DECLARED,
        )

        out_has_more = len(out_edges) > cap
        in_has_more = len(in_edges) > cap
        out_edges = out_edges[:cap]
        in_edges = in_edges[:cap]

        linked_ids: set[UUID] = set()
        for e in out_edges:
            linked_ids.add(e.dst_id)
        for e in in_edges:
            linked_ids.add(e.src_id)

        if not linked_ids:
            return [], out_has_more, [], in_has_more

        result = await self.db.execute(
            select(Memory).where(
                and_(
                    Memory.id.in_(list(linked_ids)),
                    Memory.workspace_id == workspace_id,
                    Memory.context_id == context_id,
                    Memory.deleted_at.is_(None),
                )
            )
        )
        memories_by_id = {m.id: m for m in result.scalars().all()}

        # #1299: linked refs expose neighbor type/summary — the same context
        # is guaranteed by the edge invariant, but types may differ, so the
        # per-memory binding filter must drop disallowed refs too (log-only
        # in shadow; refs are adjacent metadata, not an MAE operation).
        from services.agent_binding_service import filter_memory_rows_by_binding

        kept_refs, _ = await filter_memory_rows_by_binding(
            self.db, list(memories_by_id.values()), operation=None, user_id=None
        )
        memories_by_id = {m.id: m for m in kept_refs}

        outgoing_links: list[LinkedMemoryRef] = []
        for e in out_edges:
            target = memories_by_id.get(e.dst_id)
            if target is None:
                continue
            outgoing_links.append(
                LinkedMemoryRef(
                    memory_id=target.id,
                    summary=target.summary,
                    type=target.type,
                    importance=target.importance,
                    weight=e.weight,
                    created_at=e.created_at,
                )
            )

        incoming_links: list[LinkedMemoryRef] = []
        for e in in_edges:
            source = memories_by_id.get(e.src_id)
            if source is None:
                continue
            incoming_links.append(
                LinkedMemoryRef(
                    memory_id=source.id,
                    summary=source.summary,
                    type=source.type,
                    importance=source.importance,
                    weight=e.weight,
                    created_at=e.created_at,
                )
            )

        return outgoing_links, out_has_more, incoming_links, in_has_more

    async def _create_declared_links(
        self,
        memory_id: UUID,
        request: RememberRequest,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> None:
        """Create declared_link edges from linked_memory_ids and linked_source_uris.

        Issue #215: Best-effort — failures are logged but never roll back
        the memory creation. Forward references (source_uri not yet in DB)
        are silently skipped.
        """
        if (
            not request.linked_memory_ids
            and not request.linked_source_uris
            and not getattr(request, "supersedes", None)
        ):
            return

        if not workspace_id or not context_id:
            logger.warning("declared_links_skipped_no_isolation", memory_id=str(memory_id))
            return

        from repositories.neural_edge import NeuralEdgeRepository

        edge_repo = NeuralEdgeRepository(self.db)
        created = 0

        try:
            # Direct links by memory ID (batch-validate targets exist in same scope)
            requested_ids = [t for t in (request.linked_memory_ids or []) if t != memory_id]
            if requested_ids:
                result = await self.db.execute(
                    select(Memory.id).where(
                        Memory.id.in_(requested_ids),
                        Memory.user_id == user_id,
                        Memory.workspace_id == UUID(workspace_id),
                        Memory.context_id == UUID(context_id),
                        Memory.deleted_at.is_(None),
                    )
                )
                valid_ids = {row.id for row in result}
                for target_id in requested_ids:
                    if target_id not in valid_ids:
                        logger.debug("declared_link_target_not_found", target_id=str(target_id))
                        continue
                    # Issue #741: edge_type='declared_link' deprecated;
                    # discriminator moved to origin='declared'.
                    await edge_repo.create_edge_if_absent(
                        user_id=user_id,
                        src_id=memory_id,
                        dst_id=target_id,
                        edge_type=EDGE_TYPE_NEURAL_ASSOCIATION,
                        weight=1.0,
                        confidence=1.0,
                        workspace_id=workspace_id,
                        context_id=context_id,
                        origin=EDGE_ORIGIN_DECLARED,
                    )
                    created += 1

            # Links by source_uri (batch resolve to memory_id)
            uris = request.linked_source_uris or []
            if uris:
                result = await self.db.execute(
                    select(Memory.source_uri, Memory.id).where(
                        Memory.user_id == user_id,
                        Memory.workspace_id == UUID(workspace_id),
                        Memory.context_id == UUID(context_id),
                        Memory.source_uri.in_(uris),
                        Memory.deleted_at.is_(None),
                    )
                )
                uri_to_id = {row.source_uri: row.id for row in result}
                for uri in uris:
                    target_id = uri_to_id.get(uri)
                    if target_id is None:
                        logger.debug("declared_link_forward_ref_skipped", uri=uri)
                        continue
                    if target_id == memory_id:
                        continue
                    # Issue #741: edge_type='declared_link' deprecated;
                    # discriminator moved to origin='declared'.
                    await edge_repo.create_edge_if_absent(
                        user_id=user_id,
                        src_id=memory_id,
                        dst_id=target_id,
                        edge_type=EDGE_TYPE_NEURAL_ASSOCIATION,
                        weight=1.0,
                        confidence=1.0,
                        workspace_id=workspace_id,
                        context_id=context_id,
                        origin=EDGE_ORIGIN_DECLARED,
                    )
                    created += 1

            # #1208: fact succession. src = this new memory (superseding),
            # dst = the predecessor (superseded → shadowed out of default
            # recall). Same-scope + active-target validation as direct links;
            # the predecessor is never mutated — visibility is the edge's job.
            supersedes_target = getattr(request, "supersedes", None)
            if supersedes_target and supersedes_target != memory_id:
                from models.memory import EDGE_TYPE_SUPERSEDES

                result = await self.db.execute(
                    select(Memory.id).where(
                        Memory.id == supersedes_target,
                        Memory.user_id == user_id,
                        Memory.workspace_id == UUID(workspace_id),
                        Memory.context_id == UUID(context_id),
                        Memory.deleted_at.is_(None),
                    )
                )
                if result.scalar_one_or_none() is not None:
                    # Upsert, not create-if-absent: unique_edge is keyed on
                    # (user, src, dst) regardless of edge_type, so a plain
                    # declared link created earlier in this same request
                    # (linked_memory_ids naming the same target) would make
                    # an if-absent insert silently drop the succession the
                    # caller explicitly asked for.
                    await edge_repo.create_or_update_edge(
                        user_id=user_id,
                        src_id=memory_id,
                        dst_id=supersedes_target,
                        edge_type=EDGE_TYPE_SUPERSEDES,
                        weight=1.0,
                        confidence=1.0,
                        workspace_id=workspace_id,
                        context_id=context_id,
                        origin=EDGE_ORIGIN_DECLARED,
                        return_fresh_edge=False,
                    )
                    created += 1
                else:
                    logger.warning(
                        "supersedes_target_not_found",
                        memory_id=str(memory_id),
                        target_id=str(supersedes_target),
                    )

            if created > 0:
                await self.db.commit()
                logger.info(
                    "declared_links_created",
                    memory_id=str(memory_id),
                    count=created,
                )
        except Exception as e:
            await self.db.rollback()
            logger.warning(
                "declared_links_failed",
                memory_id=str(memory_id),
                error=str(e),
            )
            # Best-effort: do not raise — memory creation already succeeded

    @staticmethod
    def _compute_recall_confidence(
        scores: list[float],
        *,
        semantic_scores: list[float] | None = None,
    ) -> RecallConfidence:
        """Issue #1047/#1052: relevance confidence — does the pool actually hold
        something relevant, or should the agent stop probing / go external?

        Two inputs, deliberately different roles:

        - ``semantic_scores`` (#1052): the RAW (pre-normalization) semantic cosines
          of the candidate pool. These carry ABSOLUTE match strength. ``level`` is
          driven by ``prominence`` = ``(top - mean_background) / mean_background``
          over these — a ratio, so it is invariant to an embedding model's overall
          cosine scale (no fixed cross-model cutoff). A very high absolute top
          cosine (``_CONF_STRONG_ABS_COSINE``) additionally floors ``level`` to at
          least ``moderate`` so a near-duplicate match is never reported as absent
          on a low-spread model.
        - ``scores`` (#1047): the normalized hybrid scores (ranking order). Used
          only for ``result_count`` and as the FALLBACK basis when no semantic
          scores are available (keyword-only search, empty pool). The fallback is
          the original z-score-separation heuristic.

        Why the change (#1052): max-normalization in hybrid merge rescales every
        arm's top hit to 1.0, and the z-score margin DIVIDES by the background
        std, which a flat off-topic tail makes tiny — so an irrelevant query's
        "least-bad" hit looked just as separated as a real match (benchmarked:
        absent queries scored ``high``). Absolute semantic strength is the signal
        that actually distinguishes present from absent; prominence makes it
        model-scale-invariant.

        Does NOT touch ranking; purely a derived annotation. ``scores`` /
        ``semantic_scores`` may be in any order — sorted descending internally.
        """
        sem = sorted((semantic_scores or []), reverse=True)
        if sem:
            return MemoryService._confidence_from_semantic(
                sem, result_count=len(scores) or len(sem)
            )

        # FALLBACK (#1047): no raw semantic cosines (keyword-only mode / empty
        # pool). Use the original z-score separation over the normalized scores.
        scores = sorted(scores, reverse=True)
        n = len(scores)
        if n == 0:
            return RecallConfidence(
                level="none",
                top_score=None,
                prominence=None,
                relative_margin=None,
                result_count=0,
                rationale="No candidates retrieved — likely nothing relevant in this context.",
            )
        top = scores[0]
        if n == 1:
            # No background distribution to measure separation against.
            return RecallConfidence(
                level="moderate",
                top_score=top,
                prominence=None,
                relative_margin=None,
                result_count=1,
                rationale="Single candidate — no background distribution to assess separation.",
            )
        background = scores[1:]
        mean_bg = statistics.fmean(background)
        std_bg = statistics.pstdev(background) if len(background) > 1 else 0.0
        if std_bg > 1e-9:
            z = (top - mean_bg) / std_bg
        else:
            # Flat background → fall back to a relative gap vs the background mean.
            denom = abs(mean_bg) if abs(mean_bg) > 1e-9 else 1.0
            z = (top - mean_bg) / denom
        if z >= 2.0:
            level, note = "high", "clear separation from background"
        elif z >= 1.0:
            level, note = "moderate", "some separation from background"
        elif z >= 0.3:
            level, note = "low", "weak separation — relevance uncertain"
        else:
            level, note = (
                "none",
                "top hit indistinguishable from background — likely nothing relevant",
            )
        return RecallConfidence(
            level=level,
            top_score=top,
            prominence=None,
            relative_margin=round(z, 3),
            result_count=n,
            rationale=(
                f"Keyword-only pool: top hit sits {z:.2f} background-std-devs above the "
                f"candidate-pool background; {note}."
            ),
        )

    @staticmethod
    def _confidence_from_semantic(sem: list[float], *, result_count: int) -> RecallConfidence:
        """Issue #1052: confidence from RAW semantic cosines (sorted desc).

        ``level`` is driven by model-scale-invariant prominence when the
        background gives a usable positive frame, and by absolute cosine bands
        when it does not (mean ≤ ``_CONF_BG_EPS``). Absolute floors additionally
        guard high/moderate so a weak top over a weaker background cannot inflate
        the ratio into a false "high", and a near-duplicate top is never reported
        as absent. See the calibration constants for thresholds.
        """
        top = sem[0]
        n = len(sem)
        if n == 1:
            # No background to assess prominence; judge on absolute strength alone.
            if top >= _CONF_STRONG_ABS_COSINE:
                level, note = (
                    "moderate",
                    "single strong absolute match, no background to corroborate",
                )
            else:
                level, note = "low", "single candidate; absolute match strength is modest"
            return RecallConfidence(
                level=level,
                top_score=round(top, 3),
                prominence=None,
                relative_margin=None,
                result_count=result_count,
                rationale=f"Single semantic candidate (cosine {top:.2f}); {note}.",
            )

        background = sem[1:]
        mean_bg = statistics.fmean(background)
        std_bg = statistics.pstdev(background) if len(background) > 1 else 0.0
        z = (top - mean_bg) / std_bg if std_bg > 1e-9 else None

        if mean_bg > _CONF_BG_EPS:
            # Positive background → prominence ratio is well-defined and
            # model-scale-invariant. Denominator ≥ _CONF_BG_EPS keeps it finite.
            prominence: float | None = (top - mean_bg) / mean_bg
            if prominence >= _CONF_HIGH_PROMINENCE and top >= _CONF_ABS_MODERATE_COSINE:
                level = "high"
            elif prominence >= _CONF_MODERATE_PROMINENCE and top >= _CONF_ABS_LOW_COSINE:
                level = "moderate"
            elif prominence >= _CONF_LOW_PROMINENCE:
                level = "low"
            else:
                level = "none"
        else:
            # Background at/below ~0 (model drives unrelated content toward 0 / a
            # negative cosine): the ratio is meaningless, so judge absolute strength.
            prominence = None
            if top >= _CONF_STRONG_ABS_COSINE:
                level = "high"
            elif top >= _CONF_ABS_MODERATE_COSINE:
                level = "moderate"
            elif top >= _CONF_ABS_LOW_COSINE:
                level = "low"
            else:
                level = "none"

        # Near-duplicate absolute match → never report as absent even if the pool
        # is tightly clustered (low-spread model produces small prominence).
        if top >= _CONF_STRONG_ABS_COSINE and level in ("low", "none"):
            level = "moderate"

        prom_str = "n/a" if prominence is None else f"{prominence:.2f}"
        note = {
            "high": "top hit is a strong, clearly-separated match",
            "moderate": "top hit is a plausible match",
            "low": "top hit is weak — relevance uncertain, consider going external",
            "none": "top hit barely exceeds the background — likely nothing relevant",
        }[level]
        return RecallConfidence(
            level=level,
            top_score=round(top, 3),
            prominence=round(prominence, 3) if prominence is not None else None,
            relative_margin=round(z, 3) if z is not None else None,
            result_count=result_count,
            rationale=(
                f"Top semantic cosine {top:.2f}, prominence {prom_str} above the candidate-pool "
                f"mean ({mean_bg:.2f}); {note}."
            ),
        )

    @staticmethod
    def _reinforce_factor(
        *,
        reference_count: int,
        net_helpful: int,
        importance: float,
        age_days: int,
        max_boost: float,
        adopt_norm: float = 5.0,
        feedback_norm: float = 3.0,
        recency_tau_days: float = 14.0,
        cold_start_weight: float = 0.25,
        adopt_weight: float = 0.5,
        feedback_weight: float = 0.5,
    ) -> float:
        """Issue #1048: bounded, importance-weighted recall-standing multiplier.

        Returns a factor in ``[1 - max_boost, 1 + max_boost]`` to multiply a
        result's hybrid_score by. Combines, all importance-weighted:
        - adoption (reference_count, #1046) — log-scaled, capped at 1 so unbounded
          popularity cannot exceed the bound (popularity-bias guard);
        - retrieval feedback (net helpful, #888) — tanh-squashed to [-1, 1];
        plus a cold-start RECENCY prior (positive, decays with age) so zero-adoption
        NEW memories still surface — the boost is deliberately NOT purely
        usage-monotonic. Uses ONLY adoption + feedback + importance + recency, never
        graph/Hebbian signals (Issue #120 recall/explore boundary). The bound keeps
        semantic relevance dominant: this only reorders the already
        relevance-filtered candidate pool, it never pulls in new hits.
        """
        adopt = min(1.0, math.log1p(max(0, reference_count)) / math.log1p(adopt_norm))
        fb = math.tanh(net_helpful / feedback_norm)
        usage = importance * (adopt_weight * adopt + feedback_weight * fb)
        cold = cold_start_weight * math.exp(-max(0, age_days) / recency_tau_days)
        signal = max(-1.0, min(1.0, usage + cold))
        return 1.0 + max_boost * signal

    @staticmethod
    def _reinforce_telemetry(
        *,
        order_before: list[str],
        order_after: list[str],
        factors: dict[str, float],
        zero_adoption_ids: set[str],
        top_k: int,
    ) -> dict[str, object]:
        """Issue #1069: a pure, side-effect-free summary of one reinforce re-rank.

        Emitted as a ``reinforce_rerank_applied`` structured log so per-context
        enabling and any regression are observable without a metrics backend (the
        codebase's observability is structlog/JSON, not Prometheus). The load-bearing
        regression signal is ``zero_adoption_in_topk`` — the cold-start /
        popularity-bias guard: if reinforce starves brand-new (adoption=0) memories
        out of the user-visible slice, this drops. ``reordered`` / ``top1_changed``
        confirm the re-rank is actually active once an operator flips
        ``reinforce_enabled`` on a context; the ``factor_*`` distribution shows how
        hard it is pushing. ``top_k`` is the user-visible slice (``request.k``).
        """
        vals = list(factors.values())
        eps = 1e-9
        k = max(0, top_k)
        return {
            "candidates": len(order_after),
            "reordered": order_before != order_after,
            "top1_changed": order_before[:1] != order_after[:1],
            "factor_min": round(min(vals), 4) if vals else 1.0,
            "factor_max": round(max(vals), 4) if vals else 1.0,
            "factor_mean": round(sum(vals) / len(vals), 4) if vals else 1.0,
            "boosted": sum(1 for f in vals if f > 1.0 + eps),
            "demoted": sum(1 for f in vals if f < 1.0 - eps),
            "zero_adoption_in_topk": sum(1 for mid in order_after[:k] if mid in zero_adoption_ids),
            "topk": min(k, len(order_after)),
        }

    async def _apply_supersede_shadowing(
        self,
        search_results: list[dict],
        memories: dict,
        *,
        include_superseded: bool,
    ) -> tuple[dict[UUID, UUID], dict[UUID, list[UUID]]]:
        """#1208: demote superseded memories out of the candidate pool.

        Direction convention (models/memory.py): a ``supersedes`` edge points
        src = superseding (newer) → dst = superseded (older). A candidate that
        is the dst of a **live** edge — one whose superseding src memory still
        exists and is not soft-deleted — is shadowed:

        - default: removed from ``search_results`` IN PLACE (before the
          re-rank and the top-k slice, so the slice stays full);
        - ``include_superseded=True``: kept, and the returned shadow map lets
          the caller annotate it with ``superseded_by``.

        The liveness JOIN is the self-healing property: edge memory refs are
        bare UUIDs (no FK), so a deleted superseder must stop shadowing —
        deleting the edge OR the superseder restores full visibility.

        ``contradicts`` edges NEVER hide anything: both sides of every edge
        touching a candidate are collected into the returned contradiction
        map for annotation (resolution is an arbitration problem).

        Fail-open: shadowing is an enhancement on the read path — any error
        preserves the original results (a query bug must not blank recall).

        Returns:
            (shadow_map dst→src, contradiction_map memory→[opponents]).
        """
        try:
            from sqlalchemy import or_
            from sqlalchemy.orm import aliased

            from models.memory import (
                EDGE_TYPE_CONTRADICTS,
                EDGE_TYPE_SUPERSEDES,
                NeuralMemoryEdge,
            )

            candidate_ids = {memories[r["id"]].id for r in search_results if r["id"] in memories}
            if not candidate_ids:
                return {}, {}

            superseder = aliased(Memory)
            # Deliberately NOT scoped to the calling user: in a shared
            # context, member B's supersedes edge must shadow the stale fact
            # for member A too — otherwise stale-fact suppression fails in
            # exactly the team setting it matters most. Safe because
            # candidate_ids are already visibility-scoped by the recall
            # upstream, memory UUIDs are globally unique, and edge creation
            # validates both endpoints exist in the creator's own context.
            # Ordered so the NEWEST superseder wins when multiple live
            # supersedes edges target the same dst (allowed by the schema):
            # dict() keeps the last row per key, so ascending created_at
            # (id as tiebreak) makes the annotation deterministic instead
            # of flapping with row order between runs.
            rows = await self.db.execute(
                select(NeuralMemoryEdge.dst_id, NeuralMemoryEdge.src_id)
                .join(superseder, superseder.id == NeuralMemoryEdge.src_id)
                .where(
                    NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
                    NeuralMemoryEdge.dst_id.in_(candidate_ids),
                    superseder.deleted_at.is_(None),
                )
                .order_by(superseder.created_at.asc(), superseder.id.asc())
            )
            shadow_map: dict[UUID, UUID] = dict(rows.all())

            c_rows = await self.db.execute(
                select(NeuralMemoryEdge.src_id, NeuralMemoryEdge.dst_id).where(
                    NeuralMemoryEdge.edge_type == EDGE_TYPE_CONTRADICTS,
                    or_(
                        NeuralMemoryEdge.src_id.in_(candidate_ids),
                        NeuralMemoryEdge.dst_id.in_(candidate_ids),
                    ),
                )
            )
            contradiction_map: dict[UUID, list[UUID]] = {}
            for src, dst in c_rows.all():
                contradiction_map.setdefault(src, []).append(dst)
                contradiction_map.setdefault(dst, []).append(src)

            if shadow_map and not include_superseded:
                before = len(search_results)
                search_results[:] = [
                    r
                    for r in search_results
                    if r["id"] not in memories or memories[r["id"]].id not in shadow_map
                ]
                logger.info(
                    "supersede_shadowing_applied",
                    shadowed=before - len(search_results),
                    candidates=before,
                )

            return shadow_map, contradiction_map
        except Exception as e:
            logger.warning("supersede_shadowing_failed", error=str(e))
            return {}, {}

    async def _resolve_search_mode(
        self,
        request: RecallRequest,
        context_id: UUID,
        *,
        cross_context: bool,
        config: Any = None,
    ) -> str:
        """#1212: resolve the effective search mode, optionally via the router.

        Resolution order:

        1. An explicitly passed ``search_mode`` ALWAYS wins (router never
           overrides a caller's choice, in any routing_mode) — and returns
           immediately, skipping classification: no classify/log work on the
           hot path when the outcome is already decided. Router telemetry
           therefore covers only the calls the router could actually
           influence (search_mode omitted).
        2. ``routing_mode='active'`` and no explicit mode → the classifier's
           lane.
        3. Otherwise → ``"hybrid"`` (the historical default).

        In ``log_only`` and ``active`` the decision is stamped into telemetry
        (``query_router_decision`` — numeric features only, never query text).
        Cross-context recalls skip routing entirely: the config is
        per-context and a mixed set has no single answer. This method issues
        NO DB read (#1220 advisor rec): ``recall()`` prefetches the config
        row once (read-only, fail-open) and threads it here and into the
        reinforce re-rank — a missing/unfetched row simply means routing
        off, exactly the pre-prefetch behavior.

        Args:
            request: The recall request (``search_mode`` may be None).
            context_id: The single target context.
            cross_context: True for multi-context recalls (routing skipped).
            config: The prefetched ContextSearchConfig row (or None).

        Returns:
            The effective search mode ("hybrid" | "semantic" | "keyword").
        """
        requested_mode = request.search_mode
        default_mode = requested_mode or "hybrid"
        if cross_context or requested_mode is not None:
            return default_mode

        routing_mode = getattr(config, "routing_mode", None) if config else None
        if routing_mode not in ("log_only", "active"):
            return default_mode

        route = classify_query(request.query)
        applied = routing_mode == "active" and requested_mode is None
        effective_mode = route.lane if applied else default_mode
        logger.info(
            "query_router_decision",
            context_id=str(context_id),
            routing_mode=routing_mode,
            decided_lane=route.lane,
            applied=applied,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            reasons=list(route.reasons),
            features=route.features,
        )
        return effective_mode

    @staticmethod
    def _graph_boost_settings() -> tuple[bool, float]:
        """#1213: env-gated experiment flag (read per call, like the eval pins).

        An env flag — not a ContextSearchConfig column — because this is a
        flagged EXPERIMENT: it must not mint a fifth parallel migration head
        (e55-e58 already coordinate), and per-context graduation only happens
        if the placebo gate (tests/eval/graph_boost_gate.py) passes.
        """
        import math
        import os

        enabled = os.getenv("KAGURA_GRAPH_BOOST_ENABLED", "").lower() in ("1", "true")
        try:
            max_boost = float(os.getenv("KAGURA_GRAPH_BOOST_MAX", "0.15"))
            # float() accepts "nan"/"inf" without raising — "nan" would
            # otherwise clamp to 0.0 and silently disable the experiment
            # while telemetry still reports it applied.
            if not math.isfinite(max_boost):
                max_boost = 0.15
        except ValueError:
            max_boost = 0.15
        return enabled, max(0.0, min(max_boost, 0.5))

    async def _maybe_graph_boost(
        self,
        search_results: list[dict],
        memories: dict,
        context_id: UUID | None,
        user_id: str | None,
        top_k: int | None = None,
    ) -> None:
        """#1213: bounded multiplicative graph term over the hybrid top-k.

        Placebo-gated EXPERIMENT (default off; when off, recall is
        bit-identical: no edge query, no reorder). When
        ``KAGURA_GRAPH_BOOST_ENABLED`` is set, each candidate score is
        multiplied by ``1 + b * conn/max_conn`` where ``conn`` is the summed
        weight of its hebbian edges to OTHER candidates in the same pool —
        the warm co-activation companion structure the eval program measured
        (+0.20 recovery over a degree-matched rewiring). Boost-only
        ([1, 1+b]); isolated candidates keep factor 1.0.
        Multiplicative-with-cap, NOT additive score fusion — the F1 hybrid
        null showed fixed additive fusion dilutes precision.

        Hebbian origin only: boosting on ``semantic`` edges would
        double-count the vector similarity already in the base score, and
        ``declared`` edges are unmeasured. Composes with the reinforce
        re-rank via the ``_rerank_factor`` stamp (a product of two bounded
        factors stays bounded). Single-context only; fail-safe (any failure
        preserves the ranking).
        """
        enabled, max_boost = self._graph_boost_settings()
        if not enabled or context_id is None or user_id is None or len(search_results) < 2:
            return
        try:
            from sqlalchemy import select

            from models.memory import NeuralMemoryEdge

            # The experiment is defined over the hybrid top-k SLICE, not the
            # expanded candidate pool (recall over-fetches k*4 when neural is
            # on): boosting the whole pool would let pool-only items ride
            # into the visible slice — a different experiment than the one
            # the eval program measured — and inflate the edge-query size.
            limit = len(search_results) if top_k is None else min(top_k, len(search_results))
            scoped = search_results[:limit]
            if len(scoped) < 2:
                return

            cand_ids = []
            seen: set = set()
            for r in scoped:
                mem = memories.get(r["id"])
                if mem is not None and mem.id not in seen:
                    seen.add(mem.id)
                    cand_ids.append(mem.id)
            if len(cand_ids) < 2:
                return

            rows = await self.db.execute(
                select(
                    NeuralMemoryEdge.src_id, NeuralMemoryEdge.dst_id, NeuralMemoryEdge.weight
                ).where(
                    NeuralMemoryEdge.context_id == context_id,
                    # Per-user scope, same discipline as activation.py: in a
                    # shared context another member's co-activation history
                    # (forgeable by deliberate co-recall) must not move THIS
                    # caller's ranking (gate2/CSO).
                    NeuralMemoryEdge.user_id == user_id,
                    NeuralMemoryEdge.origin == "hebbian",
                    NeuralMemoryEdge.src_id.in_(cand_ids),
                    NeuralMemoryEdge.dst_id.in_(cand_ids),
                    NeuralMemoryEdge.src_id != NeuralMemoryEdge.dst_id,
                )
            )
            conn: dict[str, float] = {}
            for src_id, dst_id, weight in rows.all():
                w = float(weight or 0.0)
                conn[str(src_id)] = conn.get(str(src_id), 0.0) + w
                conn[str(dst_id)] = conn.get(str(dst_id), 0.0) + w
            max_conn = max(conn.values(), default=0.0)
            if max_conn <= 0.0:
                return

            def _sid(r: dict) -> str | None:
                mem = memories.get(r["id"])
                return str(mem.id) if mem is not None else None

            factors: dict[str, float] = {
                sid: 1.0 + max_boost * (conn.get(sid, 0.0) / max_conn)
                for sid in (str(c) for c in cand_ids)
            }

            def _adjusted(r: dict) -> float:
                base = r.get("hybrid_score")
                if base is None:
                    base = r.get("score") or 0.0
                sid = _sid(r)
                g = factors.get(sid, 1.0) if sid is not None else 1.0
                return base * r.get("_rerank_factor", 1.0) * g

            # Sort and stamp ONLY the top-k slice; items past the slice keep
            # their original order (they are outside the experiment).
            order_before = [x for x in (_sid(r) for r in scoped) if x is not None]
            scoped.sort(key=_adjusted, reverse=True)
            for r in scoped:
                sid = _sid(r)
                if sid is not None:
                    r["_rerank_factor"] = r.get("_rerank_factor", 1.0) * factors.get(sid, 1.0)
            search_results[:limit] = scoped
            order_after = [x for x in (_sid(r) for r in scoped) if x is not None]

            try:
                logger.info(
                    "graph_boost_applied",
                    context_id=str(context_id),
                    max_boost=round(max_boost, 4),
                    candidates=len(cand_ids),
                    connected=len(conn),
                    max_conn=round(max_conn, 4),
                    reordered_topk=order_before != order_after,
                )
            except Exception:  # noqa: BLE001 - telemetry must not break recall
                logger.warning("graph_boost_telemetry_failed", context_id=str(context_id))
        except Exception as exc:  # noqa: BLE001 - experiment must never break recall
            logger.warning("graph_boost_skipped", context_id=str(context_id), error=str(exc))

    async def _maybe_reinforce_rerank(
        self,
        search_results: list[dict],
        memories: dict,
        context_id: UUID | None,
        top_k: int | None = None,
        config: Any = None,
    ) -> None:
        """Issue #1048: bounded, config-gated recall re-rank by adoption + feedback.

        No-op unless the context's ``ContextSearchConfig.reinforce_enabled`` is set
        (#1207: ON by default for config rows created since then — including rows
        lazily materialized by the search path's ``create_or_get`` — with a stored
        ``false`` as the opt-out; pre-#1207 rows keep their stored value).
        Single-context only — a per-context config/feedback set can't govern a
        cross-context pool.
        Re-sorts ``search_results`` IN PLACE by ``hybrid_score * _reinforce_factor``.

        Issue #1069: when the re-rank fires it emits a ``reinforce_rerank_applied``
        structured log carrying per-context uplift + popularity-bias telemetry, so a
        staged rollout is monitorable. ``top_k`` is the user-visible slice the
        telemetry measures the zero-adoption surfacing rate over (the caller passes
        ``request.k``); ``None`` falls back to the whole candidate pool.

        #1220 (advisor rec): ``config`` is the row ``recall()`` prefetched at
        the top of the call — passing it skips this helper's own SELECT.
        ``None`` falls back to a local read: the prefetch happens BEFORE
        hybrid_search's ``create_or_get`` materializes a fresh context's row,
        so a row-less-at-prefetch context must be re-read here to keep the
        first-ever recall's re-rank behavior unchanged.
        """
        if context_id is None or len(search_results) < 2:
            return
        # Reinforce is an optional enhancement — it must NEVER break recall. Any
        # failure (config fetch, feedback query, bad data) is swallowed and the
        # original hybrid ranking is preserved (fail-safe, mirrors the spend-cap
        # fail-open philosophy).
        try:
            cfg = config
            if cfg is None:
                from repositories.config_repository import ContextSearchConfigRepository

                # READ-ONLY lookup — this helper must not create/commit a config
                # row (create_or_get would INSERT+COMMIT, adding a side effect
                # here). In practice hybrid_search's _get_search_config has
                # usually materialized the row earlier in this same recall (with
                # the #1207 default), so the cfg-None branch is a fail-safe for
                # genuinely row-less states, not a legacy-context opt-out.
                cfg = await ContextSearchConfigRepository(self.db).get_by_context(context_id)
            if cfg is None or not getattr(cfg, "reinforce_enabled", False):
                return
            max_boost = float(cfg.reinforce_max_boost)
            # Issue #1065: forge-resistant mode. When set, only host-arbitrated
            # feedback (an unforgeable, independently-verdicted signal) moves
            # ranking — an untrusted agent's self-emitted feedback(helpful=True)
            # is ignored. Default OFF → all feedback counts (pre-#1065 behaviour).
            require_host = bool(getattr(cfg, "reinforce_require_host_arbitration", False))

            from services.feedback_service import FeedbackService

            seen: set = set()
            cand_ids = []
            for r in search_results:
                mem = memories.get(r["id"])
                if mem is not None and mem.id not in seen:
                    seen.add(mem.id)
                    cand_ids.append(mem.id)
            feedback = await FeedbackService(self.db).aggregate_for_memories(
                context_id, cand_ids, host_only=require_host
            )
            now = utcnow()

            # Precompute the per-memory factor once (id-keyed) so the sort key and the
            # telemetry summary share one computation and can never diverge.
            factors: dict[str, float] = {}
            zero_adoption_ids: set[str] = set()
            for r in search_results:
                mem = memories.get(r["id"])
                if mem is None:
                    continue
                sid = str(mem.id)
                if sid in factors:
                    continue
                agg = feedback.get(sid)
                factors[sid] = self._reinforce_factor(
                    reference_count=mem.reference_count or 0,
                    net_helpful=agg.net if agg else 0,
                    importance=mem.importance,
                    age_days=max(0, (now - mem.created_at).days),
                    max_boost=max_boost,
                )
                if (mem.reference_count or 0) == 0:
                    zero_adoption_ids.add(sid)

            def _sid(r: dict) -> str | None:
                mem = memories.get(r["id"])
                return str(mem.id) if mem is not None else None

            def _adjusted(r: dict) -> float:
                base = r.get("hybrid_score")
                if base is None:
                    base = r.get("score") or 0.0
                sid = _sid(r)
                return base * (factors.get(sid, 1.0) if sid is not None else 1.0)

            # #1213: stamp the factor so any later bounded re-ranker (e.g. the
            # graph-boost experiment) composes multiplicatively instead of
            # re-sorting by the raw base and silently erasing this re-rank.
            for r in search_results:
                sid = _sid(r)
                r["_rerank_factor"] = factors.get(sid, 1.0) if sid is not None else 1.0

            order_before = [s for s in (_sid(r) for r in search_results) if s is not None]
            search_results.sort(key=_adjusted, reverse=True)
            order_after = [s for s in (_sid(r) for r in search_results) if s is not None]

            # Telemetry is isolated in its own guard: the re-rank has ALREADY been
            # applied above, so a telemetry/log failure must NOT fall through to the
            # outer except and emit the misleading "skipped" signal an operator is
            # monitoring the rollout on. It also must not break recall — hence a
            # distinct, swallowed ``reinforce_telemetry_failed`` event.
            try:
                logger.info(
                    "reinforce_rerank_applied",
                    context_id=str(context_id),
                    max_boost=round(max_boost, 4),
                    require_host_arbitration=require_host,
                    **self._reinforce_telemetry(
                        order_before=order_before,
                        order_after=order_after,
                        factors=factors,
                        zero_adoption_ids=zero_adoption_ids,
                        top_k=top_k if top_k is not None else len(order_after),
                    ),
                )
            except Exception as texc:  # noqa: BLE001 — telemetry must not break recall
                logger.warning("reinforce_telemetry_failed", error=str(texc))
        except Exception as exc:  # noqa: BLE001 — reinforce must never break recall
            logger.warning("reinforce_rerank_skipped", error=str(exc))

    async def _build_selection_evidence(
        self,
        config: RecallSelectionConfig,
        *,
        eligible_ids: tuple[str, ...],
        request: RecallRequest,
        search_config: Any,
        context_id: UUID,
    ) -> tuple[RecallSelectionPlan, dict[str, Any]]:
        """Build identity-only #1306 evidence from an already-authorized pool."""
        plan = plan_recall_selection(eligible_ids, top_k=request.k, config=config)
        effective_search_config = search_config
        if effective_search_config is None:
            from repositories.config_repository import ContextSearchConfigRepository

            effective_search_config = await ContextSearchConfigRepository(self.db).get_by_context(
                context_id
            )
        graph_enabled, graph_max_boost = self._graph_boost_settings()
        evidence = {
            "selection_probabilities": plan.selection_probabilities,
            "selection_policy": {
                **plan.policy,
                "ranking_policy": {
                    "name": "production_hybrid_recall_v1",
                    "search_mode": request.search_mode,
                    "use_rerank": request.use_rerank,
                    "reinforce_enabled": bool(
                        getattr(effective_search_config, "reinforce_enabled", False)
                    ),
                    "reinforce_require_host_arbitration": bool(
                        getattr(
                            effective_search_config,
                            "reinforce_require_host_arbitration",
                            False,
                        )
                    ),
                    "graph_boost_enabled": graph_enabled,
                    "graph_boost_max": graph_max_boost,
                    "trust_filter": "trusted",
                },
            },
        }
        return plan, evidence

    async def recall(
        self,
        request: RecallRequest,
        user_id: str,
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,  # NEW: Workspace ID (Issue #146)
        context_workspace_id: UUID
        | None = None,  # Issue #708: source workspace for shared-context Option A
        context_ids: list[UUID] | None = None,  # Issue #81: cross-context recall
        selection_config: RecallSelectionConfig | None = None,
    ) -> RecallResponse:
        """Search memories with Hybrid Search + Neural Memory.

        Issue #1 + #20 specification:
        Issue #146: Workspace-scoped API keys support
        - Phase 2: Hybrid Search (Semantic 60% + BM25 40%)
        - Phase 3: Neural Memory integration
          - Activation Spreading (graph association)
          - Unified Scoring (semantic + graph + temporal + trust)
          - Co-activation Tracking
          - Hebbian Update (async)

        Issue #82: Context-based multi-collection support.

        Issue #708: shared-context reads pay against the context's owner
        workspace ("Option A"). When ``context_workspace_id`` differs from
        ``current_workspace_id`` (caller's), the embedding key lookup,
        BYOK spend cap deduction, search filtering, and graph mutations
        all route to ``context_workspace_id``. Upstream rate-limit gating
        (``mcp_server.tools.__init__._check_rate_limit``) keeps the
        caller's workspace as the rate-limit subject so attackers cannot
        bypass their own RPS limit via shared-context reads.

        Args:
            request: Recall request
            user_id: User ID
            current_context_id: Current context UUID (Issue #82)
            current_workspace_id: Caller's active workspace (#146).
            context_workspace_id: Context owner workspace (#708 Option A).
                When None or equal to ``current_workspace_id`` the caller
                pays (self-workspace read). When different, the source
                workspace pays — provided it has a BYOK key configured
                (#708 H1; otherwise a uniform NotFoundException is raised
                to avoid leaking existence + configuration state via
                CWE-639 / OWASP A01).

        Returns:
            RecallResponse with search results
        """
        import os

        logger.info(
            "recall_request",
            user_id=user_id,
            query=request.query,
            k=request.k,
            use_rerank=request.use_rerank,
        )

        # Single Collection Migration: Validate required parameters
        if not current_workspace_id or not current_context_id:
            raise ValueError("recall() requires current_workspace_id and current_context_id")
        if selection_config is not None and (
            not request.filters or request.filters.get("trust_tier") != "trusted"
        ):
            raise ValueError("selection evidence requires trusted-tier recall")

        # #1291: recall does NOT pass through
        # ``PermissionService.resolve_context_for_workspace_read`` — the MCP
        # handler gates that path via ``_resolve_context_for_read``, but the REST
        # ``/memory/recall`` route calls this service method directly and so
        # bypassed the subtractive agent-binding filter before this fix. Apply
        # the gate HERE (service layer) so an agent-bound credential cannot read
        # a binding-denied context via ANY caller. No-op for non-agent
        # credentials (``get_agent_scope()`` is None → one contextvar read, no
        # DB query); deny raises the uniform ``NotFoundException("Context")``
        # matching the MCP path's 404. Covers the single declared context and
        # each entry on the #81 cross-context list (exactly the set searched
        # below).
        from services.agent_binding_service import agent_binding_permits

        for _gated_cid in context_ids if context_ids else [current_context_id]:
            if not await agent_binding_permits(
                self.db,
                _gated_cid,
                "read",
                operation="recall",  # #1286 (P0-5): deny-capture audit identity
                user_id=user_id,
            ):
                raise NotFoundException("Context", str(_gated_cid))

        # #708 Option A: route embedding cost (key + spend cap + paid_by
        # + search + graph) to the context's owner workspace. Rate-limit
        # gating stays on the caller upstream — do NOT change it here.
        # The H1 BYOK gate is deferred until we know hybrid_search will
        # actually fire (after the analysis_cluster short-circuit below),
        # so no-cost reads (empty cluster, etc.) do not get falsely 404'd.
        effective_workspace_id = current_workspace_id
        is_shared_context_read = (
            context_workspace_id is not None and context_workspace_id != current_workspace_id
        )
        if is_shared_context_read:
            effective_workspace_id = context_workspace_id  # type: ignore[assignment]

        # Check if Neural Memory is enabled
        neural_enabled = os.getenv("ENABLE_NEURAL_MEMORY", "false").lower() == "true"

        # Issue #81: Cross-context recall — pass list of context IDs to search service
        search_context_id: str | list[str] = str(current_context_id)
        if context_ids:
            search_context_id = [str(cid) for cid in context_ids]

        # #1220 (advisor rec): ONE read-only config fetch threads into both
        # the router below and the reinforce re-rank later — previously each
        # issued its own get_by_context SELECT per recall (the #341 class of
        # duplicated hot-path reads). Fail-open: a config read failure must
        # never break recall (None = routing off; reinforce re-reads).
        search_config = None
        if not context_ids:
            try:
                from repositories.config_repository import ContextSearchConfigRepository

                search_config = await ContextSearchConfigRepository(self.db).get_by_context(
                    current_context_id
                )
            except Exception as exc:
                logger.warning(
                    "search_config_prefetch_failed",
                    context_id=str(current_context_id),
                    error=str(exc),
                )

        # #1212: resolve the effective search mode exactly once, before any
        # downstream read of request.search_mode (BYOK charge gate, hybrid
        # search, neural checks). After this line it is always a concrete
        # mode string; None (caller omitted it) never flows further.
        # #1220 (advisor rec): model_copy instead of in-place mutation — a
        # caller that reuses one RecallRequest across calls must never see
        # its search_mode silently rewritten.
        request = request.model_copy(
            update={
                "search_mode": await self._resolve_search_mode(
                    request,
                    current_context_id,
                    cross_context=bool(context_ids),
                    config=search_config,
                )
            }
        )

        # Issue #496: ``analysis_cluster`` filter pre-resolves the cluster's
        # memory_ids so we can both (a) short-circuit empty clusters before
        # paying for the embedding+search round-trip and (b) expand the
        # candidate pool to cover the whole cluster — without this, a small
        # cluster may end up with zero overlap with Qdrant's top-N candidates.
        # The actual ``Memory.id IN ...`` filter is applied at the PG SELECT
        # below alongside the existing source_uri_prefix / source_type
        # post-filters; rerank / hybrid scoring see only cluster members.
        cluster_memory_ids: list[UUID] | None = None
        if request.filters and (cluster_filter := request.filters.get("analysis_cluster")):
            from services.analysis import query_service as _analysis_query_service

            # Issue #496 Copilot review fix: guard against non-dict shape so a
            # client sending ``analysis_cluster: "abc"`` (string) or a list
            # surfaces as a clean 422 ``ValidationError`` instead of an
            # internal 500 ``AttributeError`` on ``.get(...)``.
            #
            # ``ValidationError`` (a ``MemoryCloudException`` subclass) is the
            # right exception type — plain ``ValueError`` is NOT caught by
            # the global ``memory_cloud_exception_handler``, so the recall
            # route would 500 instead of returning 422 with the structured
            # ``{error, message, details}`` envelope. Caught by Copilot
            # review (loop 5).
            if not isinstance(cluster_filter, dict):
                raise ValidationError(
                    "filters.analysis_cluster must be an object with run_id + cluster_index",
                    field="filters.analysis_cluster",
                )
            run_id_raw = cluster_filter.get("run_id")
            cluster_index_raw = cluster_filter.get("cluster_index")
            if run_id_raw is None or cluster_index_raw is None:
                raise ValidationError(
                    "filters.analysis_cluster requires 'run_id' and 'cluster_index'",
                    field="filters.analysis_cluster",
                )
            try:
                cluster_run_id = UUID(str(run_id_raw))
                cluster_index_int = int(cluster_index_raw)
            except (ValueError, TypeError) as e:
                raise ValidationError(
                    "filters.analysis_cluster: 'run_id' must be a UUID and "
                    "'cluster_index' must be an integer",
                    field="filters.analysis_cluster",
                ) from e
            # Issue #496 security fix: pass current_workspace_id so a
            # stolen ``run_id`` from a foreign workspace returns None
            # (same shape as cluster-not-found) and the recall short-
            # circuits to empty results without leaking existence.
            cluster_memory_ids = await _analysis_query_service.get_memory_ids_in_cluster(
                self.db,
                workspace_id=effective_workspace_id,
                run_id=cluster_run_id,
                cluster_index=cluster_index_int,
            )
            if not cluster_memory_ids:
                # Cluster empty or unknown — return immediately with no candidates.
                return RecallResponse(
                    results=[],
                    explore_hints=[] if request.include_explore_hints else None,
                    confidence=self._compute_recall_confidence([]),  # #1047: "none"
                    selection_evidence=(
                        (
                            await self._build_selection_evidence(
                                selection_config,
                                eligible_ids=(),
                                request=request,
                                search_config=search_config,
                                context_id=current_context_id,
                            )
                        )[1]
                        if selection_config is not None
                        else None
                    ),
                )

        # #708 Option A H1 gate (deferred): we now know hybrid_search will
        # actually fire (the analysis_cluster short-circuit above did not
        # trigger). Probe the source workspace for a BYOK key applicable
        # to this context BEFORE issuing the embedding API call, so the
        # OPENAI_API_KEY env fallback in ``_get_user_api_key`` cannot
        # silently bypass PR #711's BYOK-only spend cap.
        #
        # The probe MUST use the per-context embedding model (same source
        # of truth ``SearchService.hybrid_search`` uses to pick its embed
        # client at line 168). Probing with the global default would
        # falsely deny self-hosted-backed contexts (provider mismatch) and
        # falsely pass OpenAI-backed contexts when the platform default is
        # self-hosted (gate would skip entirely).
        if is_shared_context_read:
            from repositories.config_repository import ContextSearchConfigRepository

            ctx_config_repo = ContextSearchConfigRepository(self.db)
            ctx_config = await ctx_config_repo.create_or_get(current_context_id)
            byok_embed_svc = EmbeddingService(self.db, model=ctx_config.embedding_model)
            # Only gate when the embedding API call would actually fire and
            # produce a charge against the source workspace. ``keyword``
            # mode skips ``embed_with_usage`` entirely (BM25-only); a
            # self-hosted backend is free/local (no platform-key fallback path).
            will_charge_embedding_cost = (
                request.search_mode != "keyword" and byok_embed_svc.provider != "self_hosted"
            )
            if will_charge_embedding_cost and not await byok_embed_svc.has_byok_key(
                str(context_workspace_id),
                context_id=str(current_context_id),
            ):
                logger.warning(
                    "shared_context_read_no_byok_deny",
                    caller_user_id=user_id,
                    caller_workspace_id=str(current_workspace_id),
                    context_id=str(current_context_id),
                    paid_by_workspace_id=str(context_workspace_id),
                    embedding_model=ctx_config.embedding_model,
                    embedding_provider=byok_embed_svc.provider,
                    reason="missing_byok",
                )
                raise NotFoundException("Context", str(current_context_id))

        # 1. Primary Retrieval: Hybrid Search (Semantic + BM25)
        # Fetch more candidates when neural is enabled for better hybrid merge
        # and to feed Hebbian learning with broader co-activation data
        candidates_k = request.k * 4 if neural_enabled else request.k
        # #1306 evaluation seam: over-fetch through the EXISTING production
        # hybrid pipeline so the evidence covers the full registered candidate
        # pool.  This changes neither scoring nor authorization and is inert for
        # every ordinary recall/bootstrap call.
        if selection_config is not None:
            candidates_k = max(candidates_k, selection_config.candidate_pool_k)
        # When ``analysis_cluster`` filter is active, ensure the candidate
        # pool can hold the whole cluster + a safety buffer so the post-
        # filter does not starve a small ``k`` request.
        if cluster_memory_ids is not None:
            candidates_k = max(candidates_k, len(cluster_memory_ids) + 50)
        search_results = await self.search_service.hybrid_search(
            query=request.query,
            user_id=user_id,
            workspace_id=str(effective_workspace_id),
            context_id=search_context_id,
            k=candidates_k,
            use_rerank=request.use_rerank,
            filters=request.filters,
            search_mode=request.search_mode,
            include_vectors=neural_enabled,
            # #708 Option A: tell SearchService this is a cross-workspace
            # shared-context read. This ONLY bypasses the redundant
            # ``is_workspace_member(workspace_id)`` check — handler-layer
            # ``_resolve_context_for_read`` is the authoritative access
            # check. The Qdrant ``user_id == caller`` filter is dropped
            # only when the context's privacy state itself is shared
            # (``ContextService.is_context_shared``); a private context
            # read across workspaces still keeps the filter so other
            # users' memories are not exposed. See loop-5 fix and the
            # ``is_shared_context`` derivation in ``SearchService.
            # hybrid_search``.
            is_shared_context_read=is_shared_context_read,
        )

        # Get full memory data from PostgreSQL
        memory_ids = [r["id"] for r in search_results]

        if not memory_ids:
            return RecallResponse(
                results=[],
                explore_hints=[] if request.include_explore_hints else None,
                confidence=self._compute_recall_confidence([]),  # #1047: "none"
                selection_evidence=(
                    (
                        await self._build_selection_evidence(
                            selection_config,
                            eligible_ids=(),
                            request=request,
                            search_config=search_config,
                            context_id=current_context_id,
                        )
                    )[1]
                    if selection_config is not None
                    else None
                ),
            )

        # Fetch memories from PostgreSQL (exclude soft-deleted)
        #
        # ``memory_ids`` are Qdrant point ids. For ``remember()``-written
        # memories the point id equals ``Memory.id`` (see
        # ``add_memory_to_qdrant`` / ``summary_embedding_id == id`` at line 348).
        # Resource-projected memories (``ResourceIndexer._apply_upsert``) instead
        # store the point under ``uuid5(resource_id:doc_id:vN) != Memory.id`` and
        # record the link in ``Memory.summary_embedding_id`` (the authoritative
        # "Qdrant point id" column). Matching on ``Memory.id`` alone silently
        # drops every resource hit during hydration → recall returns 0 in all
        # modes (Issue #972). Resolve via either identifier; the per-row lookup
        # dict below is keyed by both so ``memories.get(point_id)`` succeeds for
        # both kinds.
        pg_conditions = [
            or_(
                Memory.id.in_(memory_ids),
                Memory.summary_embedding_id.in_(memory_ids),
            ),
            Memory.deleted_at.is_(None),
        ]
        # Issue #214: source_uri_prefix and source_type post-filters
        if request.filters:
            if prefix := request.filters.get("source_uri_prefix"):
                escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pg_conditions.append(Memory.source_uri.like(f"{escaped}%", escape="\\"))
            if stype := request.filters.get("source_type"):
                pg_conditions.append(Memory.source_type == stype)
            # Issue #887: trust-tier read filter. ``trusted`` excludes
            # external-origin memories from behaviour-influencing reads (OWASP
            # LLM01/LLM03 — external content is data, not instructions).
            if request.filters.get("trust_tier") == "trusted":
                # Authoritative check: the memory's context must itself be
                # trusted. ``Context.trust_tier`` is the server-side trust
                # signal (connector contexts = 'external'), so a non-connector
                # row that somehow lives in an external context is still
                # excluded — this does not rely on the per-row source_type.
                pg_conditions.append(
                    Memory.context_id.in_(
                        select(Context.id).where(Context.trust_tier == CONTEXT_TRUST_TIER_TRUSTED)
                    )
                )
                # Defense-in-depth: also drop any connector-sourced row.
                pg_conditions.append(Memory.source_type != SOURCE_TYPE_CONNECTOR)
        # Issue #496: cluster-scoped recall — restrict to the
        # cluster's member memories. ``cluster_memory_ids`` was
        # pre-resolved above and is guaranteed non-empty by the
        # short-circuit (empty cluster returns early without
        # reaching this block).
        if cluster_memory_ids is not None:
            pg_conditions.append(Memory.id.in_(cluster_memory_ids))
        result = await self.db.execute(select(Memory).where(*pg_conditions))
        memories_list = list(result.scalars().all())
        # Key by both the row id and the Qdrant point id (summary_embedding_id)
        # so a search hit resolves whether it carries a ``remember()`` point id
        # (== Memory.id) or a resource-projected point id (#972). Keying by both
        # (rather than only the point id) keeps the lookup robust when
        # summary_embedding_id is unset/divergent. For normal memories the two
        # coincide (one key); across rows they never collide because both id
        # spaces are globally unique UUIDs.
        memories: dict[str, Memory] = {}
        for m in memories_list:
            memories[str(m.id)] = m
            if m.summary_embedding_id is not None:
                memories[str(m.summary_embedding_id)] = m

        # #1299: per-memory type/source binding filter — applied to the FULL
        # candidate pool at the service layer (REST, MCP, share-key and the
        # bootstrap recall lane all pass through here) BEFORE shadowing,
        # rerank and the top-k slice, so a denied row can never survive into
        # the slice. Enforcement is subtractive: the candidate pool is already
        # bounded to ~k by hybrid_search's own top-k truncation, so removing
        # denied rows MAY return fewer than k results — there is no backfill
        # (matching the deletion/supersede-shadowing precedent). Shadow mode
        # keeps every row and records the per-context would_deny aggregate
        # inside the helper.
        binding_row_filtered = 0
        if memories:
            from services.agent_binding_service import filter_memory_rows_by_binding

            unique_rows = list({m.id: m for m in memories.values()}.values())
            kept_rows, binding_row_filtered = await filter_memory_rows_by_binding(
                self.db, unique_rows, operation="recall", user_id=user_id
            )
            if binding_row_filtered:
                kept_ids = {m.id for m in kept_rows}
                search_results[:] = [
                    r
                    for r in search_results
                    if r["id"] not in memories or memories[r["id"]].id in kept_ids
                ]
                memories = {key: m for key, m in memories.items() if m.id in kept_ids}

        # === Issue #120: Neural Memory graph is for explore() only ===
        # recall uses pure hybrid search scores (no UnifiedScorer).
        # Hebbian learning still runs to build the graph for explore().

        # #1208: supersede shadowing — memories that are the dst of a LIVE
        # supersedes edge are demoted out of the candidate pool BEFORE the
        # re-rank and the top-k slice (so shadowing never leaves the slice
        # short). include_superseded=true keeps them, annotated. This is the
        # non-destructive counterpart of dedup's update-by-removal: the truth
        # is shadowed, not gone.
        shadow_map, contradiction_map = await self._apply_supersede_shadowing(
            search_results,
            memories,
            include_superseded=request.include_superseded,
        )

        # Issue #1048: bounded reinforce re-rank (adoption + retrieval feedback),
        # config-gated per-context (default ON since #1207), single-context only. Reorders the
        # candidate pool BEFORE the top-k slice below; uses only reference_count +
        # feedback + importance + recency — never graph signals (respects #120).
        await self._maybe_reinforce_rerank(
            search_results,
            memories,
            current_context_id if not context_ids else None,
            top_k=request.k,
            # #1220: the row prefetched at the top of recall(); None (fresh
            # context — materialized by hybrid_search after the prefetch)
            # falls back to the helper's own read.
            config=search_config,
        )
        # #1213: placebo-gated graph-boost experiment (env flag, default off —
        # bit-identical when off). Runs after reinforce and composes with it
        # via the _rerank_factor stamp.
        await self._maybe_graph_boost(
            search_results,
            memories,
            current_context_id if not context_ids else None,
            user_id,
            top_k=request.k,
        )

        selection_evidence: dict[str, Any] | None = None
        if selection_config is not None:
            # All subtractive gates (trusted context/source, agent row binding,
            # deletion, cluster scope, supersede shadowing) have run.  Therefore
            # this is precisely the authorized eligible pool used by this query.
            eligible_results: list[dict[str, Any]] = []
            eligible_ids: list[str] = []
            seen_memory_ids: set[UUID] = set()
            for search_result in search_results:
                memory = memories.get(search_result["id"])
                if memory is None or memory.id in seen_memory_ids:
                    continue
                seen_memory_ids.add(memory.id)
                eligible_results.append(search_result)
                eligible_ids.append(str(memory.id))

            # The over-fetch above (neural k*4, cluster buffers) can exceed
            # candidate_pool_k, but the stamped policy promises a bounded
            # registered pool. Truncate on the EVIDENCE side only, in
            # production rank order — eligible_results / search_results stay
            # untouched so response building and neural co-activation keep
            # seeing exactly what production saw.
            registered_pool = tuple(eligible_ids[: selection_config.candidate_pool_k])

            plan, selection_evidence = await self._build_selection_evidence(
                selection_config,
                eligible_ids=registered_pool,
                request=request,
                search_config=search_config,
                context_id=current_context_id,
            )
            by_memory_id = {str(memories[result["id"]].id): result for result in eligible_results}
            selected_set = set(plan.selected_ids)
            selected_results = [by_memory_id[memory_id] for memory_id in plan.selected_ids]
            unselected_results = [
                result
                for memory_id, result in by_memory_id.items()
                if memory_id not in selected_set
            ]
            # The normal response builder and neural co-activation path continue
            # to consume ``search_results[:k]``; reorder only, never synthesize.
            search_results[:] = selected_results + unselected_results

        responses = []
        for search_result in search_results[: request.k]:
            memory_id = search_result["id"]
            memory = memories.get(memory_id)

            if not memory:
                continue

            # Issue #1047: snapshot updated_at BEFORE update_access_stats.
            # #1317 removed the column's onupdate, so the access-stats flush
            # no longer touches (or expires) updated_at — the DB value is now
            # correct by construction; the snapshot stays as a cheap guard
            # against any future in-session dirtying of the row.
            snapshot_updated_at = memory.updated_at

            # Update access stats
            await self.memory_repo.update_access_stats(
                memory.id,
                client=request.filters.get("client", "api") if request.filters else "api",
            )

            # Check for auto-promotion
            await self._check_and_promote(memory)

            responses.append(
                MemoryResponse(
                    memory_id=memory.id,
                    summary=memory.summary,
                    context_summary=memory.context_summary,
                    type=memory.type,
                    importance=memory.importance,
                    scope=memory.scope,
                    created_at=memory.created_at,
                    updated_at=snapshot_updated_at,  # #1047: staleness cue (pre-bump snapshot)
                    client=memory.client,
                    tags=memory.tags or [],
                    context=memory.context,
                    score=search_result.get("hybrid_score", search_result["score"]),
                    source_uri=memory.source_uri,
                    source_type=memory.source_type,
                    # #1208: succession annotations. superseded_by is only
                    # non-None under include_superseded=true (default recall
                    # filtered shadowed memories out of search_results above).
                    superseded_by=shadow_map.get(memory.id),
                    contradicts=contradiction_map.get(memory.id, []),
                )
            )

        # Hebbian learning: build graph for explore() (best-effort, does not affect recall)
        if neural_enabled and request.search_mode != "keyword":
            try:
                from neural.co_activation import CoActivationTracker
                from neural.config import NeuralMemoryConfig
                from neural.hebbian import HebbianLearner
                from neural.models import ActivationState, NeuralMemoryNode
                from repositories.graph import GraphRepository
                from services.graph_service import GraphService

                config = await NeuralMemoryConfig.from_db(self.db)
                graph_repo = GraphRepository(self.db)
                await graph_repo.get_or_create(user_id)

                graph_service = GraphService(
                    user_id=user_id,
                    db=self.db,
                    workspace_id=str(effective_workspace_id) if effective_workspace_id else None,
                    context_id=str(current_context_id) if current_context_id else None,
                )

                co_activation_tracker = CoActivationTracker(config)
                await co_activation_tracker.load_from_redis(user_id)
                # #983: the learner reads/writes the cliff pending_weight on the
                # tracker's records so it survives this per-recall instance.
                hebbian_learner = HebbianLearner(graph_service, config, co_activation_tracker)

                # Only co-activate top-k results for higher-quality edges
                coactivation_k = min(config.top_k_coactivation, request.k, len(search_results))
                top_results = search_results[:coactivation_k]

                # Build NeuralMemoryNode list and score map from top results
                nodes_dict: dict[str, NeuralMemoryNode] = {}
                score_map: dict[str, float] = {}
                for search_result in top_results:
                    memory = memories.get(search_result["id"])
                    if not memory:
                        continue
                    mid = str(memory.id)
                    embedding = search_result.get("embedding", [])
                    score_map[mid] = search_result.get("hybrid_score", search_result["score"])
                    nodes_dict[mid] = NeuralMemoryNode(
                        id=mid,
                        user_id=user_id,
                        kind=memory.type,
                        text=memory.summary,
                        embedding=embedding,
                        created_at=memory.created_at,
                        last_used_at=memory.last_used_at,
                        use_count=memory.access_count or 0,
                        importance=memory.importance,
                        confidence=memory.confidence,
                        long_term=(memory.scope == "persistent"),
                    )

                # Score-weighted activation: clamp to [0, 1] for Hebbian stability
                activated_nodes = [
                    ActivationState(
                        node_id=nid, activation=min(1.0, max(0.0, score_map.get(nid, 0.0)))
                    )
                    for nid in nodes_dict
                ]

                # Co-activation tracking with semantic gating
                embedding_map = {
                    nid: node.embedding for nid, node in nodes_dict.items() if node.embedding
                }
                # Resolve the calibrated edge-gate threshold (#982). The gate is
                # keyed on the context's embedding (model, dimensions); both the
                # co-activation tracker and the Hebbian learner use the same
                # resolved value so they agree on which pairs may form edges.
                # Best-effort: on any failure (or no edge_gate calibration row)
                # this stays None and the gate falls back to the config absolute
                # ``min_similarity_for_edge`` inside record_activation/queue_update.
                edge_threshold: float | None = None
                edge_dims = next((len(e) for e in embedding_map.values() if e), None)
                if edge_dims and current_context_id is not None:
                    # Resolve in an ISOLATED session: a transient DB error here
                    # must not abort the recall's own transaction (which still
                    # has the neural graph writes below to commit). The lookup
                    # is READ-ONLY (get_by_context, never create_or_get) so the
                    # recall hot path never writes a config row. Missing config
                    # or any failure leaves edge_threshold=None → the gate falls
                    # back to the absolute config value in the gating calls.
                    from db.base import get_db
                    from neural.calibration import resolve_edge_threshold
                    from repositories.config_repository import (
                        ContextSearchConfigRepository,
                    )

                    try:
                        async for edge_db in get_db():
                            ctx_search_cfg = await ContextSearchConfigRepository(
                                edge_db
                            ).get_by_context(current_context_id)
                            if ctx_search_cfg is not None:
                                edge_threshold = await resolve_edge_threshold(
                                    db=edge_db,
                                    config=config,
                                    model_name=ctx_search_cfg.embedding_model,
                                    dimensions=edge_dims,
                                )
                            break
                    except Exception:  # noqa: BLE001 — best-effort; never break recall
                        logger.debug("edge_threshold_resolve_failed", exc_info=True)
                        edge_threshold = None

                # 2D edge gate (#983): when enabled, lower the recording gate
                # to the floor so band pairs accumulate same-event evidence,
                # and hand those counts to the Hebbian gate below. The floor
                # is clamped to the effective threshold so it can only widen
                # the band downward, never tighten the 1-D gate.
                edge_floor: float | None = None
                if config.edge_gate_repetition_enabled:
                    effective_threshold = (
                        edge_threshold
                        if edge_threshold is not None
                        else config.min_similarity_for_edge
                    )
                    edge_floor = min(config.min_similarity_for_edge_floor, effective_threshold)

                # Distinct-query evidence dedup (#983): the same query
                # replayed N times re-produces its top-k — one ranking
                # accident is one observation, however often it repeats.
                query_event_key = hashlib.sha256(request.query.encode("utf-8")).hexdigest()[:16]

                updated_records = co_activation_tracker.record_activation(
                    user_id,
                    activated_nodes,
                    embeddings=embedding_map,
                    similarity_threshold=edge_threshold,
                    floor_threshold=edge_floor,
                    event_key=query_event_key,
                )
                co_activation_counts = (
                    {(r.node_id_1, r.node_id_2): r.same_event_count for r in updated_records}
                    if edge_floor is not None
                    else None
                )
                # NOTE: save_to_redis is deferred until AFTER apply_updates so
                # the persisted records also capture the cliff pending_weight
                # the Hebbian pass writes (#983).

                # Add nodes to graph
                nodes_added = 0
                for node_id, node in nodes_dict.items():
                    if not await graph_service.has_node(node_id):
                        await graph_service.add_node(
                            node_id=node_id,
                            node_type="memory",
                            data={
                                "user_id": user_id,
                                "kind": node.kind,
                                "text": node.text,
                                "created_at": node.created_at,
                                "importance": node.importance,
                                "confidence": node.confidence,
                                "long_term": node.long_term,
                            },
                        )
                        nodes_added += 1

                # Hebbian updates (same calibrated gate as co-activation above;
                # band pairs may be admitted by repetition evidence, #983)
                await hebbian_learner.queue_update(
                    user_id,
                    activated_nodes,
                    nodes_dict,
                    similarity_threshold=edge_threshold,
                    floor_threshold=edge_floor,
                    co_activation_counts=co_activation_counts,
                )
                edges_updated = await hebbian_learner.apply_updates(user_id)

                # #983: persist co-activation records now — this captures both
                # the same-event evidence (record_activation) and the cliff
                # pending_weight (apply_updates) in a single round-trip.
                await co_activation_tracker.save_to_redis(user_id)

                logger.info(
                    "graph_updated",
                    user_id=user_id,
                    nodes_added=nodes_added,
                    edges_updated=edges_updated,
                )
            except Exception as exc:
                logger.warning("hebbian_update_failed", error=str(exc))

        await self.db.commit()

        # Issue #216: Generate explore hints (best-effort, opt-in)
        explore_hints = [] if request.include_explore_hints else None
        if request.include_explore_hints and responses:
            try:
                explore_hints = await self._generate_explore_hints(
                    responses,
                    user_id,
                    current_context_id,
                    effective_workspace_id,
                    neural_enabled=neural_enabled,
                )
            except Exception as exc:
                logger.warning("explore_hints_generation_failed", error=str(exc))

        # Issue #104: Aggregate related tags from results
        related_tags = self._aggregate_related_tags(responses, limit=10)

        logger.info("recall_completed", user_id=user_id, results=len(responses))

        # Issue #1047/#1052: relevance confidence from the full candidate pool's
        # score distribution (search_results, which holds up to candidates_k >> k
        # when neural is on). ``candidate_scores`` are the normalized hybrid scores
        # (ranking order); ``semantic_scores`` are the RAW per-memory cosines that
        # carry absolute match strength for absence detection (#1052). See
        # _compute_recall_confidence.
        candidate_scores = [
            r.get("hybrid_score", r["score"])
            for r in search_results
            if r.get("hybrid_score", r.get("score")) is not None
        ]
        semantic_scores = [
            r["semantic_score_raw"]
            for r in search_results
            if r.get("semantic_score_raw") is not None
        ]
        # #1278/#1281 item 7: audit the recall (no-op unless verified agent
        # identity). result_count + a keyed hash of the query; raw query never
        # stored. effective_workspace_id is the #708-aware search workspace.
        # #1299: enforce-mode row filtering is not a request deny — the count
        # rides this success row so the subtraction stays observable without
        # corrupting the deny metrics.
        from services.agent_binding_service import ROW_FILTER_KIND
        from services.memory_access_event_writer import emit_memory_access_event

        await emit_memory_access_event(
            operation="recall",
            outcome="success",
            workspace_id=effective_workspace_id,
            user_id=user_id,
            context_id=current_context_id,
            result_count=len(responses),
            query=request.query,
            extra_metadata=(
                {
                    "filter_kind": ROW_FILTER_KIND,
                    "binding_row_filtered_count": binding_row_filtered,
                }
                if binding_row_filtered
                else None
            ),
        )

        return RecallResponse(
            results=responses,
            related_tags=related_tags,
            explore_hints=explore_hints,
            confidence=self._compute_recall_confidence(
                candidate_scores, semantic_scores=semantic_scores or None
            ),
            selection_evidence=selection_evidence,
        )

    async def load_pinned(
        self,
        user_id: str,
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
        cap: int | str | None = None,
        key_workspace_id: UUID | None = None,  # Issue #963/#1281: pure key scope
        trusted_only: bool = False,
    ) -> LoadPinnedResponse:
        """Deterministically load a context's always-delivery memories (#886).

        The deterministic counterpart to ``recall()``: returns the complete,
        unranked, ordered always-load set for the bound context — no embedding,
        no Qdrant, no rerank. Bounded by ``cap`` (default
        ``settings.pinned_load_cap``); when the pinned set exceeds it, the
        response flags ``truncated`` with the true ``total_available`` and logs
        a warning (never a silent truncation). Items carry L1 + L2 only; full
        content is fetched on demand via ``reference()``.

        Args:
            user_id: Caller user ID.
            current_context_id: Bound context (the always-load set is per-context).
            current_workspace_id: Bound workspace (isolation scope).
            cap: Optional override for the hard cap (defaults to settings).
            trusted_only: #1293 — when set (the agent-bootstrap pinned lane),
                apply the recall trusted-tier gate so external/connector-origin
                rows never establish behaviour at session start. Left False for
                the user-initiated standalone ``load_pinned`` surface.

        Returns:
            LoadPinnedResponse with the bounded ordered set + truncation flags.
        """
        from config.settings import get_settings

        context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id,
            current_context_id,
            key_workspace_id=key_workspace_id,
            operation="load_pinned",  # #1286 (P0-5): deny-capture audit identity
        )
        if not workspace_id_str or not context_id_str:
            raise ValueError("load_pinned() requires current_context_id")

        effective_cap = self._clamp_pinned_cap(cap, get_settings().pinned_load_cap)

        rows, total = await self.memory_repo.list_pinned(
            UUID(workspace_id_str), UUID(context_id_str), effective_cap, trusted_only=trusted_only
        )
        truncated = total > effective_cap

        # #1299: per-memory type/source binding filter — the deterministic
        # pinned lane returns memory rows to agent credentials (standalone
        # load_pinned + the bootstrap pinned component), so the same
        # subtractive rule as recall applies. total_available stays the repo
        # count (the context's pinned-set size); the filter narrows what THIS
        # credential receives.
        from services.agent_binding_service import filter_memory_rows_by_binding

        rows, pinned_row_filtered = await filter_memory_rows_by_binding(
            self.db, list(rows), operation="load_pinned", user_id=user_id
        )
        if truncated:
            logger.warning(
                "pinned_load_capped",
                context_id=context_id_str,
                workspace_id=workspace_id_str,
                total_available=total,
                cap=effective_cap,
            )

        # #1278: append-only audit (no-op unless verified agent identity).
        # #1299: the enforce-mode row-filter count rides the success row.
        from services.agent_binding_service import ROW_FILTER_KIND
        from services.memory_access_event_writer import emit_memory_access_event

        await emit_memory_access_event(
            operation="load_pinned",
            outcome="success",
            workspace_id=UUID(workspace_id_str),
            user_id=user_id,
            context_id=UUID(context_id_str),
            result_count=len(rows),
            extra_metadata=(
                {
                    "filter_kind": ROW_FILTER_KIND,
                    "binding_row_filtered_count": pinned_row_filtered,
                }
                if pinned_row_filtered
                else None
            ),
        )

        return LoadPinnedResponse(
            memories=[
                PinnedMemoryItem(
                    memory_id=m.id,
                    summary=m.summary,
                    context_summary=m.context_summary,
                    type=m.type,
                    importance=m.importance,
                    delivery_mode=m.delivery_mode,
                    created_at=m.created_at,
                )
                for m in rows
            ],
            total_available=total,
            truncated=truncated,
            cap=effective_cap,
        )

    async def forget(
        self,
        request: ForgetRequest,
        user_id: str,
        current_context_id: UUID | None = None,
        key_workspace_id: UUID | None = None,  # Issue #963/#1281: pure key scope
    ) -> ForgetResponse:
        """Delete memory (single or multiple via query).

        Issue #82: Context-based multi-collection support.

        Args:
            request: Forget request (memory_id or query)
            user_id: User ID
            current_context_id: Current context UUID (Issue #82)

        Returns:
            ForgetResponse with deleted count and IDs
        """
        # Single Collection Migration: Extract isolation params (optimized).
        # Issue #1275: forget is a WRITE — gate the declared context against
        # the agent binding (no-op for non-agent credentials).
        context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id,
            current_context_id,
            access="write",
            key_workspace_id=key_workspace_id,
            operation="forget",  # #1286 (P0-5): deny-capture audit identity
        )

        deleted_ids = []
        # #1286 item 2 (P0-5): the deleted rows' own (hard-validated)
        # workspace/context — the audit fallback when no context is declared.
        deleted_workspace_ids: set[UUID] = set()
        deleted_context_ids: set[UUID] = set()

        # Case 1: Delete by memory_id
        if request.memory_id:
            # #1320: repo.get() excludes soft-deleted rows by default — an
            # already-deleted memory is not re-stamped or counted again
            # (deleted_count=0, same as a never-existed id).
            memory = await self.memory_repo.get(request.memory_id)

            if memory:
                # Issue #XXX: Team collaboration - verify delete permission
                from services.permission_service import CallerId, MemoryAuthorId, PermissionService

                perm_service = PermissionService(self.db)
                can_access = await perm_service.can_access_memory(
                    user_id=CallerId(user_id),
                    memory_user_id=MemoryAuthorId(memory.user_id),
                    workspace_id=memory.workspace_id,
                    context_id=memory.context_id,
                    access="write",  # #1275: WRITE path — can_read-only binding must not permit mutation
                    operation="forget",  # #1286 (P0-5): the denied row is the ONLY
                    memory_id=request.memory_id,  # record — this deny is a silent empty success
                    memory_type=memory.type,  # #1299: per-memory type/source filter
                    memory_source_type=memory.source_type,
                )

                if not can_access:
                    logger.warning(
                        "forget_access_denied",
                        memory_id=str(request.memory_id),
                        user_id=user_id,
                    )
                    # Return empty response instead of error for security
                    return ForgetResponse(deleted_count=0, memory_ids=[])
                # Migration 063: Get workspace_id/context_id from memory directly
                # CRITICAL: Validate workspace_id/context_id are not NULL (data integrity)
                if not memory.workspace_id or not memory.context_id:
                    raise ValueError(
                        f"Memory {memory.id} has NULL workspace_id/context_id. "
                        "This indicates data migration issue. Run Migration 063."
                    )

                memory_workspace_id = str(memory.workspace_id)
                memory_context_id = str(memory.context_id)
                deleted_workspace_ids.add(memory.workspace_id)
                deleted_context_ids.add(memory.context_id)

                # Soft delete in PostgreSQL (set deleted_at, deleted_by)

                memory.deleted_at = utcnow()
                memory.deleted_by = user_id
                await self.memory_repo.update(memory.id, memory)

                # Hard delete from Qdrant (remove from search index)
                del_collection = await resolve_collection_name(self.db, memory.context_id)
                await delete_memory_from_qdrant(
                    user_id, request.memory_id, collection_name=del_collection
                )

                # Clean up neural memory edges with 3-level isolation
                from repositories.neural_edge import NeuralEdgeRepository

                edge_repo = NeuralEdgeRepository(self.db)
                edges_deleted = await edge_repo.delete_node_edges(
                    user_id=user_id,
                    node_id=request.memory_id,
                    workspace_id=memory_workspace_id,
                    context_id=memory_context_id,
                )
                if edges_deleted > 0:
                    logger.info(
                        "neural_edges_cleaned",
                        memory_id=str(request.memory_id),
                        edges_deleted=edges_deleted,
                        user_id=user_id,
                    )

                deleted_ids.append(request.memory_id)

                logger.info(
                    "memory_soft_deleted", memory_id=str(request.memory_id), user_id=user_id
                )

        # Case 2: Delete by query (search and delete)
        elif request.query:
            # Search for matching memories
            # #1212: pin the historical mode explicitly — the query router
            # must never decide the candidate set of a DESTRUCTIVE operation
            # (an explicit search_mode always wins, in every routing_mode).
            recall_request = RecallRequest(
                query=request.query,
                k=request.k,
                use_rerank=False,  # No reranking for delete
                filters=None,
                # #1208: superseded memories must stay deletable by query —
                # default shadowing would hide them from this search and
                # make forget(query=...) silently skip them.
                include_superseded=True,
                search_mode="hybrid",
            )

            # Issue #82: Pass project ID to recall
            search_response = await self.recall(recall_request, user_id, current_context_id)

            # Soft delete each found memory
            for memory_response in search_response.results:
                memory = await self.memory_repo.get(memory_response.memory_id)
                if memory:
                    memory.deleted_at = utcnow()
                    memory.deleted_by = user_id
                    await self.memory_repo.update(memory.id, memory)

                    # Hard delete from Qdrant
                    del_collection = await resolve_collection_name(self.db, memory.context_id)
                    await delete_memory_from_qdrant(
                        user_id, memory_response.memory_id, collection_name=del_collection
                    )

                    # Clean up neural memory edges
                    from repositories.neural_edge import NeuralEdgeRepository

                    edge_repo = NeuralEdgeRepository(self.db)
                    edges_deleted = await edge_repo.delete_node_edges(
                        user_id, memory_response.memory_id
                    )
                    if edges_deleted > 0:
                        logger.info(
                            "neural_edges_cleaned",
                            memory_id=str(memory_response.memory_id),
                            edges_deleted=edges_deleted,
                            user_id=user_id,
                        )

                    deleted_ids.append(memory_response.memory_id)
                    if memory.workspace_id:
                        deleted_workspace_ids.add(memory.workspace_id)
                    if memory.context_id:
                        deleted_context_ids.add(memory.context_id)

            logger.info(
                "memories_soft_deleted_by_query",
                query=request.query,
                count=len(deleted_ids),
                user_id=user_id,
            )

        await self.db.commit()

        # #1286 item 2 (P0-5): audit the destructive write (no-op unless a
        # verified agent). The REST route declares no context (#246), so the
        # isolation helper resolves no workspace on that surface — fall back
        # to the deleted rows' own workspace/context (unambiguous only when
        # every deleted row shares one) so the audit row is never silently
        # dropped on the REST face (#1291/#1292 parity lesson).
        emit_workspace = UUID(workspace_id_str) if workspace_id_str else None
        emit_context = UUID(context_id_str) if context_id_str else None
        if emit_workspace is None and len(deleted_workspace_ids) == 1:
            emit_workspace = next(iter(deleted_workspace_ids))
        if emit_context is None and len(deleted_context_ids) == 1:
            emit_context = next(iter(deleted_context_ids))

        from services.memory_access_event_writer import (
            MAX_METADATA_MEMORY_IDS,
            emit_memory_access_event,
        )

        await emit_memory_access_event(
            operation="forget",
            outcome="success",
            workspace_id=emit_workspace,
            user_id=user_id,
            context_id=emit_context,
            memory_id=deleted_ids[0] if len(deleted_ids) == 1 else None,
            result_count=len(deleted_ids),
            extra_metadata=(
                {"memory_ids": [str(mid) for mid in deleted_ids[:MAX_METADATA_MEMORY_IDS]]}
                if len(deleted_ids) > 1
                else None
            ),
        )

        return ForgetResponse(deleted_count=len(deleted_ids), memory_ids=deleted_ids)

    async def _check_and_promote(self, memory: Memory) -> None:
        """Check and promote working memory to persistent.

        Args:
            memory: Memory entity
        """
        if memory.scope == "persistent":
            return

        age_days = (utcnow() - memory.created_at).days

        if should_promote_to_persistent(
            access_count=memory.access_count or 0,
            age_days=age_days,
            importance=memory.importance,
            accessed_by_clients=memory.accessed_by_clients or [],
        ):
            await self.memory_repo.promote_to_persistent(memory.id)
            await self.db.commit()

            logger.info(
                "memory_promoted",
                memory_id=str(memory.id),
                access_count=memory.access_count,
                age_days=age_days,
                importance=memory.importance,
            )

    async def cleanup_old_working_memories(self, user_id: str, age_days: int = 30) -> int:
        """Cleanup old working memories (30+ days, not promoted).

        Args:
            user_id: User ID
            age_days: Minimum age in days (default: 30)

        Returns:
            Number of deleted memories
        """
        from config.retention import WORKING_MEMORY_RETENTION_DAYS

        age_days = age_days or WORKING_MEMORY_RETENTION_DAYS

        # Get old working memories
        old_memories = await self.memory_repo.get_old_working_memories(user_id, age_days)

        deleted_count = 0

        for memory in old_memories:
            # Skip if already promoted or high importance
            if memory.scope == "persistent" or memory.importance >= 0.7:
                continue

            # Delete from PostgreSQL
            await self.memory_repo.delete(memory.id)

            # Delete from Qdrant
            del_collection = await resolve_collection_name(self.db, memory.context_id)
            await delete_memory_from_qdrant(user_id, memory.id, collection_name=del_collection)

            deleted_count += 1

        await self.db.commit()

        logger.info(
            "old_memories_cleaned_up",
            user_id=user_id,
            deleted=deleted_count,
            age_days=age_days,
        )

        return deleted_count

    async def explore(
        self,
        request: ExploreRequest,
        user_id: str,
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
    ) -> ExploreResponse:
        """Explore related memories via graph traversal with 3-level isolation.

        Neural Memory graph exploration using Activation Spreading.
        Single Collection Migration: Added workspace_id and context_id for isolation.

        Args:
            request: Explore request
            user_id: User ID
            current_context_id: Current context UUID
            current_workspace_id: Workspace ID

        Returns:
            ExploreResponse with related memories

        Example:
            >>> result = await service.explore(request, "kiyota")
            >>> result.related_memories
            [RelatedMemoryResponse(...), ...]
        """
        from sqlalchemy import select

        from neural.activation import ActivationSpreader
        from neural.config import NeuralMemoryConfig
        from repositories.graph import GraphRepository
        from services.graph_service import GraphService

        logger.info(
            "explore_request",
            user_id=user_id,
            memory_id=str(request.memory_id),
            depth=request.depth,
        )

        # 1. Get seed memory. #1316: repo.get() excludes soft-deleted rows by
        # default — a forgotten memory must not surface as an exploration seed
        # (its edges are already gone, so it previously leaked as
        # seed_not_in_graph with the summary exposed). The not-found message
        # below MUST stay byte-identical to the access-denied raise so a
        # tombstoned, never-existed, and denied id are indistinguishable.
        seed_memory = await self.memory_repo.get(request.memory_id)
        if not seed_memory:
            raise NotFoundException("Memory", str(request.memory_id))

        # Issue #XXX: Team collaboration - verify access permission
        from services.permission_service import CallerId, MemoryAuthorId, PermissionService

        perm_service = PermissionService(self.db)
        # #1286 (P0-5): no operation threaded — "explore" is outside the
        # MAE_OPERATIONS vocabulary; its denies stay log-only until the
        # vocabulary grows (CHECK widening = a migration, out of scope here).
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(seed_memory.user_id),
            workspace_id=seed_memory.workspace_id,
            context_id=seed_memory.context_id,
            memory_type=seed_memory.type,  # #1299: per-memory type/source filter
            memory_source_type=seed_memory.source_type,
        )

        if not can_access:
            # Two-arg form — byte-identical message to the not-found raise
            # above (uniform 404; no existence oracle, and no doubled
            # "not found not found" from the constructor's own suffix).
            raise NotFoundException("Memory", str(request.memory_id))

        # Migration 063: Get workspace_id and context_id directly from seed memory
        # CRITICAL: Validate seed memory has workspace_id/context_id (data integrity)
        if not seed_memory.workspace_id or not seed_memory.context_id:
            raise ValueError(
                f"Seed memory {seed_memory.id} has NULL workspace_id/context_id. "
                "This indicates data migration issue. Run Migration 063."
            )

        if not current_context_id:
            current_context_id = seed_memory.context_id
        if not current_workspace_id:
            current_workspace_id = seed_memory.workspace_id

        # 2. Load user's graph
        graph_repo = GraphRepository(self.db)
        await graph_repo.get_or_create(user_id)

        # 3. Convert to GraphService with 3-level isolation
        graph_service = GraphService(
            user_id=user_id,
            db=self.db,
            workspace_id=str(current_workspace_id) if current_workspace_id else None,
            context_id=str(current_context_id) if current_context_id else None,
        )

        # 4. Check if seed node exists in graph
        if not await graph_service.has_node(str(request.memory_id)):
            # Return empty result if seed not in graph
            seed_response = MemoryResponse(
                memory_id=seed_memory.id,
                summary=seed_memory.summary,
                context_summary=seed_memory.context_summary,
                type=seed_memory.type,
                importance=seed_memory.importance,
                scope=seed_memory.scope,
                created_at=seed_memory.created_at,
                client=seed_memory.client,
                tags=seed_memory.tags or [],
                context=seed_memory.context,
                score=None,
            )

            await self.memory_repo.update_access_stats(seed_memory.id, client="api")
            await self.db.commit()

            return ExploreResponse(
                seed_memory=seed_response,
                related_memories=[],
                metadata={
                    "total_activated": 0,
                    "returned": 0,
                    "reason": "seed_not_in_graph",
                },
            )

        # 5. Activation spreading (Issue #107: DB-driven config)
        config = await NeuralMemoryConfig.from_db(self.db)
        spreader = ActivationSpreader(graph_service, config)

        seed_activations = {str(request.memory_id): 1.0}
        activated = await spreader.spread(
            seed_activations=seed_activations,
            max_hops=request.depth,
            user_id=user_id,
        )

        # 6. Filter by weight and exclude seed
        filtered_activations = [
            act
            for act in activated
            if act.node_id != str(request.memory_id)  # Exclude seed
            and act.hop > 0  # Only non-seed nodes
        ]

        # Filter by min_weight
        if request.min_weight > 0:
            filtered_activations = [
                act for act in filtered_activations if act.activation >= request.min_weight
            ]

        # 7. Get memory details from PostgreSQL
        memory_ids = [UUID(act.node_id) for act in filtered_activations[:50]]  # Limit to 50

        if not memory_ids:
            seed_response = MemoryResponse(
                memory_id=seed_memory.id,
                summary=seed_memory.summary,
                context_summary=seed_memory.context_summary,
                type=seed_memory.type,
                importance=seed_memory.importance,
                scope=seed_memory.scope,
                created_at=seed_memory.created_at,
                client=seed_memory.client,
                tags=seed_memory.tags or [],
                context=seed_memory.context,
                score=None,
            )

            await self.memory_repo.update_access_stats(seed_memory.id, client="api")
            await self.db.commit()

            return ExploreResponse(
                seed_memory=seed_response,
                related_memories=[],
                metadata={
                    "total_activated": len(activated),
                    "returned": 0,
                    "filtered_out": len(activated)
                    - len(
                        [a for a in activated if a.hop > 0 and a.activation >= request.min_weight]
                    ),
                    "suggestion": f"All {len(activated)} activated nodes filtered out by min_weight={request.min_weight}. Try lowering to 0.0-0.05 for results (typical edge weights: 0.02-0.05).",
                },
            )

        result = await self.db.execute(
            select(Memory).where(
                Memory.id.in_(memory_ids),
                Memory.deleted_at.is_(None),  # Exclude deleted memories
            )
        )
        memories = {str(m.id): m for m in result.scalars().all()}

        # #1299: drop neighbors the binding's type/source filter denies at
        # materialization (enforce); shadow keeps them — log-only, since
        # "explore" is outside the MAE vocabulary. Known limitation: a
        # disallowed node can still RELAY activation to allowed hop-2 nodes
        # (traversal pruning would touch the ActivationSpreader shared with
        # recall's neural scorer); its content/metadata are never returned.
        from services.agent_binding_service import filter_memory_rows_by_binding

        kept_neighbors, _ = await filter_memory_rows_by_binding(
            self.db, list(memories.values()), operation=None, user_id=user_id
        )
        memories = {str(m.id): m for m in kept_neighbors}

        # 8. Build response
        related_memories = []
        activation_map = {act.node_id: act for act in filtered_activations}

        for memory_id, memory in memories.items():
            activation = activation_map.get(memory_id)
            if not activation:
                continue

            # Get edge weight
            edge_data = await graph_service.get_edge(str(request.memory_id), memory_id)
            weight = edge_data["weight"] if edge_data else 0.0

            # Reconstruct path (simple: seed -> current)
            path = [request.memory_id, UUID(memory_id)]

            related_memories.append(
                RelatedMemoryResponse(
                    memory_id=memory.id,
                    summary=memory.summary,
                    context_summary=memory.context_summary,
                    type=memory.type,
                    activation=activation.activation,
                    hop=activation.hop,
                    weight=weight,
                    path=path,
                )
            )

        # Sort by activation (descending)
        related_memories.sort(key=lambda x: x.activation, reverse=True)

        # Limit to top 10
        related_memories = related_memories[:10]

        seed_response = MemoryResponse(
            memory_id=seed_memory.id,
            summary=seed_memory.summary,
            context_summary=seed_memory.context_summary,
            type=seed_memory.type,
            importance=seed_memory.importance,
            scope=seed_memory.scope,
            created_at=seed_memory.created_at,
            client=seed_memory.client,
            tags=seed_memory.tags or [],
            context=seed_memory.context,
            score=None,
        )

        await self.memory_repo.update_access_stats(seed_memory.id, client="api")
        for related in related_memories:
            await self.memory_repo.update_access_stats(related.memory_id, client="api")
        await self.db.commit()

        logger.info(
            "explore_completed",
            user_id=user_id,
            total_activated=len(activated),
            returned=len(related_memories),
        )

        return ExploreResponse(
            seed_memory=seed_response,
            related_memories=related_memories,
            metadata={
                "total_activated": len(activated),
                "returned": len(related_memories),
                "filtered_out": len(activated) - 1 - len(filtered_activations),  # -1 for seed
                "max_activation": max(
                    [act.activation for act in filtered_activations], default=0.0
                ),
                "min_activation": min([act.activation for act in filtered_activations], default=0.0)
                if filtered_activations
                else 0.0,
            },
        )

    async def get_stats(
        self,
        user_id: str,
        workspace_id: str | None = None,
        context_id: str | None = None,
        include_details: bool = True,
        time_window_hours: int = 168,
        is_shared_context: bool = False,
    ) -> MemoryStatsResponse:
        """Get memory usage statistics with 3-level isolation.

        Single Collection Migration: Uses workspace_id/context_id instead of collection_name.

        Args:
            user_id: User ID
            workspace_id: Workspace ID (for context-scoped stats)
            context_id: Context ID (for context-scoped stats)
            include_details: Include type/importance breakdown
            time_window_hours: Recent activity window in hours (default: 168 = 7 days)
            is_shared_context: If True, count all members' memories (not just user_id)

        Returns:
            MemoryStatsResponse with counts and breakdowns
        """
        from datetime import timedelta

        from sqlalchemy import and_, case, func, literal_column, select

        from models.memory import Memory
        from models.schemas import MemoryStatsResponse

        # Build base filter conditions
        # Issue #204: For shared contexts, count all members' memories
        # - Private contexts: Filter by user_id (creator's memories only)
        # - Shared contexts: No user_id filter (all members' memories)
        base_conditions = [Memory.deleted_at.is_(None)]
        if not is_shared_context:
            base_conditions.append(Memory.user_id == user_id)  # Private: creator only

        # Single Collection Migration: Filter by workspace_id and context_id
        if workspace_id:
            base_conditions.append(Memory.workspace_id == UUID(workspace_id))
        if context_id:
            base_conditions.append(Memory.context_id == UUID(context_id))

        # Total count (exclude soft-deleted)
        total_result = await self.db.execute(
            select(func.count(Memory.id)).where(and_(*base_conditions))
        )
        total_count = total_result.scalar() or 0

        # Working vs Persistent (exclude soft-deleted)
        working_result = await self.db.execute(
            select(func.count(Memory.id)).where(and_(*base_conditions, Memory.scope == "working"))
        )
        working_count = working_result.scalar() or 0
        persistent_count = total_count - working_count

        by_type = {}
        by_importance = {}
        recent_activity = 0

        # Add details if requested
        if include_details:
            # By type
            type_result = await self.db.execute(
                select(Memory.type, func.count(Memory.id))
                .where(and_(*base_conditions))
                .group_by(Memory.type)
            )
            by_type = {row[0]: row[1] for row in type_result.all()}

            # By importance level
            importance_case = case(
                (Memory.importance >= 0.7, literal_column("'high'")),
                (Memory.importance >= 0.4, literal_column("'medium'")),
                else_=literal_column("'low'"),
            )

            importance_result = await self.db.execute(
                select(importance_case, func.count(Memory.id))
                .where(and_(*base_conditions))
                .group_by(importance_case)
            )
            by_importance = {row[0]: row[1] for row in importance_result.all()}

            # Recent activity (exclude soft-deleted)
            recent_result = await self.db.execute(
                select(func.count(Memory.id)).where(
                    and_(
                        *base_conditions,
                        Memory.created_at >= utcnow() - timedelta(hours=time_window_hours),
                    )
                )
            )
            recent_activity = recent_result.scalar() or 0

        return MemoryStatsResponse(
            total_count=total_count,
            working_count=working_count,
            persistent_count=persistent_count,
            by_type=by_type,
            by_importance=by_importance,
            recent_activity=recent_activity,
        )

    def _aggregate_related_tags(
        self, responses: list[MemoryResponse], limit: int = 10
    ) -> list[RelatedTagItem]:
        """Aggregate tags from recall results.

        Issue #104: Extract related tags from search results to help LLMs
        understand tag context without overwhelming them with all tags.

        Args:
            responses: List of memory responses
            limit: Maximum number of tags to return (default: 10)

        Returns:
            List of RelatedTagItem with tag, count, and sample_summary
        """
        from collections import Counter

        # Count tag occurrences
        tag_counter: Counter[str] = Counter()
        tag_samples: dict[str, str] = {}  # tag -> first summary encountered

        for response in responses:
            if response.tags:
                for tag in response.tags:
                    tag_counter[tag] += 1
                    # Store first summary as sample
                    if tag not in tag_samples:
                        tag_samples[tag] = response.summary

        # Sort by count (descending) and take top N
        top_tags = tag_counter.most_common(limit)

        # Build RelatedTagItem list
        related_tags = [
            RelatedTagItem(
                tag=tag,
                count=count,
                sample_summary=tag_samples.get(tag),
            )
            for tag, count in top_tags
        ]

        return related_tags

    async def _generate_explore_hints(
        self,
        responses: list[MemoryResponse],
        user_id: str,
        context_id: UUID | None,
        workspace_id: UUID | None,
        neural_enabled: bool = False,
    ) -> list[ExploreHint]:
        """Generate up to 3 explore hints from recall results.

        Issue #216: Best-effort hints for graph discovery bridging.
        Failures never propagate — caller wraps in try/except.

        Hint selection:
          1. top_result: highest-scored result
          2. high_centrality: top-3 result with most edges
          3. unexplored_neighbor: top-3 result with edges + created in last 7d
        """
        from datetime import timedelta

        from repositories.neural_edge import NeuralEdgeRepository

        hints: list[ExploreHint] = []
        if not responses:
            return hints

        # hint #1: top result (always available, no graph query needed)
        hints.append(ExploreHint(memory_id=responses[0].memory_id, reason="top_result"))

        # Graph-derived hints require neural memory to be enabled
        if not neural_enabled:
            return hints

        # For hints #2 and #3, query edge counts for top-3 results
        top_n = responses[:3]
        edge_repo = NeuralEdgeRepository(self.db)

        degree_map: dict[UUID, int] = {}
        for resp in top_n:
            try:
                in_deg, out_deg = await edge_repo.get_node_degree(user_id, resp.memory_id)
                degree_map[resp.memory_id] = in_deg + out_deg
            except Exception:
                degree_map[resp.memory_id] = 0

        # hint #2: high_centrality — top-3 result with highest edge count
        used_ids = {hints[0].memory_id}
        candidates = [
            (mid, deg) for mid, deg in degree_map.items() if deg > 0 and mid not in used_ids
        ]
        if candidates:
            best = max(candidates, key=lambda x: x[1])
            hints.append(ExploreHint(memory_id=best[0], reason="high_centrality"))
            used_ids.add(best[0])

        # hint #3: unexplored_neighbor — has edges + created in last 7 days
        cutoff = utcnow() - timedelta(days=7)
        for resp in top_n:
            if resp.memory_id in used_ids:
                continue
            deg = degree_map.get(resp.memory_id, 0)
            # Normalize to naive datetime for comparison (DB stores naive UTC)
            created = (
                resp.created_at.replace(tzinfo=None)
                if resp.created_at and resp.created_at.tzinfo
                else resp.created_at
            )
            if deg > 0 and created and created >= cutoff:
                hints.append(ExploreHint(memory_id=resp.memory_id, reason="unexplored_neighbor"))
                break

        return hints


async def _create_knn_seed_edges(
    db: AsyncSession,
    memory: Memory,
    vector: list[float],
    collection_name: str,
    model_name: str,
) -> None:
    """Create k-NN cold-start seed edges for a newly embedded memory.

    Issue #221: After a memory is upserted to Qdrant, search for its k nearest
    neighbors within the same workspace+context and create weak
    `semantic_similarity` edges. This prevents new memories from being born as
    isolated nodes (graph cold-start UX failure).

    Issue #406 (Phase B): the similarity threshold is resolved via
    ``neural.calibration.resolve_knn_threshold`` which implements the D4
    fallback chain (operator override → model-global calibration percentile →
    disabled). When ``resolve_knn_threshold`` returns ``None`` (no
    calibration row yet), seeding is skipped for this call — the bootstrap
    trigger in the remember() path will populate the row once the context
    crosses D3, and subsequent calls will use the calibrated threshold.

    Best-effort: any failure is logged and swallowed — must not affect memory
    creation. Edge creation uses an isolated commit so partial failures do not
    roll back unrelated work.

    Args:
        db: AsyncSession (already bound to memory's transaction context)
        memory: The freshly created Memory entity
        vector: The dense embedding vector that was upserted to Qdrant.
            Its length is the embedding dimensionality passed to calibration.
        collection_name: Qdrant collection name (varies by embedding model)
        model_name: Embedding model name (e.g. ``text-embedding-3-small``).
            Combined with ``len(vector)`` to key the calibration lookup.
    """
    from db.qdrant import search_memories_qdrant
    from neural.calibration import resolve_knn_threshold
    from neural.config import NeuralMemoryConfig
    from repositories.neural_edge import NeuralEdgeRepository

    try:
        config = await NeuralMemoryConfig.from_db(db)

        if not config.knn_seed_enabled:
            return

        if not memory.workspace_id or not memory.context_id:
            # Defensive: 3-level isolation requires both
            return

        # Resolve the threshold via the D4 fallback chain (#406). None means
        # "disable seeding for this call" — bootstrap will catch up later.
        threshold = await resolve_knn_threshold(
            db=db,
            config=config,
            model_name=model_name,
            dimensions=len(vector),
        )
        if threshold is None:
            logger.debug(
                "knn_seed_skip_no_threshold",
                memory_id=str(memory.id),
                model=model_name,
                dimensions=len(vector),
            )
            # Bootstrap trigger (#406 C2 trigger 1): if this (model, dims)
            # has crossed D3, enqueue a deduped calibration job so
            # subsequent remember() calls find a populated row and exit
            # step 3. Dedup (Redis SET NX, 1h TTL) ensures at most one
            # enqueue per hour regardless of concurrent ingestion.
            try:
                from tasks.neural_calibration import maybe_trigger_bootstrap

                await maybe_trigger_bootstrap(
                    db=db,
                    model_name=model_name,
                    dimensions=len(vector),
                )
            except Exception as exc:
                # Bootstrap is best-effort. A failure here must not block
                # memory creation — log and move on; the next remember()
                # will retry the trigger.
                logger.warning(
                    "knn_seed_bootstrap_trigger_failed",
                    memory_id=str(memory.id),
                    model=model_name,
                    error=str(exc),
                )
            return

        workspace_id_str = str(memory.workspace_id)
        context_id_str = str(memory.context_id)
        memory_id_str = str(memory.id)

        # Idempotency guard: process_pending_embedding is called on both new
        # memory creation AND update_memory re-embed (memory_service.py:431).
        # Without this check, re-embeds would overwrite existing Hebbian edges
        # via ON CONFLICT DO UPDATE on the (user_id, src_id, dst_id) unique
        # constraint — downgrading `neural_association` weight=1.5 to
        # `semantic_similarity` weight=0.3. Skip seeding if the memory already
        # has any outgoing edges.
        edge_repo = NeuralEdgeRepository(db)
        existing_edges = await edge_repo.get_outgoing_edges(
            user_id=memory.user_id,
            src_id=memory.id,
            workspace_id=workspace_id_str,
            context_id=context_id_str,
            limit=1,
        )
        if existing_edges:
            logger.debug(
                "knn_seed_skip_already_seeded",
                memory_id=memory_id_str,
            )
            return

        # Search for k+1 candidates because the new memory itself will appear
        # as the top hit (cosine similarity with itself = 1.0).
        candidates = await search_memories_qdrant(
            user_id=memory.user_id,
            query_vector=vector,
            workspace_id=workspace_id_str,
            context_id=context_id_str,
            limit=config.knn_seed_k + 1,
            collection_name=collection_name,
        )

        # Filter: exclude self, apply resolved (calibrated) similarity threshold, cap at k
        seed_neighbors = [
            c for c in candidates if str(c["id"]) != memory_id_str and c["score"] >= threshold
        ][: config.knn_seed_k]

        if not seed_neighbors:
            logger.debug(
                "knn_seed_no_neighbors",
                memory_id=memory_id_str,
                candidates=len(candidates),
                threshold=threshold,
            )
            return

        edges_created = 0
        similarities: list[float] = []

        for neighbor in seed_neighbors:
            try:
                neighbor_id = UUID(str(neighbor["id"]))
                neighbor_score = float(neighbor["score"])
            except (ValueError, TypeError, KeyError):
                continue

            # SAVEPOINT per edge: if one insert raises a DB error (e.g.,
            # IntegrityError, connection issue), the inner transaction is
            # rolled back without poisoning the outer session, so subsequent
            # inserts and the final commit still succeed.
            try:
                async with db.begin_nested():
                    # Issue #741: edge_type='semantic_similarity' deprecated;
                    # discriminator moved to origin='semantic'.
                    edge = await edge_repo.create_edge_if_absent(
                        user_id=memory.user_id,
                        src_id=memory.id,
                        dst_id=neighbor_id,
                        edge_type=EDGE_TYPE_NEURAL_ASSOCIATION,
                        weight=config.knn_seed_weight,
                        confidence=neighbor_score,
                        workspace_id=workspace_id_str,
                        context_id=context_id_str,
                        origin=EDGE_ORIGIN_SEMANTIC,
                    )
                # edge is None when ON CONFLICT DO NOTHING fired — existing
                # edge was preserved (TOCTOU-safe against concurrent Hebbian
                # writes). Count only actually-created seed edges.
                if edge is not None:
                    edges_created += 1
                    similarities.append(neighbor_score)
            except Exception as edge_err:
                logger.warning(
                    "knn_seed_edge_failed",
                    memory_id=memory_id_str,
                    neighbor_id=str(neighbor.get("id", "?")),
                    error=str(edge_err),
                )

        if edges_created > 0:
            await db.commit()
            avg_similarity = sum(similarities) / len(similarities)
            logger.info(
                "knn_seed_edges_created",
                memory_id=memory_id_str,
                edges_created=edges_created,
                k=config.knn_seed_k,
                avg_similarity=round(avg_similarity, 4),
                weight=config.knn_seed_weight,
            )

    except Exception as e:
        # Best-effort: never let kNN seeding failures affect memory creation.
        # Rollback may itself fail if the session is already in a bad state;
        # log that failure at debug so it's observable but doesn't mask the
        # original error which is already being logged below.
        try:
            await db.rollback()
        except Exception as rollback_err:
            logger.debug(
                "knn_seeding_rollback_failed",
                memory_id=str(memory.id),
                error=str(rollback_err),
            )
        logger.warning(
            "knn_seeding_failed",
            memory_id=str(memory.id),
            error=str(e),
        )


async def _create_tag_cooccurrence_seed_edges(
    db: AsyncSession,
    memory: Memory,
) -> None:
    """Create tag-cooccurrence seed edges for a newly-persisted memory.

    Issue #223 (Tier 2 cold-start seeding): after a memory persists, find
    existing memories within the same (user, workspace, context) that share
    ``tag_cooccurrence_min_shared`` or more tags and create weak
    ``tag_cooccurrence`` edges. Complementary to k-NN semantic seeding —
    catches structural similarity that embedding similarity misses.

    Best-effort: any failure is logged and swallowed (must not affect memory
    creation). Mirrors the structure of ``_create_knn_seed_edges`` with three
    deliberate divergences:

    1. **SQL candidates, not Qdrant**: uses ``tags && ARRAY[?]`` (GIN-indexed
       overlap) with a ``cardinality(... INTERSECT ...)`` filter pushed down
       to SQL so the "shared >= N" threshold is enforced by Postgres, not
       materialized in Python. Requires the ``idx_memories_tags_gin`` index
       added by migration b05_223.
    2. **Edge-type-scoped idempotency guard**: skips only when
       ``tag_cooccurrence`` edges already exist for this memory (knn uses an
       any-type guard). If we used the any-type guard, the knn seed pass that
       ran just before would prevent us from ever seeding, even though the
       two seed types are independent.
    3. **One direction only**: mirrors knn (src=memory, dst=neighbor). Tag
       co-occurrence is logically symmetric, but the graph BFS treats edges
       as undirected, and the ``unique_edge`` constraint is ``(user, src,
       dst)`` — creating both directions doubles edge count without adding
       information. Kept asymmetric for consistency with the knn path.

    Args:
        db: AsyncSession bound to the caller's transaction context
        memory: The freshly-created Memory entity (must have non-empty tags)
    """
    from sqlalchemy import select, text

    from neural.config import NeuralMemoryConfig
    from repositories.neural_edge import NeuralEdgeRepository

    try:
        config = await NeuralMemoryConfig.from_db(db)

        if not config.tag_cooccurrence_enabled:
            return

        if not memory.workspace_id or not memory.context_id:
            # Defensive: 3-level isolation requires both
            return

        if not memory.tags:
            # No tags → no candidates by definition
            return

        # Pre-migration guard (Copilot loop 1): the documented deploy order
        # ships code before migration b05_223. In that window
        # ``hub_tag_cache`` does not exist yet, so the first SELECT against
        # it would raise UndefinedTable for every new memory until migration
        # runs. Use ``to_regclass`` (returns NULL on missing) to silently
        # no-op instead of relying on the outer try/except to swallow noisy
        # warnings. Cheap (no I/O — Postgres catalog lookup, plan cached).
        table_check = await db.execute(text("SELECT to_regclass('hub_tag_cache')"))
        if table_check.scalar() is None:
            logger.debug(
                "tag_cooccurrence_skip_pre_migration",
                memory_id=str(memory.id),
            )
            return

        workspace_id_str = str(memory.workspace_id)
        context_id_str = str(memory.context_id)
        memory_id_str = str(memory.id)

        # Source-scoped idempotency guard. Unlike knn (any-type guard), we
        # only skip when tag_cooccurrence edges already exist for this memory
        # — otherwise the knn seeding pass that just ran would prevent us from
        # ever writing tag_cooccurrence edges. Re-embed on update_memory()
        # would re-enter this function with the guard protecting us from
        # duplicate writes (create_edge_if_absent also protects via ON
        # CONFLICT, but skipping the SQL work is cheaper).
        #
        # Issue #741: the original guard filtered by ``edge_type='tag_cooccurrence'``.
        # After #741 those rows are merged into ``neural_association`` and the
        # tag-cooccurrence stamp moves to ``edge_metadata['source']``. The
        # filter is pushed into SQL (``metadata::jsonb ->> 'source'``) with
        # ``limit=1`` so high-degree memory nodes don't pull thousands of
        # co-activation edges into Python just to answer "already seeded?".
        edge_repo = NeuralEdgeRepository(db)
        existing_seed = await edge_repo.get_outgoing_edges(
            user_id=memory.user_id,
            src_id=memory.id,
            workspace_id=workspace_id_str,
            context_id=context_id_str,
            origin=EDGE_ORIGIN_HEBBIAN,
            metadata_source="tag_cooccurrence",
            limit=1,
        )
        if existing_seed:
            logger.debug(
                "tag_cooccurrence_skip_already_seeded",
                memory_id=memory_id_str,
            )
            return

        # Read hub-tag set for this (workspace, context). Missing row = never
        # computed = "no exclusion" (first-night fallback). Sleep Maintenance
        # populates the cache on its nightly run.
        from models.hub_tag import HubTagCache

        hub_row = await db.execute(
            select(HubTagCache.hub_tags).where(
                HubTagCache.workspace_id == memory.workspace_id,
                HubTagCache.context_id == memory.context_id,
            )
        )
        hub_tags_raw = hub_row.scalar_one_or_none()
        hub_tags_set: set[str] = set(hub_tags_raw) if hub_tags_raw else set()

        # Exclude hub tags from the incoming memory's tag set when forming the
        # candidate query. If every tag is hub-like, there's nothing left to
        # correlate on.
        query_tags = [t for t in memory.tags if t not in hub_tags_set]
        if len(query_tags) < config.tag_cooccurrence_min_shared:
            logger.debug(
                "tag_cooccurrence_skip_no_nonhub_tags",
                memory_id=memory_id_str,
                total_tags=len(memory.tags),
                nonhub_tags=len(query_tags),
                min_shared=config.tag_cooccurrence_min_shared,
            )
            return

        # Push the "share >= min_shared non-hub tags" predicate into SQL.
        # The `&&` operator uses the GIN index (idx_memories_tags_gin) as a
        # coarse filter; cardinality(... INTERSECT ...) enforces the exact
        # threshold. Ordering by INTERSECT cardinality descending lets us
        # pick the top-N "strongest" candidates without client-side sort.
        # Cast :query_tags to ``varchar[]`` (matching the column type
        # ``Column(ARRAY(String))`` → varchar[] in PG) instead of casting
        # the column. Casting the column would form expression
        # ``tags::text[] && X`` which no longer matches
        # ``idx_memories_tags_gin`` (indexed on bare ``tags``), turning the
        # seeding query into a seq scan on large contexts (Copilot loops
        # 1+3 history).
        #
        # Compute the expensive ``cardinality(... INTERSECT ...)`` exactly
        # once per row by wrapping the inner query and filtering on the
        # alias from the outer query. The previous shape repeated the
        # INTERSECT in both SELECT and WHERE; Postgres cannot always
        # CSE-collapse identical scalar subqueries, so on large candidate
        # sets the duplicate work was visible (Copilot loop 4 catch).
        sql = text(
            """
            SELECT id, tags, shared_count
            FROM (
                SELECT id, tags,
                       cardinality(ARRAY(
                           SELECT unnest(tags)
                           INTERSECT
                           SELECT unnest(CAST(:query_tags AS varchar[]))
                       )) AS shared_count
                FROM memories
                WHERE user_id = :user_id
                  AND workspace_id = CAST(:workspace_id AS uuid)
                  AND context_id = CAST(:context_id AS uuid)
                  AND deleted_at IS NULL
                  AND id != CAST(:self_id AS uuid)
                  AND tags && CAST(:query_tags AS varchar[])
            ) m
            WHERE shared_count >= :min_shared
            ORDER BY shared_count DESC, id
            LIMIT :max_matches
            """
        )
        result = await db.execute(
            sql,
            {
                "user_id": memory.user_id,
                "workspace_id": workspace_id_str,
                "context_id": context_id_str,
                "self_id": memory_id_str,
                "query_tags": query_tags,
                "min_shared": config.tag_cooccurrence_min_shared,
                "max_matches": config.tag_cooccurrence_max_per_remember,
            },
        )
        rows = result.all()

        if not rows:
            logger.debug(
                "tag_cooccurrence_no_candidates",
                memory_id=memory_id_str,
                nonhub_tag_count=len(query_tags),
            )
            return

        # Degree cap: if this memory already has N tag_cooccurrence edges
        # (from a prior incomplete run that created some but not all — should
        # only happen if the process crashed mid-seed; the idempotency guard
        # above would normally fire), truncate further writes.
        degree_cap = config.tag_cooccurrence_max_degree_per_node
        edges_created = 0

        for row in rows:
            if edges_created >= degree_cap:
                break

            try:
                neighbor_id = UUID(str(row.id))
                shared_count = int(row.shared_count)
            except (ValueError, TypeError, AttributeError):
                continue

            # Weight: 2 shared → 0.25, 3 → 0.35, 4+ → 0.40 (capped).
            weight = min(0.4, 0.15 + 0.10 * (shared_count - 1))
            # Confidence: 2 → 0.50, 3 → 0.75, 4+ → 1.00.
            confidence = min(1.0, shared_count / 4.0)

            # SAVEPOINT per edge so a single IntegrityError / validation
            # failure cannot poison the session for the remaining inserts.
            try:
                async with db.begin_nested():
                    # Issue #741: edge_type='tag_cooccurrence' deprecated. The
                    # row is still hebbian-origin (decays/prunes apply), but
                    # the original derivation is preserved via metadata so the
                    # idempotency guard above can detect prior seeding.
                    edge = await edge_repo.create_edge_if_absent(
                        user_id=memory.user_id,
                        src_id=memory.id,
                        dst_id=neighbor_id,
                        edge_type=EDGE_TYPE_NEURAL_ASSOCIATION,
                        weight=weight,
                        confidence=confidence,
                        workspace_id=workspace_id_str,
                        context_id=context_id_str,
                        edge_metadata={"source": "tag_cooccurrence"},
                    )
                # edge is None when ON CONFLICT DO NOTHING fired — e.g. a
                # semantic_similarity edge from knn seeding already exists
                # for this pair. unique_edge is (user, src, dst) so only one
                # edge per ordered pair regardless of edge_type.
                if edge is not None:
                    edges_created += 1
            except Exception as edge_err:
                logger.warning(
                    "tag_cooccurrence_edge_failed",
                    memory_id=memory_id_str,
                    neighbor_id=str(row.id),
                    error=str(edge_err),
                )

        if edges_created > 0:
            await db.commit()
            logger.info(
                "tag_cooccurrence_edges_created",
                memory_id=memory_id_str,
                edges_created=edges_created,
                candidates=len(rows),
                nonhub_tags=len(query_tags),
                hub_tags_excluded=len(memory.tags) - len(query_tags),
            )

    except Exception as e:
        # Best-effort: never let tag-cooccurrence seeding failures affect
        # memory creation. Rollback may itself fail if the session is in a
        # bad state; log at debug so the original error (logged below) isn't
        # masked.
        try:
            await db.rollback()
        except Exception as rollback_err:
            logger.debug(
                "tag_cooccurrence_rollback_failed",
                memory_id=str(memory.id),
                error=str(rollback_err),
            )
        logger.warning(
            "tag_cooccurrence_seeding_failed",
            memory_id=str(memory.id),
            error=str(e),
        )


def embedding_retry_eligible_clause(now: datetime):
    """SQLAlchemy clause: a ``failed`` embedding eligible for #979 auto-requeue.

    Eligible when it still has retry budget (``embedding_retry_count <
    MAX_EMBEDDING_RETRIES``) and its backoff has elapsed. The backoff is
    measured from ``updated_at`` (the failure ``UPDATE`` stamps it to the
    failure time EXPLICITLY — #1317 removed the column's ``onupdate``, so
    nothing stamps it implicitly anymore); a NULL ``updated_at`` is treated as
    immediately eligible so a row can never get permanently stuck ``failed``
    — the exact state #979 exists to prevent.

    Shared by the sweep prefilter (``tasks/embedding_tasks.py``) and the atomic
    claim in ``process_pending_embedding`` so the two gates cannot drift.
    """
    from datetime import timedelta

    from sqlalchemy import and_, or_

    from config.constants import EMBEDDING_RETRY_BACKOFF_SECONDS, MAX_EMBEDDING_RETRIES
    from models.memory import Memory

    retry_cutoff = now - timedelta(seconds=EMBEDDING_RETRY_BACKOFF_SECONDS)
    return and_(
        Memory.embedding_status == "failed",
        Memory.embedding_retry_count < MAX_EMBEDDING_RETRIES,
        or_(Memory.updated_at.is_(None), Memory.updated_at < retry_cutoff),
    )


async def process_pending_embedding(memory_id: UUID) -> None:
    """Process embedding generation + Qdrant upsert for a pending memory.

    Issue #76: Called via asyncio.create_task (fire-and-forget) or by the
    periodic sweep task for crash recovery. Reads memory from DB to get
    all needed fields — no parameter sprawl.
    """

    from datetime import timedelta

    from sqlalchemy import and_, case, or_, select, update

    from db.base import get_db
    from models.memory import Memory
    from services.context_routing import resolve_context_routing
    from services.embedding_service import EmbeddingService
    from utils.sparse_vector import build_document_sparse_vector
    from utils.text import normalize_for_search
    from utils.tokenizer import tokenize_and_reading, tokenize_for_search

    async for db in get_db():
        try:
            # Claim: atomically set pending/stale-processing → processing
            # Stale processing: updated_at older than 60s (crash recovery)
            from utils.datetime import utcnow as _utcnow

            now = _utcnow()
            stale_cutoff = now - timedelta(seconds=60)
            # #979: a `failed` row is also eligible for auto-retry once its
            # backoff has elapsed and it still has retry budget. The shared
            # clause keeps this gate byte-identical to the sweep prefilter. The
            # counter is incremented only when we claim a `failed` row (CASE
            # below), so the initial pending->processing claim spends no budget.
            result = await db.execute(
                update(Memory)
                .where(
                    Memory.id == memory_id,
                    Memory.deleted_at.is_(None),
                    or_(
                        Memory.embedding_status == "pending",
                        and_(
                            Memory.embedding_status == "processing",
                            Memory.updated_at < stale_cutoff,
                        ),
                        embedding_retry_eligible_clause(now),
                    ),
                )
                .values(
                    embedding_status="processing",
                    updated_at=now,
                    # Count only retries of a previously-failed row. The CASE
                    # reads the pre-UPDATE status, so pending/stale-processing
                    # claims leave the counter untouched.
                    embedding_retry_count=case(
                        (
                            Memory.embedding_status == "failed",
                            Memory.embedding_retry_count + 1,
                        ),
                        else_=Memory.embedding_retry_count,
                    ),
                )
                .returning(Memory.id)
            )
            claimed = result.scalar_one_or_none()
            if not claimed:
                return  # Already claimed, not eligible, exhausted, or soft-deleted

            await db.commit()

            # Load memory (verify not soft-deleted)
            mem_result = await db.execute(
                select(Memory).where(
                    Memory.id == memory_id,
                    Memory.deleted_at.is_(None),
                )
            )
            memory = mem_result.scalar_one_or_none()
            if not memory:
                return

            # Resolve per-context routing (#341: shared helper)
            collection, embed_svc = await resolve_context_routing(
                db, memory.context_id, default_service=EmbeddingService(db)
            )

            # Generate embedding
            vector = await embed_svc.embed(
                memory.summary,
                memory.user_id,
                context_id=memory.context_id,
                workspace_id=memory.workspace_id,
            )

            # Tokenize for BM25
            normalized_summary = normalize_for_search(memory.summary) or memory.summary
            normalized_ctx = normalize_for_search(memory.context_summary)
            content_text = (memory.content or "")[:2000]

            summary_tokens, summary_reading, _ = tokenize_and_reading(normalized_summary)
            ctx_tokens = tokenize_for_search(normalized_ctx or "")
            content_tokens = tokenize_for_search(content_text) if content_text else ""

            sparse_indices, sparse_values = build_document_sparse_vector(
                summary_tokens=summary_tokens,
                context_summary_tokens=ctx_tokens,
                content_tokens=content_tokens,
                summary_reading=summary_reading,
            )

            payload = {
                "user_id": memory.user_id,
                "summary": normalized_summary,
                "context_summary": normalized_ctx,
                "summary_tokens": summary_tokens,
                "context_summary_tokens": ctx_tokens,
                "content_tokens": content_tokens,
                "summary_reading": summary_reading,
                "type": memory.type,
                "importance": memory.importance,
                "tags": memory.tags or [],
                "scope": memory.scope,
                "client": memory.client or "unknown",
                "created_at": to_utc_iso(memory.created_at or utcnow()),
                "updated_at": to_utc_iso(memory.updated_at or memory.created_at or utcnow()),
            }
            if memory.context:
                payload["context"] = memory.context

            await add_memory_to_qdrant(
                user_id=memory.user_id,
                memory_id=memory_id,
                vector=vector,
                payload=payload,
                workspace_id=str(memory.workspace_id),
                context_id=str(memory.context_id),
                sparse_indices=sparse_indices,
                sparse_values=sparse_values,
                collection_name=collection,
            )

            # Mark success. Clear the prior embedding_error AND reset
            # embedding_retry_count (#979): the retry budget is per
            # failure-episode, not a lifetime tally — a later, unrelated
            # failure must get the full MAX_EMBEDDING_RETRIES budget again.
            await db.execute(
                update(Memory)
                .where(Memory.id == memory_id)
                .values(
                    embedding_status="success",
                    embedding_error=None,
                    embedding_retry_count=0,
                )
            )
            await db.commit()

            logger.info("embedding_completed", memory_id=str(memory_id))

            # ================================================================
            # Issue #221: k-NN cold-start seeding
            # ================================================================
            # After Qdrant upsert succeeds, search for k nearest neighbors
            # within the same workspace+context and create weak
            # `semantic_similarity` edges. This guarantees new memories are
            # not born as isolated nodes (cold-start UX failure).
            #
            # Best-effort: failures must NOT affect memory creation.
            # Runs in the background embedding task, so no impact on
            # remember() response latency.
            await _create_knn_seed_edges(
                db=db,
                memory=memory,
                vector=vector,
                collection_name=collection,
                model_name=embed_svc.model,
            )

            # ================================================================
            # Issue #223: tag co-occurrence cold-start seeding (Tier 2)
            # ================================================================
            # Independent of knn (different signal source: shared tags vs
            # vector similarity). Both can run; unique_edge constraint and
            # ON CONFLICT DO NOTHING ensure no double-write per pair. The
            # idempotency guard inside the function is edge-type-scoped so
            # this does NOT skip just because knn already wrote edges.
            await _create_tag_cooccurrence_seed_edges(
                db=db,
                memory=memory,
            )

        except Exception as e:
            await db.rollback()
            try:
                await db.execute(
                    update(Memory)
                    .where(Memory.id == memory_id)
                    .values(
                        embedding_status="failed",
                        embedding_error=str(e)[:500],
                        # #1317: the column's onupdate is gone — stamp the
                        # failure time explicitly; the #979 retry backoff
                        # (embedding_retry_eligible_clause) anchors on it.
                        updated_at=utcnow(),
                    )
                )
                await db.commit()
            except Exception:
                logger.warning("embedding_status_update_failed", memory_id=str(memory_id))

            logger.error("embedding_failed", memory_id=str(memory_id), error=str(e))
