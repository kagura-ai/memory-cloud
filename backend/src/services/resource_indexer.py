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
from typing import Any, cast
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5  # Issue #262: uuid5 for deterministic point_id

# Third-party imports (PEP8)
from qdrant_client.models import PointStruct, SparseVector
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

# Local application imports (PEP8)
from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    KAGURA_MEMORIES_VECTOR_NAME,
    get_qdrant_client,
)
from models.auth import Context, Workspace
from models.memory import (  # Issue #262: Memory model for resource data storage
    SOURCE_TYPE_CONNECTOR,
    Memory,
)
from models.resource import IndexerState, Resource, ResourceEvent, ResourceSchema
from services.context_routing import resolve_context_routing
from services.embedding_service import EmbeddingService
from services.plan_suspension import ingest_suspended
from services.quota_service import QuotaService
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import QdrantError
from utils.logger import get_logger
from utils.sparse_vector import build_resource_sparse_vector

logger = get_logger(__name__)

# #896: Memory.source_uri is String(2048); a longer worker-supplied URI is
# dropped (not truncated) so a corrupted slack:// prefix can't break
# find_by_channel and a flush DataError can't poison the event offset.
_SOURCE_URI_MAX_LEN = 2048


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
        "resource_entity_missing",
        "memories_per_day_exceeded",  # #1549: batch deferred to the UTC reset
        "plan_suspended",  # #1939: the plan no longer carries this ingest
        "memory_limit_exceeded",  # #1939: workspace memory limit reached
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
            "last_run_at": to_utc_iso(last_run_at),
            "next_run_at": to_utc_iso(state_row.next_run_at),
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
        created_iso = to_utc_iso(ev.created_at)
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


def resource_point_source(resource_id: str, doc_id: str, version: int | None) -> str:
    """The natural key a resource point's id is derived from."""
    return f"{resource_id}:{doc_id}:v{version}"


def resource_point_id(resource_id: str, doc_id: str, version: int | None) -> UUID:
    """The vector store point id of one version of a resource document.

    Deterministic (uuid5), so indexing the same version twice writes the same
    point. The Memory row keeps it in ``summary_embedding_id`` (#1829).
    """
    return uuid5(NAMESPACE_DNS, resource_point_source(resource_id, doc_id, version))


def owns_resource_point(memory: Memory) -> bool:
    """Whether ``memory`` is a row the indexer wrote, owning an indexer point.

    ``details.resource_id`` alone does not say so: ``remember(external_id=...)``
    sets it on memories the API wrote, whose point is their own id. An indexer
    row carries the full natural key and names the point derived from it.
    """
    if (
        memory.resource_id is None
        or memory.resource_doc_id is None
        or memory.resource_version is None
    ):
        return False
    return memory.summary_embedding_id == resource_point_id(
        memory.resource_id, memory.resource_doc_id, memory.resource_version
    )


def resource_document_text(projected: dict[str, Any], doc_id: str) -> str:
    """The text a resource document is embedded and BM25-indexed by.

    The projected fulltext, or a minimal stand-in naming the document when the
    schema projects none.
    """
    return projected["fulltext_content"] or f"Document ID: {doc_id}"


def build_resource_point(
    *,
    context: Context,
    resource_id: str,
    doc_id: str,
    version: int | None,
    content: str,
    projected: dict[str, Any],
    embedding: list[float],
    updated_at: datetime,
    memory_id: UUID,
) -> PointStruct:
    """Build the point of one resource document version.

    The one place the point's id, vectors and payload are put together: the
    indexer (``_apply_upsert``) and the rebuild of a row whose point is gone
    (``rebuild_point``, #1870) both use it, so a rebuilt point has the shape of
    an indexed one.

    Args:
        context: The context the document is indexed into.
        resource_id / doc_id / version: The document's natural key.
        content: ``resource_document_text`` of ``projected``; what
            ``embedding`` was computed from.
        projected: ``ResourceIndexer._project_payload`` of the document.
        embedding: Dense vector of ``content``.
        updated_at: When the document version was ingested.
        memory_id: The Memory row that owns the point.
    """
    # Issue #335: sparse BM25 vector from the same text as the dense
    # embedding, so resource points participate in hybrid search instead of
    # scoring zero on BM25.
    sparse_indices, sparse_values = build_resource_sparse_vector(content)

    # kagura_memories collections are configured with named vectors
    # (dense + sparse bm25); anonymous vectors are rejected at upsert.
    point_vector: dict[str, Any] = {KAGURA_MEMORIES_VECTOR_NAME: embedding}
    if sparse_indices and sparse_values:
        point_vector[KAGURA_MEMORIES_BM25_VECTOR_NAME] = SparseVector(
            indices=sparse_indices, values=sparse_values
        )

    # Qdrant requires a UUID or integer point id, not a string.
    return PointStruct(
        id=str(resource_point_id(resource_id, doc_id, version)),
        vector=point_vector,
        payload={
            "workspace_id": str(context.workspace_id),  # 3-level isolation
            "context_id": str(context.id),  # 3-level isolation
            "user_id": str(context.created_by),  # 3-level isolation
            "resource_id": resource_id,
            "doc_id": doc_id,
            "version": version,
            "content": content,
            "facets": projected["facets"],
            "sortable": projected["sortable"],
            "metadata": projected["metadata"],
            # The event time: the updated_after / updated_before recall
            # filters read it. Not when the point was written.
            "updated_at": to_utc_iso(updated_at),
            # When the point was written (#1869): the orphan sweep's grace
            # period is measured from here, see the upsert in _apply_upsert.
            "indexed_at": to_utc_iso(utcnow()),
            "memory_id": str(memory_id),
            "point_id_source": resource_point_source(resource_id, doc_id, version),
        },
    )


class ResourceRebuildError(Exception):
    """A resource-ingested row's point cannot be rebuilt from the row (#1870)."""


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
            # 1. Resolve context first so workspace_id is available for
            # ``resource_pk`` lookup (Issue #390 Phase 2). All satellite
            # queries below filter by resource_pk to avoid the CWE-639
            # slug-reuse leak when a soft-deleted context releases its slug.
            context = await self._get_context(context_id)

            from services.resource_lookup import resolve_resource_pk

            resource_pk = await resolve_resource_pk(self.db, context.workspace_id, resource_id)
            if resource_pk is None:
                # Pre-a97 orphan or writer gap: refuse to index rather than
                # surface potentially cross-tenant rows via slug fallback.
                metrics.skipped = True
                metrics.reason = "resource_entity_missing"
                logger.warning(
                    "indexer_resource_entity_missing",
                    resource_id=resource_id,
                    context_id=context_id,
                )
                return metrics

            # 2. Get or create indexer state
            state = await self._get_or_create_state(resource_id, context_id, resource_pk)
            last_offset = state.last_offset

            # 3. Fetch pending events
            events = await self._fetch_events(resource_pk, after_id=last_offset, limit=batch_size)

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

            # #1939: a workspace back on Free keeps its resources, but the
            # paid-only ingest is suspended. Nothing is applied and the offset
            # stays put, so the events survive until it re-subscribes; the job
            # re-queues the row (tasks/resource_indexer_job.py).
            if await self._plan_suspended(context, resource_pk):
                metrics.skipped = True
                metrics.reason = "plan_suspended"
                logger.info(
                    "indexer_plan_suspended",
                    resource_id=resource_id,
                    context_id=context_id,
                    workspace_id=str(context.workspace_id),
                )
                return metrics

            # 4. Load schema for JSONB projection
            schema = await self._get_latest_schema(resource_pk)
            if not schema:
                logger.warning(
                    "indexer_schema_not_found",
                    resource_id=resource_id,
                )
                metrics.skipped = True
                metrics.reason = "schema_not_found"
                return metrics

            # Resolve Qdrant collection + per-context EmbeddingService from the
            # same ContextSearchConfig (#334 Layer B + #338 Layer C). Single
            # SELECT per batch — all events share the same context_id.
            collection_name, embedding_service = await resolve_context_routing(
                self.db, context_id, default_service=self.embedding_service
            )

            # Issue #1549: charge the daily memory-creation quota ONCE for the
            # whole batch, up front, for the doc_ids that do not exist yet. A
            # re-index of a known doc (the update branch of _apply_upsert, or a
            # new version that replaces the old row) creates no net row and
            # must not burn the workspace's shared daily budget — one SELECT
            # splits the batch into new vs known. All-or-nothing: a batch that
            # does not fit is left untouched (offset unchanged) and the job
            # re-queues the row for the next UTC midnight, when the counter
            # resets (tasks/resource_indexer_job.py). Deletes create nothing.
            # A batch larger than the whole daily limit never fits, so an
            # operator lowering ``PLAN_*_MEMORIES_PER_DAY`` below ``batch_size``
            # must lower the batch size too.
            upsert_doc_ids = {event.doc_id for event in events if event.op == "upsert"}
            if upsert_doc_ids:
                known_doc_ids = await self._existing_resource_doc_ids(
                    resource_id, context, upsert_doc_ids
                )
                new_count = len(upsert_doc_ids - known_doc_ids)
            else:
                new_count = 0
            if new_count:
                # #1939: resource ingest creates memories too, so it honours the
                # workspace memory limit like remember does. Read BEFORE the
                # daily charge so a refused batch burns no daily budget, and
                # without the workspace row lock: the batch then embeds for a
                # while, and holding the lock that long would stall every
                # remember in the workspace. Advisory, like the daily cap.
                within_limit, limit_error = await QuotaService(self.db).check_memory_quota(
                    context.workspace_id, lock_workspace=False
                )
                if not within_limit:
                    metrics.skipped = True
                    metrics.reason = "memory_limit_exceeded"
                    logger.warning(
                        "indexer_memory_limit_exceeded",
                        resource_id=resource_id,
                        context_id=context_id,
                        workspace_id=str(context.workspace_id),
                        new_docs=new_count,
                        error=limit_error,
                    )
                    return metrics
                allowed, quota_error = await QuotaService(self.db).check_memories_per_day(
                    context.workspace_id, count=new_count
                )
                if not allowed:
                    metrics.skipped = True
                    metrics.reason = "memories_per_day_exceeded"
                    logger.warning(
                        "indexer_memories_per_day_exceeded",
                        resource_id=resource_id,
                        context_id=context_id,
                        workspace_id=str(context.workspace_id),
                        new_docs=new_count,
                        error=quota_error,
                    )
                    return metrics

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

    async def _plan_suspended(self, context: Context, resource_pk: UUID) -> bool:
        """Whether ingest into ``resource_pk`` is suspended on the plan (#1939).

        Args:
            context: The resource's context (carries ``workspace_id``).
            resource_pk: ``resources.id`` being indexed.

        Returns:
            True when the workspace's plan no longer carries the connector /
            resource ingest this resource needs.
        """
        workspace = await self.db.get(Workspace, context.workspace_id)
        if workspace is None:
            return False
        return await ingest_suspended(self.db, workspace, resource_pk)

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

    # #896: details keys that drive persisted Computed columns the resource path
    # must NOT let a worker populate — a worker-supplied external_blob would make
    # external_blob_backend/ref non-NULL (triggering R2 blob/retention logic on a
    # non-blob memory), and trigger would make a resource_data memory surface as a
    # Time Memory. resource_id/doc_id are also computed-backed but the indexer
    # overwrites those itself, so they need no stripping.
    # #896 rule: details keys that drive generated columns / platform lanes
    # must never be worker-supplied. 'location' (#1331) drives
    # location_lat/lon — connector-ingested coordinates are stripped.
    # 'tool_trigger' marks a tool guardrail that client hooks inject into the
    # model's context: connector-ingested content must never become one, even
    # if the trusted-tier read gate were ever bypassed.
    _LINEAGE_RESERVED_KEYS = frozenset({"external_blob", "trigger", "location", "tool_trigger"})

    def _extract_worker_lineage(self, event: ResourceEvent) -> tuple[dict[str, Any], str | None]:
        """Extract ai-worker lineage (#896) from event_metadata.

        Returns ``(memory_details, source_uri)``. Both default to empty/None for
        non-worker events. Malformed values are dropped with a warning rather
        than silently masked, so worker schema bugs surface in logs.
        """
        meta = event.event_metadata or {}

        raw_details = meta.get("memory_details")
        if raw_details is None:
            opaque_details: dict[str, Any] = {}
        elif isinstance(raw_details, dict):
            opaque_details = dict(raw_details)
            # Strip computed-column source keys (defense against pollution).
            reserved = self._LINEAGE_RESERVED_KEYS & opaque_details.keys()
            if reserved:
                logger.warning(
                    "ingest_event_lineage_reserved_keys_stripped",
                    event_id=event.id,
                    keys=sorted(reserved),
                )
                for key in reserved:
                    opaque_details.pop(key, None)
        else:
            logger.warning(
                "ingest_event_memory_details_not_dict",
                event_id=event.id,
                got_type=type(raw_details).__name__,
            )
            opaque_details = {}

        raw_uri = meta.get("source_uri")
        source_uri: str | None
        if raw_uri is None:
            source_uri = None
        elif not isinstance(raw_uri, str):
            logger.warning(
                "ingest_event_source_uri_not_str",
                event_id=event.id,
                got_type=type(raw_uri).__name__,
            )
            source_uri = None
        elif len(raw_uri) > _SOURCE_URI_MAX_LEN:
            # Drop (don't truncate — a corrupted slack:// prefix would break
            # find_by_channel) and don't poison the event with a flush DataError.
            logger.warning(
                "ingest_event_source_uri_too_long",
                event_id=event.id,
                length=len(raw_uri),
                max_length=_SOURCE_URI_MAX_LEN,
            )
            source_uri = None
        else:
            source_uri = raw_uri

        return opaque_details, source_uri

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

        # #896: opaque lineage passthrough. The ai-worker (resource-ingest write
        # path, worker #91 Option A) supplies the recall-contract lineage in
        # event_metadata so an ingest_event-written Memory is byte-equivalent in
        # details + source_uri to a remember()-written one:
        #   event_metadata["memory_details"] -> merged into Memory.details
        #   event_metadata["source_uri"]     -> Memory.source_uri
        # Both are optional; absent (non-worker resources) → legacy behavior.
        # event_metadata lives on the event, NOT in payload, so payload/content
        # stays pure document data (mirrors the ResourceEventRequest precedent).
        opaque_details, lineage_source_uri = self._extract_worker_lineage(event)

        # 1. Project payload
        projected = self._project_payload(event.payload, schema)

        # 2. Generate embedding for fulltext content
        # Use system user for public contexts (no personal API key needed)
        # TODO: Use workspace-scoped API key or system key
        if not projected["fulltext_content"]:
            logger.warning(
                "upsert_event_no_fulltext_content", event_id=event.id, doc_id=event.doc_id
            )
        # The fulltext, or the doc_id as minimal content
        content = resource_document_text(projected, event.doc_id)

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
        point_id_str = resource_point_source(event.resource_id, event.doc_id, event.version)
        point_id_uuid = resource_point_id(event.resource_id, event.doc_id, event.version)

        # Resolve the Memory row BEFORE the point is built, so the payload's
        # ``memory_id`` is the row's id on a re-index too (#1829). It used to be
        # a fresh uuid4() overwritten only after the upsert, so a re-indexed
        # point named an id no row carried and nothing could judge the point
        # by it (#1808).
        #
        # Idempotency lookup for re-indexing. Performance: generated columns
        # (Migration 061) instead of a JSONB search. Context uses 'created_by',
        # not 'owner_id'. Single Collection Migration: workspace_id/context_id
        # instead of collection_name. #1549 review: tombstones are excluded so
        # a doc the user forgot is RE-CREATED on re-sync (a fresh, visible row
        # — charged once by the batch gate, which ignores tombstones the same
        # way) instead of being patched in place under its deleted_at and
        # staying invisible. Tombstones are terminal: the #1521 sweep
        # hard-deletes them later.
        try:
            existing_memory_query = await self.db.execute(
                select(Memory).where(
                    Memory.user_id == str(context.created_by),
                    Memory.workspace_id == context.workspace_id,
                    Memory.context_id == context.id,
                    Memory.resource_id == event.resource_id,  # Generated column (fast!)
                    Memory.resource_doc_id == event.doc_id,  # Generated column (fast!)
                    Memory.resource_version == event.version,  # Generated column (fast!)
                    Memory.deleted_at.is_(None),
                )
            )
        except Exception as e:
            logger.error(
                "resource_memory_lookup_failed",
                resource_id=event.resource_id,
                doc_id=event.doc_id,
                version=event.version,
                error=str(e),
            )
            raise
        existing_memory = existing_memory_query.scalar_one_or_none()
        memory_id = existing_memory.id if existing_memory else uuid4()

        point = build_resource_point(
            context=context,
            resource_id=event.resource_id,
            doc_id=event.doc_id,
            version=event.version,
            content=content,
            projected=projected,
            embedding=embedding,
            updated_at=event.created_at,
            memory_id=memory_id,
        )

        # 4. Upsert to Qdrant (per-context collection, see #334). The point is
        # written before the row that owns it commits (once per batch); the
        # orphan sweep, which judges resource points by their rows (#1829),
        # never takes a point for an orphan until its payload ``indexed_at`` —
        # the write time, not ``updated_at``, which is the event time and can
        # be far in the past on a backlog (#1869) — is older than the grace
        # period, so this window is safe without the #1798 writer lock — which
        # would pin the sweep out for the whole batch.
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
        # 3. A point left without a row is swept once its ``indexed_at`` is
        #    older than the sweep's grace period: the sweep judges resource
        #    points by their row (#1829).

        try:
            if existing_memory:
                # Update existing memory (re-indexing case)
                # P1-5: Truncate summary to 500 chars (database limit)
                summary = f"[{event.resource_id}] {event.doc_id} v{event.version}"
                existing_memory.summary = summary[:500]
                # Issue #887: keep connector provenance on re-index — a row
                # backfilled to 'manual' (pre-#887) or any stale value is
                # restamped 'connector' so provenance reflects the ingest path.
                existing_memory.source_type = SOURCE_TYPE_CONNECTOR
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
                # #896: worker lineage keys first, indexer lifecycle keys layered
                # on top. The two sets are orthogonal (worker: connector_id /
                # platform / team_id / channel_id / thread_ts / source_message_ids;
                # indexer: resource_id / doc_id / version / indexed_at), so neither
                # clobbers the other — the spread order just makes the indexer's
                # lifecycle identity authoritative.
                existing_memory.details = {
                    **opaque_details,
                    "resource_id": event.resource_id,
                    "doc_id": event.doc_id,
                    "version": event.version,
                    # Bugfix: Keep timezone for ISO string
                    "indexed_at": to_utc_iso(utcnow()),
                }
                # Always assign (mirrors the create branch) — re-indexing a doc
                # whose event carries no lineage must clear a stale source_uri,
                # not leave it matching spurious source_uri_prefix queries.
                existing_memory.source_uri = lineage_source_uri
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
                    # #896: worker lineage keys first, indexer lifecycle keys on
                    # top (orthogonal sets — see the update branch comment).
                    details={
                        **opaque_details,
                        "resource_id": event.resource_id,
                        "doc_id": event.doc_id,
                        "version": event.version,
                        # Bugfix: Keep timezone for ISO string
                        "indexed_at": to_utc_iso(utcnow()),
                    },
                    # #896: source_uri from worker lineage (slack://…) so
                    # source_uri_prefix queries (find_by_channel) work; NULL for
                    # non-worker resources (legacy behavior).
                    source_uri=lineage_source_uri,
                    type="resource_data",
                    # P0-2: Fix - use 'is not None' to allow importance=0.0
                    importance=event.importance if event.importance is not None else 0.6,
                    scope="working",
                    source="resource_ingest",  # Issue #262: Track provenance
                    # Issue #887: server-stamped provenance — connector/external
                    # ingestion is 'connector', never client-set. (Trust is
                    # authoritative at the context level; this is provenance.)
                    source_type=SOURCE_TYPE_CONNECTOR,
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

    async def rebuild_point(
        self,
        memory: Memory,
        *,
        collection_name: str,
        embedding_service: EmbeddingService,
    ) -> UUID:
        """Write the point of a resource-ingested row again, from the row (#1870).

        For a row whose point is gone while its events are already consumed —
        a restored context's (#1804) — so the indexer will not come back to
        it. The row holds the document as it was ingested (``content`` is the
        event payload), so the point is rebuilt the way ``_apply_upsert`` built
        it: the payload projected through the resource's latest schema, the
        projected text embedded, and ``build_resource_point`` under the id the
        row already names (``Memory.point_id``). The row itself is not touched.

        The row is the source, as it is for recall's hydration: a row whose
        content was edited into another JSON object is rebuilt from what it
        holds now, not checked against the event it came from (events carry
        no context and may be pruned).

        The generic embedding path must not take such a row: it would embed
        the row's summary, which is only the label ``[resource] doc vN``, and
        store it under the row id, leaving ``Memory.point_id`` naming nothing.

        Args:
            memory: A row for which ``owns_resource_point`` holds.
            collection_name: The context's collection (``resolve_context_routing``).
            embedding_service: The context's EmbeddingService.

        Returns:
            The id of the point written, equal to ``memory.point_id``.

        Raises:
            ResourceRebuildError: The row does not own a resource point, its
                resource or schema is gone, or its content is not a JSON
                object. Ingesting the document again (a newer
                version; the same one is refused as a duplicate) rebuilds it.
            QdrantError: The upsert failed.
        """
        if not owns_resource_point(memory):
            raise ResourceRebuildError(f"Memory {memory.id} does not own a resource point")
        resource_id = cast(str, memory.resource_id)
        doc_id = cast(str, memory.resource_doc_id)
        version = cast(int, memory.resource_version)

        from services.resource_lookup import resolve_resource_pk

        context = await self._get_context(memory.context_id)
        resource_pk = await resolve_resource_pk(self.db, context.workspace_id, resource_id)
        schema = await self._get_latest_schema(resource_pk) if resource_pk else None
        if resource_pk is None or schema is None:
            raise ResourceRebuildError(
                f"Resource '{resource_id}' has no schema to project document "
                f"'{doc_id}' with; ingest the document again as a newer version to rebuild its vector"
            )
        try:
            document = json.loads(memory.content or "")
        except ValueError:
            document = None
        if not isinstance(document, dict):
            raise ResourceRebuildError(
                f"Memory {memory.id} does not hold a JSON document for "
                f"'{doc_id}' v{version}; ingest the document again as a newer version to rebuild its vector"
            )

        projected = self._project_payload(document, schema)
        content = resource_document_text(projected, doc_id)
        embedding = await embedding_service.embed(
            text=content,
            user_id=str(context.created_by),
            context_id=str(context.id),
            workspace_id=str(context.workspace_id) if context.workspace_id else None,
        )

        # The indexer stamps the point with the event's time. The event is
        # still there unless it was pruned; the row's own time (set when the
        # event was indexed) stands in for it then.
        ingested_at = (
            await self.db.execute(
                select(ResourceEvent.created_at)
                .where(
                    ResourceEvent.resource_pk == resource_pk,
                    ResourceEvent.doc_id == doc_id,
                    ResourceEvent.version == version,
                    ResourceEvent.op == "upsert",
                )
                .order_by(ResourceEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        point = build_resource_point(
            context=context,
            resource_id=resource_id,
            doc_id=doc_id,
            version=version,
            content=content,
            projected=projected,
            embedding=embedding,
            updated_at=ingested_at or memory.updated_at or memory.created_at or utcnow(),
            memory_id=memory.id,
        )
        try:
            await self.qdrant_client.upsert(
                collection_name=collection_name,
                points=[point],
                wait=True,
            )
        except Exception as e:
            raise QdrantError(f"Failed to upsert point: {e}") from e

        logger.info(
            "resource_point_rebuilt",
            memory_id=str(memory.id),
            point_id=str(point.id),
            collection=collection_name,
        )
        return memory.point_id

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

    async def _existing_resource_doc_ids(
        self,
        resource_id: str,
        context: Context,
        doc_ids: set[str],
    ) -> set[str]:
        """Which of ``doc_ids`` already have a live memory in this context (#1549).

        One SELECT on the generated ``resource_doc_id`` column, scoped exactly
        like ``_apply_upsert``'s existing-memory lookup minus the version, so
        the daily quota charges only doc_ids the batch will actually create.
        Soft-deleted rows do not count: a doc the user forgot is a creation
        again when the connector re-syncs it.
        """
        if not doc_ids:
            return set()
        result = await self.db.execute(
            select(Memory.resource_doc_id)
            .where(
                Memory.user_id == str(context.created_by),
                Memory.workspace_id == context.workspace_id,
                Memory.context_id == context.id,
                Memory.resource_id == resource_id,
                Memory.resource_doc_id.in_(doc_ids),
                Memory.deleted_at.is_(None),
            )
            .distinct()
        )
        return {doc_id for doc_id in result.scalars().all() if doc_id is not None}

    async def _get_or_create_state(
        self,
        resource_id: str,
        context_id: UUID,
        resource_pk: UUID,
    ) -> IndexerState:
        """Get or create indexer state.

        Issue #390 Phase 2: lookup uses the dual-row OR fallback pattern
        (resource_indexer.get_indexer_status_for_context:156-162) so
        legacy ``resource_pk IS NULL`` rows still resolve correctly during
        the writer transition. Newly created rows populate both columns,
        satisfying the before_insert invariant listener.

        Args:
            resource_id: Resource ID (slug, legacy mirror)
            context_id: Context ID (workspace-scoped FK)
            resource_pk: Authoritative ``resources.id`` UUID

        Returns:
            IndexerState record
        """
        # Prefer rows with resource_pk populated; fall back to legacy
        # ``resource_pk IS NULL`` rows scoped by slug + context_id during
        # the Phase 1 → Phase 2 transition window.
        result = await self.db.execute(
            select(IndexerState)
            .where(
                IndexerState.context_id == context_id,
                or_(
                    IndexerState.resource_pk == resource_pk,
                    (IndexerState.resource_pk.is_(None))
                    & (IndexerState.resource_id == resource_id),
                ),
            )
            .order_by(
                # Populated rows sort before legacy NULLs; id DESC is the
                # tiebreaker if an in-flight writer leaves both shapes.
                IndexerState.resource_pk.is_(None).asc(),
                IndexerState.id.desc(),
            )
            .limit(1)
        )
        state = result.scalar_one_or_none()

        if not state:
            state = IndexerState(
                resource_pk=resource_pk,
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
        resource_pk: UUID,
        after_id: int,
        limit: int,
    ) -> list[ResourceEvent]:
        """Fetch pending events since last_offset.

        ResourceEvent has no ``context_id`` column, so slug is not a
        workspace boundary — filter strictly by ``resource_pk``.
        """
        result = await self.db.execute(
            select(ResourceEvent)
            .where(
                ResourceEvent.resource_pk == resource_pk,
                ResourceEvent.id > after_id,
            )
            .order_by(ResourceEvent.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def _get_latest_schema(
        self,
        resource_pk: UUID,
    ) -> ResourceSchema | None:
        """Get latest schema version for resource (strict resource_pk filter)."""
        result = await self.db.execute(
            select(ResourceSchema)
            .where(ResourceSchema.resource_pk == resource_pk)
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
