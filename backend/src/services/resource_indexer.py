"""Resource Indexer Service for incremental indexing.

Issue #238: Incremental indexer for Public Contexts.

Responsibilities:
- Process pending resource events since last_offset
- Project JSONB payload into searchable representation
- Apply upsert/delete operations to Qdrant
- Track indexer state and metrics
"""

from __future__ import annotations

# Standard library imports (PEP8)
import json  # Issue #262: JSON serialization for Memory content
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5  # Issue #262: uuid5 for deterministic point_id

# Third-party imports (PEP8)
from qdrant_client.models import PointStruct, SparseVector
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

# Local application imports (PEP8)
from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    KAGURA_MEMORIES_VECTOR_NAME,
    get_collection_name,
    get_qdrant_client,
)
from models.auth import Context
from models.memory import Memory  # Issue #262: Memory model for resource data storage
from models.resource import IndexerState, Resource, ResourceEvent, ResourceSchema
from services.embedding_service import EmbeddingService
from utils.datetime import utcnow
from utils.exceptions import QdrantError
from utils.logger import get_logger
from utils.sparse_vector import build_resource_sparse_vector

logger = get_logger(__name__)


@dataclass
class IndexerMetrics:
    """Metrics from indexer run."""

    applied_upserts: int = 0
    applied_deletes: int = 0
    lag_seconds: float = 0.0
    errors: int = 0
    duration_ms: int = 0
    skipped: bool = False
    reason: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSONB storage."""
        return {
            "applied_upserts": self.applied_upserts,
            "applied_deletes": self.applied_deletes,
            "lag_seconds": self.lag_seconds,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "skipped": self.skipped,
            "reason": self.reason,
        }


# Mirrors ``IndexerSkippedReason`` (Literal) in ``api/routes/resource_indexer.py``.
# Kept here so the read service can degrade unknown DB values to None *before*
# they hit the pydantic boundary — see the wire-shape Literal for the source
# of truth and add new enum values to BOTH places (the OpenAPI snapshot test
# fails when they drift).
_KNOWN_SKIPPED_REASONS: frozenset[str] = frozenset(
    {
        "no_pending_events",
        "schema_not_found",
        "context_not_found",
        "empty_valid_points",
    }
)


async def get_indexer_status_for_context(
    db: AsyncSession,
    context: Context,
    *,
    recent_event_limit: int = 5,
) -> dict[str, Any]:
    """Read-only snapshot of indexer state + recent ingest events for a Context.

    Issue #326: backing data for ``GET /resources/{id}/indexer-status``.

    The reader intentionally lives outside ``ResourceIndexer`` because that
    class wires up Qdrant + embedding clients eagerly in ``__init__`` — read
    endpoints shouldn't pay that setup cost or fail when Qdrant is down.

    Sort contract for the dual-row transition (Phase 1 shadow column, see
    ``models.resource`` module docstring): rows whose ``resource_pk`` is
    populated take precedence over legacy ``resource_pk=NULL`` rows sharing
    the same context. Under the partial UNIQUE index
    ``uq_indexer_state_resource_context`` this always yields at most one
    authoritative row; ordering by ``id DESC`` is the tiebreaker for the
    worst case during an in-flight writer migration.

    Args:
        db: Async DB session.
        context: Pre-resolved Context (caller already checked workspace access).
        recent_event_limit: Max ingest events to return, newest first.

    Returns:
        Dict with:
            ``resource_id``: echo of ``context.resource_id``.
            ``state``: indexer state dict or ``None`` if indexer never ran
                for this context.
            ``recent_events``: list of event dicts (up to ``recent_event_limit``).
    """
    resource_id = context.resource_id
    assert resource_id is not None, "resolve_resource_by_slug returns only slugged contexts"

    # Cross-tenant safety (Copilot review #347): filter satellite tables by
    # the authoritative ``resources.id`` (UUID) rather than the slug. The
    # ``contexts.resource_id`` global UNIQUE only covers active rows — a
    # soft-deleted context releases the slug, so a string-only filter could
    # surface events from a previously-deleted resource (potentially a
    # different workspace) once the slug is reused. The Resource entity
    # row, by contrast, is workspace-scoped (``UniqueConstraint(workspace_id,
    # resource_id)``) and is what every satellite table's ``resource_pk``
    # FK actually points at.
    resource_pk = (
        await db.execute(
            select(Resource.id).where(
                Resource.workspace_id == context.workspace_id,
                Resource.resource_id == resource_id,
            )
        )
    ).scalar_one_or_none()

    # IndexerState lookup carries its own workspace boundary via
    # ``context_id`` (Context FK is workspace-scoped), so the dual-row
    # ordering contract from the docstring is restored here: prefer the
    # post-#323 row with ``resource_pk`` populated, fall back to a legacy
    # ``resource_pk IS NULL`` row scoped by slug + context_id when only the
    # legacy form exists. ``id DESC`` is the secondary tiebreaker.
    # Build the slug/pk predicate. The dual-row fallback is genuinely a
    # disjunction during Phase 1: when a Resource row is present, accept
    # either the ``resource_pk``-populated row OR the legacy ``resource_pk
    # IS NULL`` row that still carries the matching slug — both can exist
    # for the same logical state until the writer migration drains the
    # NULL bucket. Without the OR, a context that hasn't been re-written
    # since #323 would surface ``state: null`` despite legacy state being
    # present. When the Resource row is absent, fall back to slug-only
    # (workspace boundary still comes from context_id).
    if resource_pk is not None:
        state_predicate = or_(
            IndexerState.resource_pk == resource_pk,
            (IndexerState.resource_pk.is_(None)) & (IndexerState.resource_id == resource_id),
        )
    else:
        state_predicate = IndexerState.resource_id == resource_id

    state_row = (
        await db.execute(
            select(IndexerState)
            .where(
                IndexerState.context_id == context.id,
                state_predicate,
            )
            .order_by(
                # Prefer rows with resource_pk populated when both exist.
                IndexerState.resource_pk.is_(None).asc(),
                IndexerState.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    # ResourceEvent has no context_id column, so the slug alone is NOT a
    # workspace boundary — a soft-deleted context can release its slug for
    # reuse and surface old events from a prior workspace. Filter strictly
    # by the authoritative ``resource_pk`` here. If the Resource entity row
    # is genuinely missing (shouldn't happen for a slug that resolved
    # through ``resolve_resource_by_slug``), fail-safe to empty events
    # rather than fall back to slug filtering — "no recent activity" is
    # always preferable to a cross-tenant leak.
    events: Sequence[ResourceEvent]
    if resource_pk is None:
        events = []
    else:
        events = (
            (
                await db.execute(
                    select(ResourceEvent)
                    .where(ResourceEvent.resource_pk == resource_pk)
                    .order_by(ResourceEvent.id.desc())
                    .limit(recent_event_limit)
                )
            )
            .scalars()
            .all()
        )

    state_dict: dict[str, Any] | None
    if state_row is None:
        state_dict = None
    else:
        last_run_at = state_row.last_run_at
        lag_seconds: float | None
        if last_run_at is None:
            lag_seconds = None
        else:
            # `last_run_at` is stored as naive UTC; compare against timezone-aware now().
            if last_run_at.tzinfo is None:
                last_run_at_utc = last_run_at.replace(tzinfo=UTC)
            else:
                last_run_at_utc = last_run_at
            lag_seconds = (datetime.now(tz=UTC) - last_run_at_utc).total_seconds()

        metrics_raw = state_row.metrics or {}
        # Coerce ``metrics.reason`` to the known enum set the API surfaces.
        # The wire schema (`IndexerSkippedReason` Literal in
        # ``api/routes/resource_indexer.py``) is a strict pydantic enum;
        # if an older DB row carries a reason string the API doesn't know
        # about, blindly passing it through would 500 on response_model
        # validation. Degrade unknowns to None so the panel gracefully
        # shows "skipped" without an Alert text — matching the behavior
        # the route's `IndexerSkippedReason` docstring promises.
        skipped_reason: str | None
        if metrics_raw.get("skipped"):
            raw_reason = metrics_raw.get("reason")
            skipped_reason = raw_reason if raw_reason in _KNOWN_SKIPPED_REASONS else None
        else:
            # Don't surface a stale reason after a successful re-run.
            skipped_reason = None

        state_dict = {
            "job_status": state_row.job_status,
            "last_run_at": last_run_at.isoformat() + "Z"
            if last_run_at and last_run_at.tzinfo is None
            else (last_run_at.isoformat() if last_run_at else None),
            "next_run_at": (
                state_row.next_run_at.isoformat() + "Z"
                if state_row.next_run_at and state_row.next_run_at.tzinfo is None
                else (state_row.next_run_at.isoformat() if state_row.next_run_at else None)
            ),
            "active_version": state_row.active_version,
            "last_offset": state_row.last_offset,
            "lag_seconds": lag_seconds,
            "metrics": {
                "applied_upserts": int(metrics_raw.get("applied_upserts", 0) or 0),
                "applied_deletes": int(metrics_raw.get("applied_deletes", 0) or 0),
                "errors": int(metrics_raw.get("errors", 0) or 0),
                "skipped_reason": skipped_reason,
            },
        }

    event_dicts = []
    for ev in events:
        created_at = ev.created_at
        created_iso = (
            created_at.isoformat() + "Z"
            if created_at and created_at.tzinfo is None
            else (created_at.isoformat() if created_at else None)
        )
        event_dicts.append(
            {
                "id": ev.id,
                "op": ev.op,
                "doc_id": ev.doc_id,
                "version": ev.version,
                "created_at": created_iso,
            }
        )

    return {
        "resource_id": resource_id,
        "state": state_dict,
        "recent_events": event_dicts,
    }


class ResourceIndexer:
    """Incremental indexer for public contexts.

    Issue #238: Processes resource events and updates Qdrant collections.
    """

    def __init__(self, db: AsyncSession):
        """Initialize resource indexer.

        Args:
            db: Database session
        """
        self.db = db
        self.embedding_service = EmbeddingService(db)
        self.qdrant_client = get_qdrant_client()

    async def process_incremental(
        self,
        resource_id: str,
        context_id: UUID,
        batch_size: int = 100,
    ) -> IndexerMetrics:
        """Process pending events since last_offset.

        Args:
            resource_id: Resource identifier
            context_id: Context ID
            batch_size: Max events per run (default: 100)

        Returns:
            IndexerMetrics with execution stats
        """
        start_time = utcnow()
        metrics = IndexerMetrics()

        try:
            # 1. Get or create indexer state
            state = await self._get_or_create_state(resource_id, context_id)
            last_offset = state.last_offset

            # 2. Fetch pending events
            events = await self._fetch_events(resource_id, after_id=last_offset, limit=batch_size)

            if not events:
                metrics.skipped = True
                metrics.reason = "no_pending_events"
                logger.debug(
                    "indexer_no_pending_events",
                    resource_id=resource_id,
                    context_id=context_id,
                    last_offset=last_offset,
                )
                return metrics

            # 3. Load schema for JSONB projection
            schema = await self._get_latest_schema(resource_id)
            if not schema:
                logger.warning(
                    "indexer_schema_not_found",
                    resource_id=resource_id,
                )
                metrics.skipped = True
                metrics.reason = "schema_not_found"
                return metrics

            # 4. Get context for workspace_id/context_id
            context = await self._get_context(context_id)

            # Resolve Qdrant collection + per-context EmbeddingService from the
            # same ContextSearchConfig (#334 Layer B + #338 Layer C). Single
            # SELECT per batch — all events share the same context_id.
            collection_name, embedding_service = await self._resolve_routing_for_context(context_id)

            # 5. Process each event
            for event in events:
                try:
                    if event.op == "upsert":
                        await self._apply_upsert(
                            event, schema, context, collection_name, embedding_service
                        )
                        metrics.applied_upserts += 1
                    elif event.op == "delete":
                        await self._apply_delete(event, context, collection_name)
                        metrics.applied_deletes += 1

                    # Update offset after each successful event
                    state.last_offset = event.id

                except Exception as e:
                    logger.error(
                        "indexer_event_failed",
                        event_id=event.id,
                        doc_id=event.doc_id,
                        error=str(e),
                    )
                    metrics.errors += 1
                    # Continue processing (don't block on single event failure)

            # 6. Calculate lag
            if events:
                last_event_time = events[-1].created_at
                # Bugfix: Remove timezone for DB compatibility (TIMESTAMP WITHOUT TIME ZONE)
                current_time = utcnow()
                metrics.lag_seconds = (current_time - last_event_time).total_seconds()

            # 7. Update state
            # Bugfix: Remove timezone for DB compatibility (TIMESTAMP WITHOUT TIME ZONE)
            state.last_run_at = utcnow()
            state.metrics = metrics.to_dict()
            await self.db.commit()

            # 8. Calculate duration
            metrics.duration_ms = int((utcnow() - start_time).total_seconds() * 1000)

            logger.info(
                "indexer_run_completed",
                resource_id=resource_id,
                context_id=context_id,
                metrics=metrics.to_dict(),
            )

            return metrics

        except Exception as e:
            await self.db.rollback()
            logger.error(
                "indexer_run_failed",
                resource_id=resource_id,
                context_id=context_id,
                error=str(e),
            )
            metrics.errors += 1
            metrics.reason = str(e)
            return metrics

    # ========================================================================
    # JSONB Projection
    # ========================================================================

    def _project_payload(self, payload: dict, schema: ResourceSchema) -> dict:
        """Project JSONB payload into searchable representation.

        Args:
            payload: Raw JSONB payload from event
            schema: Resource schema with field definitions

        Returns:
            Dict with: {fulltext_content, facets, sortable, metadata}
        """
        fulltext_parts = []
        facets = {}
        sortable = {}
        metadata = {}

        field_defs = schema.field_definitions

        for field_def in field_defs:
            field_name = field_def.get("name")
            value = payload.get(field_name)

            if value is None:
                continue

            # Filter out non-public fields
            classification = field_def.get("classification", "public")
            if classification != "public":
                continue

            index_hint = field_def.get("index_hint", "")
            description = field_def.get("description", field_name)

            # Fulltext indexing
            if "fulltext" in index_hint or "vector" in index_hint:
                if isinstance(value, str):
                    fulltext_parts.append(f"{description}: {value}")
                elif isinstance(value, (int, float)):
                    fulltext_parts.append(f"{description}: {value}")

            # Facets (categorical fields)
            if "facet" in index_hint:
                facets[field_name] = value

            # Sortable (numeric/date fields)
            if "sort" in index_hint:
                sortable[field_name] = value

            # Metadata (all public fields)
            metadata[field_name] = value

        return {
            "fulltext_content": "\n".join(fulltext_parts),
            "facets": facets,
            "sortable": sortable,
            "metadata": metadata,
        }

    async def _apply_upsert(
        self,
        event: ResourceEvent,
        schema: ResourceSchema,
        context: Context,
        collection_name: str,
        embedding_service: EmbeddingService,
    ) -> None:
        """Apply upsert operation to Qdrant + PostgreSQL Memory.

        Args:
            event: Resource event
            schema: Resource schema
            context: Context object
            collection_name: Resolved Qdrant collection (per-context, see #334)
            embedding_service: Per-context EmbeddingService configured for the
                context's embedding_model/dimensions (see #338). Generated
                embedding dim must match collection_name's dim.
        """
        if not event.payload:
            logger.warning("upsert_event_has_no_payload", event_id=event.id)
            return

        # 1. Project payload
        projected = self._project_payload(event.payload, schema)

        # 2. Generate embedding for fulltext content
        # Use system user for public contexts (no personal API key needed)
        # TODO: Use workspace-scoped API key or system key
        content = projected["fulltext_content"]
        if not content:
            logger.warning(
                "upsert_event_no_fulltext_content", event_id=event.id, doc_id=event.doc_id
            )
            # Use doc_id as minimal content
            content = f"Document ID: {event.doc_id}"

        try:
            # Generate embedding using workspace-scoped or owner's API key
            # Bugfix: Context uses 'created_by' not 'owner_id'
            embedding = await embedding_service.embed(
                text=content,
                user_id=str(context.created_by),
                context_id=str(context.id),
                workspace_id=str(context.workspace_id) if context.workspace_id else None,
            )

        except Exception as e:
            logger.error("embedding_generation_failed", event_id=event.id, error=str(e))
            raise

        # 3. Prepare Qdrant point
        # Bugfix: Qdrant requires UUID or integer point_id, not string
        # Use uuid5 for deterministic UUID generation (idempotent)
        point_id_str = f"{event.resource_id}:{event.doc_id}:v{event.version}"
        point_id_uuid = uuid5(NAMESPACE_DNS, point_id_str)

        # Generate Memory ID for new memories (may be overwritten if memory exists)
        memory_id = uuid4()

        # Issue #335: Build sparse BM25 vector from the same fulltext_content
        # used for the dense embedding, so resource points participate in
        # hybrid search instead of scoring zero on BM25.
        sparse_indices, sparse_values = build_resource_sparse_vector(content)

        # kagura_memories collections are configured with named vectors
        # (dense + sparse bm25); anonymous vectors are rejected at upsert.
        point_vector: dict[str, Any] = {KAGURA_MEMORIES_VECTOR_NAME: embedding}
        if sparse_indices and sparse_values:
            point_vector[KAGURA_MEMORIES_BM25_VECTOR_NAME] = SparseVector(
                indices=sparse_indices, values=sparse_values
            )

        point = PointStruct(
            id=str(point_id_uuid),
            vector=point_vector,
            payload={
                "workspace_id": str(context.workspace_id),  # 3-level isolation
                "context_id": str(context.id),  # 3-level isolation
                "user_id": str(context.created_by),  # 3-level isolation
                "resource_id": event.resource_id,
                "doc_id": event.doc_id,
                "version": event.version,
                "content": content,
                "facets": projected["facets"],
                "sortable": projected["sortable"],
                "metadata": projected["metadata"],
                "updated_at": event.created_at.isoformat(),
                "memory_id": str(memory_id),
                "point_id_source": point_id_str,
            },
        )

        # 4. Upsert to Qdrant (per-context collection, see #334)
        try:
            await self.qdrant_client.upsert(
                collection_name=collection_name,
                points=[point],
                wait=True,
            )

            # Code quality: Use info for important business events
            logger.info(
                "qdrant_upsert_success",
                point_id=str(point_id_uuid),
                point_id_source=point_id_str,
                collection=collection_name,
            )

        except Exception as e:
            logger.error(
                "qdrant_upsert_failed",
                point_id=str(point_id_uuid),
                point_id_source=point_id_str,
                error=str(e),
            )
            raise QdrantError(f"Failed to upsert point: {e}") from e

        # ========================================================================
        # 5. Issue #262: Create or update Memory entry
        # ========================================================================
        # P1-4: Transaction consistency - Memory operations use same transaction
        # If Memory operation fails, PostgreSQL will rollback but Qdrant stays committed
        # This is acceptable because:
        # 1. Qdrant upsert is idempotent (same point_id)
        # 2. Next indexer run will retry Memory creation
        # 3. Orphaned Qdrant points are harmless (will be garbage collected later)

        try:
            # Check if Memory already exists (idempotency for re-indexing)
            # Performance: Use generated columns (Migration 061) instead of JSONB search
            # Bugfix: Context uses 'created_by' not 'owner_id'
            # Single Collection Migration: Use workspace_id/context_id instead of collection_name
            existing_memory_query = await self.db.execute(
                select(Memory).where(
                    Memory.user_id == str(context.created_by),
                    Memory.workspace_id == context.workspace_id,
                    Memory.context_id == context.id,
                    Memory.resource_id == event.resource_id,  # Generated column (fast!)
                    Memory.resource_doc_id == event.doc_id,  # Generated column (fast!)
                    Memory.resource_version == event.version,  # Generated column (fast!)
                )
            )
            existing_memory = existing_memory_query.scalar_one_or_none()

            if existing_memory:
                # Update existing memory (re-indexing case)
                # P0-1: Use existing memory_id, update summary_embedding_id to point_id
                memory_id = existing_memory.id

                # P1-5: Truncate summary to 500 chars (database limit)
                summary = f"[{event.resource_id}] {event.doc_id} v{event.version}"
                existing_memory.summary = summary[:500]
                # P2-10: Safe truncation with ellipsis for context_summary
                if len(content) > 2000:
                    existing_memory.context_summary = content[:1997] + "..."
                else:
                    existing_memory.context_summary = content
                existing_memory.content = json.dumps(event.payload, ensure_ascii=False)
                # P0-2: Fix - use 'is not None' to allow importance=0.0
                existing_memory.importance = (
                    event.importance if event.importance is not None else 0.6
                )
                existing_memory.details = {
                    "resource_id": event.resource_id,
                    "doc_id": event.doc_id,
                    "version": event.version,
                    # Bugfix: Keep timezone for ISO string
                    "indexed_at": utcnow().isoformat(),
                }
                # Bugfix: Remove timezone for DB compatibility
                existing_memory.updated_at = utcnow()
                existing_memory.embedding_status = "success"
                # Bugfix: Use UUID format for summary_embedding_id
                existing_memory.summary_embedding_id = point_id_uuid

                await self.db.flush()

                logger.info(
                    "resource_memory_updated",
                    memory_id=str(existing_memory.id),
                    resource_id=event.resource_id,
                    doc_id=event.doc_id,
                    version=event.version,
                )
            else:
                # Create new memory
                # P1-5: Truncate summary to 500 chars (database limit)
                summary = f"[{event.resource_id}] {event.doc_id} v{event.version}"
                # P2-10: Safe truncation with ellipsis for context_summary
                context_summary = content[:1997] + "..." if len(content) > 2000 else content
                memory = Memory(
                    id=memory_id,
                    user_id=str(context.created_by),
                    workspace_id=context.workspace_id,
                    context_id=context.id,
                    summary=summary[:500],
                    context_summary=context_summary,
                    content=json.dumps(event.payload, ensure_ascii=False),
                    details={
                        "resource_id": event.resource_id,
                        "doc_id": event.doc_id,
                        "version": event.version,
                        # Bugfix: Keep timezone for ISO string
                        "indexed_at": utcnow().isoformat(),
                    },
                    type="resource_data",
                    # P0-2: Fix - use 'is not None' to allow importance=0.0
                    importance=event.importance if event.importance is not None else 0.6,
                    scope="working",
                    source="resource_ingest",  # Issue #262: Track provenance
                    tags=[],
                    context={"context_id": str(context.id)},
                    client="resource_indexer",
                    # Bugfix: Use UUID format for summary_embedding_id
                    summary_embedding_id=point_id_uuid,
                    embedding_status="success",
                )

                self.db.add(memory)
                await self.db.flush()

                logger.info(
                    "resource_memory_created",
                    memory_id=str(memory_id),
                    resource_id=event.resource_id,
                    doc_id=event.doc_id,
                    version=event.version,
                    importance=memory.importance,
                )

            # ==================================================================
            # 6. Issue #355: Auto-cleanup old versions of same doc_id
            # ==================================================================
            try:
                old_memories_result = await self.db.execute(
                    select(Memory).where(
                        Memory.user_id == str(context.created_by),
                        Memory.workspace_id == context.workspace_id,
                        Memory.context_id == context.id,
                        Memory.resource_id == event.resource_id,
                        Memory.resource_doc_id == event.doc_id,
                        Memory.resource_version != event.version,
                    )
                )
                old_memories = old_memories_result.scalars().all()

                if old_memories:
                    for old_mem in old_memories:
                        old_point_str = (
                            f"{event.resource_id}:{event.doc_id}:v{old_mem.resource_version}"
                        )
                        old_point_uuid = uuid5(NAMESPACE_DNS, old_point_str)
                        try:
                            await self.qdrant_client.delete(
                                collection_name=collection_name,
                                points_selector=[str(old_point_uuid)],
                            )
                        except Exception:
                            pass  # Qdrant point may already be gone
                        await self.db.delete(old_mem)

                    await self.db.flush()
                    logger.info(
                        "old_versions_cleaned",
                        resource_id=event.resource_id,
                        doc_id=event.doc_id,
                        current_version=event.version,
                        cleaned_count=len(old_memories),
                    )
            except Exception as cleanup_err:
                # Non-blocking: cleanup failure should not block upsert
                logger.warning(
                    "old_version_cleanup_failed",
                    resource_id=event.resource_id,
                    doc_id=event.doc_id,
                    error=str(cleanup_err),
                )

        except Exception as e:
            # P1-4: Log Memory creation/update failure
            # PostgreSQL transaction will rollback, but Qdrant point remains
            # Next indexer run will retry
            logger.error(
                "resource_memory_operation_failed",
                resource_id=event.resource_id,
                doc_id=event.doc_id,
                version=event.version,
                error=str(e),
            )
            # Re-raise to trigger transaction rollback
            raise

    async def _apply_delete(
        self,
        event: ResourceEvent,
        context: Context,
        collection_name: str,
    ) -> None:
        """Apply delete operation to Qdrant + PostgreSQL Memory.

        Behavior:
            - version=NULL: Delete all versions of doc_id
            - version=N: Delete only version N

        Args:
            event: Resource event
            context: Context object
            collection_name: Resolved Qdrant collection (per-context, see #334)
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        try:
            if event.version is None:
                # ================================================================
                # Delete all versions (new behavior)
                # ================================================================

                # Delete from Qdrant with 3-level isolation
                await self.qdrant_client.delete(
                    collection_name=collection_name,
                    points_selector=Filter(
                        must=[
                            FieldCondition(
                                key="workspace_id",
                                match=MatchValue(value=str(context.workspace_id)),
                            ),
                            FieldCondition(
                                key="context_id", match=MatchValue(value=str(context.id))
                            ),
                            FieldCondition(key="doc_id", match=MatchValue(value=event.doc_id)),
                            FieldCondition(
                                key="resource_id", match=MatchValue(value=event.resource_id)
                            ),
                        ]
                    ),
                )

                # Code quality: Use info for important business events
                logger.info(
                    "qdrant_delete_all_versions",
                    doc_id=event.doc_id,
                    resource_id=event.resource_id,
                    collection=collection_name,
                )

                # Delete from Memory table with 3-level isolation
                # Performance: Use generated columns (Migration 061) instead of JSONB search
                result = await self.db.execute(
                    select(Memory).where(
                        Memory.workspace_id == context.workspace_id,
                        Memory.context_id == context.id,
                        Memory.resource_id == event.resource_id,  # Generated column (fast!)
                        Memory.resource_doc_id == event.doc_id,  # Generated column (fast!)
                    )
                )
                memories_to_delete = result.scalars().all()

                for memory in memories_to_delete:
                    await self.db.delete(memory)

                await self.db.flush()

                logger.info(
                    "resource_memories_deleted_all_versions",
                    doc_id=event.doc_id,
                    resource_id=event.resource_id,
                    deleted_count=len(memories_to_delete),
                )

            else:
                # ================================================================
                # Delete specific version (new behavior)
                # ================================================================

                # Delete from Qdrant
                # Bugfix: Use UUID format like in upsert
                point_id_str = f"{event.resource_id}:{event.doc_id}:v{event.version}"
                point_id_uuid = uuid5(NAMESPACE_DNS, point_id_str)

                await self.qdrant_client.delete(
                    collection_name=collection_name,
                    points_selector=[str(point_id_uuid)],
                )

                # Code quality: Use info for important business events
                logger.info(
                    "qdrant_delete_version",
                    point_id=str(point_id_uuid),
                    point_id_source=point_id_str,
                    collection=collection_name,
                )

                # Delete from Memory table with 3-level isolation
                # Performance: Use generated columns (Migration 061) instead of JSONB search
                result = await self.db.execute(
                    select(Memory).where(
                        Memory.workspace_id == context.workspace_id,
                        Memory.context_id == context.id,
                        Memory.resource_id == event.resource_id,  # Generated column (fast!)
                        Memory.resource_doc_id == event.doc_id,  # Generated column (fast!)
                        Memory.resource_version == event.version,  # Generated column (fast!)
                    )
                )
                memory = result.scalar_one_or_none()

                if memory:
                    await self.db.delete(memory)
                    await self.db.flush()

                    logger.info(
                        "resource_memory_deleted_version",
                        memory_id=str(memory.id),
                        doc_id=event.doc_id,
                        version=event.version,
                    )

        except Exception as e:
            logger.error("qdrant_delete_failed", doc_id=event.doc_id, error=str(e))
            raise QdrantError(f"Failed to delete document: {e}") from e

    # ========================================================================
    # Helper Methods
    # ========================================================================

    async def _get_or_create_state(self, resource_id: str, context_id: UUID) -> IndexerState:
        """Get or create indexer state.

        Args:
            resource_id: Resource ID
            context_id: Context ID

        Returns:
            IndexerState record
        """
        result = await self.db.execute(
            select(IndexerState).where(
                IndexerState.resource_id == resource_id,
                IndexerState.context_id == context_id,
            )
        )
        state = result.scalar_one_or_none()

        if not state:
            state = IndexerState(
                resource_id=resource_id,
                context_id=context_id,
                last_offset=0,
                job_status="idle",
            )
            self.db.add(state)
            await self.db.flush()

        return state

    async def _fetch_events(
        self,
        resource_id: str,
        after_id: int,
        limit: int,
    ) -> list[ResourceEvent]:
        """Fetch pending events since last_offset.

        Args:
            resource_id: Resource ID
            after_id: Last processed event ID
            limit: Max events to fetch

        Returns:
            List of ResourceEvent records (ordered by ID)
        """
        result = await self.db.execute(
            select(ResourceEvent)
            .where(
                ResourceEvent.resource_id == resource_id,
                ResourceEvent.id > after_id,
            )
            .order_by(ResourceEvent.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def _get_latest_schema(self, resource_id: str) -> ResourceSchema | None:
        """Get latest schema version for resource.

        Args:
            resource_id: Resource ID

        Returns:
            ResourceSchema or None if not found
        """
        result = await self.db.execute(
            select(ResourceSchema)
            .where(ResourceSchema.resource_id == resource_id)
            .order_by(ResourceSchema.schema_version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_context(self, context_id: UUID) -> Context:
        """Get context by ID.

        Args:
            context_id: Context ID

        Returns:
            Context record

        Raises:
            ValueError: If context not found
        """
        context = await self.db.get(Context, context_id)
        if not context:
            raise ValueError(f"Context {context_id} not found")
        return context

    async def _resolve_routing_for_context(self, context_id: UUID) -> tuple[str, EmbeddingService]:
        """Resolve (collection_name, embedding_service) for a context (#334 + #338).

        Mirrors memory_service._get_context_collection_name() +
        _get_embedding_service_for_config() and fuses them so the underlying
        ContextSearchConfig row is fetched exactly once per batch. Both values
        MUST come from the same config — otherwise the generated embedding's
        dim can diverge from the target collection's dim (two-layer bug
        pattern seen in #324/#334/#338). Do NOT split this back into two
        methods without preserving the single-source-of-truth invariant.

        Fallback (no ContextSearchConfig row): returns the legacy
        `kagura_memories` collection + the indexer's default EmbeddingService,
        matching memory_service._get_context_collection_name() exactly. The
        legacy collection name is hardcoded on this path (not derived from
        self.embedding_service) to keep memory_service and resource_indexer
        reading/writing the same collection for legacy contexts, even when an
        operator overrides settings.embedding_model — cross-service consistency
        on the fallback path matters more than intra-service consistency,
        because a split-brain (memory_service on legacy, indexer on overridden)
        would silently hide writes. Operators who override settings MUST
        create a ContextSearchConfig row per context to opt into the
        per-context routing path above.
        """
        from models.config import ContextSearchConfig

        result = await self.db.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
        )
        config = result.scalar_one_or_none()
        if config:
            collection_name = get_collection_name(
                config.embedding_model, config.embedding_dimensions
            )
            embedding_service = EmbeddingService(
                self.db,
                model=config.embedding_model,
                dimensions=config.embedding_dimensions,
            )
            return collection_name, embedding_service
        # No ContextSearchConfig row: return the legacy kagura_memories
        # collection exactly as memory_service does, so both services stay on
        # the same collection for legacy contexts. See the docstring above for
        # the cross-service consistency rationale.
        legacy_collection = get_collection_name("text-embedding-3-small", 512)
        if (
            self.embedding_service.model != "text-embedding-3-small"
            or self.embedding_service.dimensions != 512
        ):
            # Operator overrode settings.embedding_model but has no
            # ContextSearchConfig row for this context. Upserts will fail on
            # Qdrant dim mismatch — surface the misconfiguration early.
            logger.warning(
                "resource_indexer_fallback_dim_mismatch",
                context_id=str(context_id),
                legacy_collection=legacy_collection,
                legacy_dim=512,
                service_model=self.embedding_service.model,
                service_dim=self.embedding_service.dimensions,
                hint="create a ContextSearchConfig row for this context to use the per-context routing path",
            )
        return legacy_collection, self.embedding_service
