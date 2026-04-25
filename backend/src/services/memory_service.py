"""Memory service for business logic.

Orchestrates memory operations across PostgreSQL, Qdrant, and Redis.
Implements remember(), recall(), forget(), reference() APIs.
Issue #82: Context-based multi-collection support.
"""

from __future__ import annotations

import asyncio
import functools
from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.retention import should_promote_to_persistent
from db.qdrant import (
    add_memory_to_qdrant,
    delete_memory_from_qdrant,
    update_memory_payload_in_qdrant,
)
from models.auth import Context
from models.memory import Memory
from models.schemas import (
    ExploreHint,
    ExploreRequest,
    ExploreResponse,
    ForgetRequest,
    ForgetResponse,
    LinkedMemoryRef,
    MemoryResponse,
    MemoryStatsResponse,
    PatchMemoryRequest,
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
from services.search_service import SearchService
from utils.datetime import utcnow
from utils.exceptions import MemoryGoneError, NotFoundException, QuotaExceededError
from utils.logger import get_logger

logger = get_logger(__name__)


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
        self, user_id: str, context_id: UUID | None
    ) -> tuple[Context | None, str | None, str | None]:
        """Extract workspace_id and context_id for 3-level isolation (performance optimization).

        Single Collection Migration: Helper to avoid duplicate context fetches.

        Args:
            user_id: User ID
            context_id: Context ID

        Returns:
            Tuple of (context_object, workspace_id_str, context_id_str)

        Raises:
            NotFoundException: If context not found
        """
        if not context_id:
            return None, None, None

        context = await self.context_service.get_context(user_id, context_id)
        return context, str(context.workspace_id), str(context_id)

    async def remember(
        self,
        request: RememberRequest,
        user_id: str,
        client: str = "unknown",
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,  # NEW: Workspace ID (Issue #146)
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

        # Single Collection Migration: Extract isolation params (optimized)
        context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id, current_context_id
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
            client=client,
            summary_embedding_id=memory_id,  # Same as memory_id
            embedding_status="pending",  # Issue #122: Track embedding state
            source_uri=request.source_uri,
            source_type=request.source_type,
        )

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

            return RememberResponse(memory_id=memory_id, scope="working")

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

        memory = await self.memory_repo.get(request.memory_id)
        if not memory:
            raise NotFoundException("Memory", str(request.memory_id))

        if memory.deleted_at is not None:
            raise NotFoundException("Memory", str(request.memory_id))

        # Permission check
        from services.permission_service import CallerId, MemoryAuthorId

        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(memory.user_id),
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
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
        if request.details is not None:
            memory.details = request.details
        if request.type is not None:
            memory.type = request.type
        if request.importance is not None:
            memory.importance = request.importance
        if request.tags is not None:
            memory.tags = request.tags
        if request.context is not None:
            memory.context = request.context

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
                payload_updates["updated_at"] = utcnow().isoformat() + "Z"
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

        Field-presence semantics: ``model_dump(exclude_unset=True)`` is used to
        distinguish "field omitted" from "field explicitly null". This matters
        for ``details`` — sending ``{"details": null}`` clears the existing JSON,
        while omitting ``details`` preserves it. ``tags`` follows the same
        contract (None/missing = preserve, [] = clear, [...] = replace).

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

        memory = await self.memory_repo.get(memory_id)
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
        if "content" in provided_fields:
            memory.content = request.content
        if "type" in provided_fields:
            memory.type = request.type
        if "importance" in provided_fields:
            memory.importance = request.importance
        if "tags" in provided_fields:
            memory.tags = request.tags
        if "details" in provided_fields:
            # Explicit null clears the column; non-null replaces it.
            memory.details = request.details

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
            if "importance" in provided_fields:
                payload_updates["importance"] = request.importance
            if "type" in provided_fields:
                payload_updates["type"] = request.type

            if payload_updates:
                payload_updates["updated_at"] = utcnow().isoformat() + "Z"
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
        )

        if not can_access:
            raise NotFoundException("Memory", str(memory_id))

        # Snapshot ``updated_at`` before bumping access stats. The Memory
        # ORM column declares ``onupdate=func.now()``; a subsequent UPDATE
        # (issued by ``update_access_stats``) makes SQLAlchemy expire the
        # in-memory attribute so the next access triggers a sync lazy-load
        # → ``MissingGreenlet`` outside the original IO context. Reading
        # the value here is also semantically right: an access bump is
        # not a meaningful edit, so the dialog's "Updated At" should
        # reflect the last real change, not "now".
        snapshot_updated_at = memory.updated_at or memory.created_at

        # Update access stats
        await self.memory_repo.update_access_stats(memory_id, client="api")
        await self.db.commit()

        logger.info("memory_referenced", memory_id=str(memory_id), user_id=user_id)

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

        out_edges = await edge_repo.get_outgoing_edges(
            user_id=None,
            src_id=memory_id,
            edge_types=["declared_link"],
            limit=cap + 1,
            workspace_id=str(workspace_id),
            context_id=str(context_id),
        )
        in_edges = await edge_repo.get_incoming_edges(
            user_id=None,
            dst_id=memory_id,
            edge_types=["declared_link"],
            limit=cap + 1,
            workspace_id=str(workspace_id),
            context_id=str(context_id),
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
        if not request.linked_memory_ids and not request.linked_source_uris:
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
                    await edge_repo.create_edge_if_absent(
                        user_id=user_id,
                        src_id=memory_id,
                        dst_id=target_id,
                        edge_type="declared_link",
                        weight=1.0,
                        confidence=1.0,
                        workspace_id=workspace_id,
                        context_id=context_id,
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
                    await edge_repo.create_edge_if_absent(
                        user_id=user_id,
                        src_id=memory_id,
                        dst_id=target_id,
                        edge_type="declared_link",
                        weight=1.0,
                        confidence=1.0,
                        workspace_id=workspace_id,
                        context_id=context_id,
                    )
                    created += 1

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

    async def recall(
        self,
        request: RecallRequest,
        user_id: str,
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,  # NEW: Workspace ID (Issue #146)
        context_ids: list[UUID] | None = None,  # Issue #81: cross-context recall
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

        Args:
            request: Recall request
            user_id: User ID
            current_context_id: Current context UUID (Issue #82)

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

        # Check if Neural Memory is enabled
        neural_enabled = os.getenv("ENABLE_NEURAL_MEMORY", "false").lower() == "true"

        # Issue #81: Cross-context recall — pass list of context IDs to search service
        search_context_id: str | list[str] = str(current_context_id)
        if context_ids:
            search_context_id = [str(cid) for cid in context_ids]

        # 1. Primary Retrieval: Hybrid Search (Semantic + BM25)
        # Fetch more candidates when neural is enabled for better hybrid merge
        # and to feed Hebbian learning with broader co-activation data
        candidates_k = request.k * 4 if neural_enabled else request.k
        search_results = await self.search_service.hybrid_search(
            query=request.query,
            user_id=user_id,
            workspace_id=str(current_workspace_id),
            context_id=search_context_id,
            k=candidates_k,
            use_rerank=request.use_rerank,
            filters=request.filters,
            search_mode=request.search_mode,
            include_vectors=neural_enabled,
        )

        # Get full memory data from PostgreSQL
        memory_ids = [r["id"] for r in search_results]

        if not memory_ids:
            return RecallResponse(
                results=[],
                explore_hints=[] if request.include_explore_hints else None,
            )

        # Fetch memories from PostgreSQL (exclude soft-deleted)
        pg_conditions = [
            Memory.id.in_(memory_ids),
            Memory.deleted_at.is_(None),
        ]
        # Issue #214: source_uri_prefix and source_type post-filters
        if request.filters:
            if prefix := request.filters.get("source_uri_prefix"):
                escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pg_conditions.append(Memory.source_uri.like(f"{escaped}%", escape="\\"))
            if stype := request.filters.get("source_type"):
                pg_conditions.append(Memory.source_type == stype)
        result = await self.db.execute(select(Memory).where(*pg_conditions))
        memories_list = list(result.scalars().all())
        memories = {str(m.id): m for m in memories_list}

        # === Issue #120: Neural Memory graph is for explore() only ===
        # recall uses pure hybrid search scores (no UnifiedScorer).
        # Hebbian learning still runs to build the graph for explore().

        responses = []
        for search_result in search_results[: request.k]:
            memory_id = search_result["id"]
            memory = memories.get(memory_id)

            if not memory:
                continue

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
                    client=memory.client,
                    tags=memory.tags or [],
                    context=memory.context,
                    score=search_result.get("hybrid_score", search_result["score"]),
                    source_uri=memory.source_uri,
                    source_type=memory.source_type,
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
                    workspace_id=str(current_workspace_id) if current_workspace_id else None,
                    context_id=str(current_context_id) if current_context_id else None,
                )

                hebbian_learner = HebbianLearner(graph_service, config)
                co_activation_tracker = CoActivationTracker(config)
                await co_activation_tracker.load_from_redis(user_id)

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
                co_activation_tracker.record_activation(
                    user_id, activated_nodes, embeddings=embedding_map
                )
                await co_activation_tracker.save_to_redis(user_id)

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

                # Hebbian updates
                await hebbian_learner.queue_update(user_id, activated_nodes, nodes_dict)
                edges_updated = await hebbian_learner.apply_updates(user_id)

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
                    current_workspace_id,
                    neural_enabled=neural_enabled,
                )
            except Exception as exc:
                logger.warning("explore_hints_generation_failed", error=str(exc))

        # Issue #104: Aggregate related tags from results
        related_tags = self._aggregate_related_tags(responses, limit=10)

        logger.info("recall_completed", user_id=user_id, results=len(responses))

        return RecallResponse(
            results=responses, related_tags=related_tags, explore_hints=explore_hints
        )

    async def forget(
        self,
        request: ForgetRequest,
        user_id: str,
        current_context_id: UUID | None = None,
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
        # Single Collection Migration: Extract isolation params (optimized)
        context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id, current_context_id
        )

        deleted_ids = []

        # Case 1: Delete by memory_id
        if request.memory_id:
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
            recall_request = RecallRequest(
                query=request.query,
                k=request.k,
                use_rerank=False,  # No reranking for delete
                filters=None,
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

            logger.info(
                "memories_soft_deleted_by_query",
                query=request.query,
                count=len(deleted_ids),
                user_id=user_id,
            )

        await self.db.commit()

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

        # 1. Get seed memory
        seed_memory = await self.memory_repo.get(request.memory_id)
        if not seed_memory:
            raise NotFoundException(f"Memory {request.memory_id} not found")

        # Issue #XXX: Team collaboration - verify access permission
        from services.permission_service import CallerId, MemoryAuthorId, PermissionService

        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(seed_memory.user_id),
            workspace_id=seed_memory.workspace_id,
            context_id=seed_memory.context_id,
        )

        if not can_access:
            raise NotFoundException(f"Memory {request.memory_id} not found")

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
                    edge = await edge_repo.create_edge_if_absent(
                        user_id=memory.user_id,
                        src_id=memory.id,
                        dst_id=neighbor_id,
                        edge_type="semantic_similarity",
                        weight=config.knn_seed_weight,
                        confidence=neighbor_score,
                        workspace_id=workspace_id_str,
                        context_id=context_id_str,
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

        # Edge-type-scoped idempotency guard. Unlike knn (any-type guard), we
        # only skip when tag_cooccurrence edges already exist for this memory
        # — otherwise the knn seeding pass that just ran would prevent us from
        # ever writing tag_cooccurrence edges. Re-embed on update_memory()
        # would re-enter this function with the guard protecting us from
        # duplicate writes (create_edge_if_absent also protects via ON
        # CONFLICT, but skipping the SQL work is cheaper).
        edge_repo = NeuralEdgeRepository(db)
        existing_edges = await edge_repo.get_outgoing_edges(
            user_id=memory.user_id,
            src_id=memory.id,
            edge_types=["tag_cooccurrence"],
            workspace_id=workspace_id_str,
            context_id=context_id_str,
            limit=1,
        )
        if existing_edges:
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
                    edge = await edge_repo.create_edge_if_absent(
                        user_id=memory.user_id,
                        src_id=memory.id,
                        dst_id=neighbor_id,
                        edge_type="tag_cooccurrence",
                        weight=weight,
                        confidence=confidence,
                        workspace_id=workspace_id_str,
                        context_id=context_id_str,
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


async def process_pending_embedding(memory_id: UUID) -> None:
    """Process embedding generation + Qdrant upsert for a pending memory.

    Issue #76: Called via asyncio.create_task (fire-and-forget) or by the
    periodic sweep task for crash recovery. Reads memory from DB to get
    all needed fields — no parameter sprawl.
    """

    from datetime import timedelta

    from sqlalchemy import and_, or_, select, update

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

            stale_cutoff = _utcnow() - timedelta(seconds=60)
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
                    ),
                )
                .values(embedding_status="processing", updated_at=_utcnow())
                .returning(Memory.id)
            )
            claimed = result.scalar_one_or_none()
            if not claimed:
                return  # Already claimed, not pending, or soft-deleted

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
                "created_at": (memory.created_at or utcnow()).isoformat() + "Z",
                "updated_at": (memory.updated_at or memory.created_at or utcnow()).isoformat()
                + "Z",
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

            # Mark success
            await db.execute(
                update(Memory).where(Memory.id == memory_id).values(embedding_status="success")
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
                    .values(embedding_status="failed", embedding_error=str(e)[:500])
                )
                await db.commit()
            except Exception:
                logger.warning("embedding_status_update_failed", memory_id=str(memory_id))

            logger.error("embedding_failed", memory_id=str(memory_id), error=str(e))
