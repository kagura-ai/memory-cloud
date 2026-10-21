"""Memory service for business logic.

Orchestrates memory operations across PostgreSQL, Qdrant, and Redis.
Implements remember(), recall(), forget(), reference() APIs.
Issue #82: Context-based multi-collection support.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import math
import os
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, tuple_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession

from config.retention import should_promote_to_persistent
from db.graph_learning_lock import try_lock_graph_learning
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
    MEMORY_TYPE_TIME,
    REVISION_KIND_CREATED,
    REVISION_KIND_FORGOTTEN,
    REVISION_KIND_UPDATED,
    REVISION_WRITER_API,
    SOURCE_TYPE_CONNECTOR,
    SOURCE_TYPE_MANUAL,
    Memory,
    MemoryRevision,
    tool_triggered_predicate,
)
from models.schemas import (
    ExploreHint,
    ExploreRequest,
    ExploreResponse,
    FoldedVersions,
    ForgetRequest,
    ForgetResponse,
    GuardrailItem,
    LinkedMemoryRef,
    LoadGuardrailsResponse,
    LoadPinnedResponse,
    MemoryResponse,
    MemoryRevisionItem,
    MemoryRevisionsResponse,
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
    SelectorReport,
    SupersedeCandidate,
    UpdateMemoryRequest,
    UpdateMemoryResponse,
    WriteLintHint,
)
from repositories.memory import AccessStats, MemoryRepository
from services.capacity_lock import CapacityPolicy
from services.content_chunk_writer import (
    chunk_cascade_needed,
    requeue_embedding,
    write_content_chunks,
)
from services.context_routing import resolve_collection_name
from services.context_service import ContextService
from services.embedding_service import EmbeddingService
from services.identity_link_service import link_set_reads, owned_by
from services.memory_versions import (
    check_expected_version,
    record_edit,
    record_revision,
    revision_values,
    stamp_writer,
    versioned_snapshot,
)
from services.persistence import persistence_info
from services.query_router import classify_query
from services.recall_selection import (
    RecallSelectionConfig,
    RecallSelectionPlan,
    plan_recall_selection,
)
from services.recall_timings import RecallTimings
from services.reranker_service import RERANK_UNSCORED_KEY
from services.search_service import SearchService
from services.supersede_dismissal import RESURFACE_SIMILARITY_DELTA, unconditional_tombstone
from services.supersede_fold import fold_linked_versions, fold_links, fold_nodes
from services.write_provenance import current_provenance
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import (
    ConfigurationError,
    EmbeddingSpendCapExceeded,
    ExternalServiceError,
    FeatureNotAvailableError,
    MemoryCloudException,
    MemoryGoneError,
    NotFoundException,
    ValidationError,
)
from utils.logger import get_logger
from utils.query_log import query_log_fields
from utils.recall_projection import content_slice, effective_full
from utils.tag_normalize import (
    MAX_TAG_GROUPS,
    TAG_GROUPS_FILTER_KEY,
    tag_filter_groups,
    tag_filter_groups_any,
)

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from models.memory import Memory
    from services.resource_indexer import ResourceIndexer

# Issue #886: upper bound for the always-load cap, matching the REST schema's
# ``LoadPinnedRequest.cap`` le=1000. The MCP path sends the raw arg with no
# Pydantic validation, so the service clamps defensively (see _clamp_pinned_cap).
_PINNED_LOAD_CAP_MAX = 1000

# #1941: rows deleted and committed per batch by ``forget_bulk`` — bounds the
# transaction (and the lock window on the rows) for a context of any size.
FORGET_BULK_CHUNK = 500
# Rows one forget-bulk request deletes at most; the client repeats the request
# while the response says ``remaining`` (bounds request time and lock windows).
FORGET_BULK_MAX_PER_REQUEST = 2000

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


class _RecallPlan(NamedTuple):
    """Effective query parameters resolved once at the top of ``recall()``.

    Attributes:
        request: The request with a concrete ``search_mode`` (#1212). Use this
            downstream, never the caller's original — it is a ``model_copy``, so
            a caller reusing one RecallRequest never sees it rewritten (#1220).
        effective_workspace_id: The workspace that pays for and scopes the
            search — the context owner's on a #708 shared-context read,
            otherwise the caller's.
        is_shared_context_read: True when #708 Option A routing is in effect.
        neural_enabled: ENABLE_NEURAL_MEMORY flag, read once.
        search_context_id: Single context id, or the #81 cross-context list.
        search_config: Prefetched per-context search config, or None when the
            read failed or the call is cross-context (#1220 fail-open).
    """

    request: RecallRequest
    effective_workspace_id: UUID
    is_shared_context_read: bool
    neural_enabled: bool
    search_context_id: str | list[str]
    search_config: Any


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


def _is_trusted_recall(request: RecallRequest) -> bool:
    """Whether a recall runs under ``filters={"trust_tier": "trusted"}`` (#887).

    The one reading of the trusted filter: the row filter, the supersede
    resolver's connector exclusion and the selection-evidence guard all ask
    here, so they cannot drift apart.
    """
    return (request.filters or {}).get("trust_tier") == "trusted"


def _rerank_sort_key(result: dict, adjusted: float) -> tuple[bool, float]:
    """Sort key for a bounded re-rank over (possibly) reranked results (#2062).

    A candidate the reranker could not score (``RERANK_UNSCORED_KEY``, stamped
    by RerankerService) has its hybrid_score floored below the scored ones
    (#2084), but a reinforce or graph-boost factor could still lift it over a
    scored one. Sorted descending, it stays after every scored candidate and
    only moves among the unscored ones. Without a rerank
    nothing is stamped and the order is the adjusted score's alone.
    """
    return (not result.get(RERANK_UNSCORED_KEY, False), adjusted)


def metadata_payload_patch(
    request: Any, normalized_ctx_summary: str | None, effective_details: Any
) -> tuple[dict[str, Any], list[str]]:
    """The Qdrant payload patch of a metadata-only edit: ``(updates, delete_keys)``.

    ``request`` is read for ``tags`` / ``importance`` / ``type`` / ``details``
    (None = not changed). Without ``updated_at``: the caller stamps it when it
    applies the patch. Shared by ``update_memory``'s in-transaction sync and
    ``rollback_session``'s post-commit one (#2044), so the two cannot drift.
    """
    payload_updates: dict[str, Any] = {}
    delete_keys: list[str] = []
    if request.tags is not None:
        payload_updates["tags"] = request.tags
    if request.importance is not None:
        payload_updates["importance"] = request.importance
    if request.type is not None:
        payload_updates["type"] = request.type
    if normalized_ctx_summary is not None:
        payload_updates["context_summary"] = normalized_ctx_summary
    # WHERE axis (#1332): a details write changes the geo payload —
    # sync it (or clear it when the location was removed) so
    # filters.near never matches a stale coordinate.
    if request.details is not None:
        loc = location_payload_from_details(effective_details)
        if loc is not None:
            payload_updates["location"] = loc
        else:
            delete_keys.append("location")
    return payload_updates, delete_keys


# Keys of a metadata patch an indexer point is not given (#2100). Its
# ``updated_at`` is the document's event time, which recall's updated_after /
# updated_before filters read, not the time the row was last edited. Its
# importance is the indexer's and Sleep's business: the re-evaluation and its
# undo leave the indexer point without one (#2065), so an edit that wrote one
# would leave a value nothing else keeps current.
RESOURCE_POINT_SKIPPED_KEYS = frozenset({"importance", "updated_at"})


@dataclass(frozen=True)
class MetadataPatchTarget:
    """Where a metadata-only payload patch goes, and what it carries (#2100)."""

    point_id: UUID
    updates: dict[str, Any]
    delete_keys: list[str]
    own_point: bool  # the point is the memory's own id (it may have chunks)


def resource_point_patch(
    updates: dict[str, Any], delete_keys: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """``(updates, delete_keys)`` without the keys an indexer point is not given."""
    return (
        {key: value for key, value in updates.items() if key not in RESOURCE_POINT_SKIPPED_KEYS},
        [key for key in delete_keys if key not in RESOURCE_POINT_SKIPPED_KEYS],
    )


async def metadata_patch_target(
    db: AsyncSession,
    memory_id: UUID,
    point_id: UUID,
    updates: dict[str, Any],
    delete_keys: list[str],
) -> MetadataPatchTarget | None:
    """The point a metadata-only edit of a memory is merged into, or ``None`` (#2100).

    The point is ``Memory.point_id`` — a resource-indexer row's is the
    document version's uuid5, not the row id — resolved by the rule
    ``forget`` and the Sleep archive / merge use (#2065,
    ``unshared_point_id``). A point another live row names too (the same
    document indexed into two contexts that share a collection) is left as
    it is: one row's edit must not rewrite the filters of the other row. The
    edit stays in Postgres and a warning is logged. ``unshared_point_id`` is
    not scoped by collection, so two contexts with separate collections that
    index the same document version also count as sharing — the same
    conservative answer ``forget`` gives.

    The patch is applied with set-payload (a merge), never as a payload
    overwrite, so an indexer point keeps its own fields (``content``,
    ``facets``, ``sortable``, ``metadata``, ``updated_at``, ``indexed_at``,
    ...). Of the edited keys it gains ``tags``, ``type``,
    ``context_summary`` and ``location`` (set or cleared) — the keys
    recall's payload filters read, so they agree with the row — and not
    ``RESOURCE_POINT_SKIPPED_KEYS``. ``scope`` is not editable by update or
    PATCH and is never in the patch. A point that is the memory's own id
    gets the patch unchanged.

    Args:
        db: The session (the shared-point lookup runs in a SAVEPOINT).
        memory_id: The edited memory.
        point_id: Its ``Memory.point_id``.
        updates / delete_keys: The metadata patch (``metadata_payload_patch``
            plus ``updated_at``).

    Returns:
        The target, or ``None`` when the point is shared or nothing is left
        to write to it.
    """
    from services.resource_indexer import unshared_point_id

    target = await unshared_point_id(db, memory_id, point_id)
    if target is None:
        logger.warning(
            "memory_metadata_sync_shared_point_skipped",
            memory_id=str(memory_id),
            point_id=str(point_id),
        )
        return None
    if target == memory_id:
        return MetadataPatchTarget(target, updates, list(delete_keys), own_point=True)
    updates, delete_keys = resource_point_patch(updates, delete_keys)
    if not (updates or delete_keys):
        return None
    return MetadataPatchTarget(target, updates, delete_keys, own_point=False)


def location_payload_from_details(details: dict | None) -> dict | None:
    """Qdrant ``location`` geo payload value from a details dict (#1332).

    Returns ``{"lat": float, "lon": float}`` only for a complete numeric
    pair, else None — never raises. Contract-valid writes are already
    normalized by ``_apply_location`` (422 on bad input); legacy rows that
    predate the contract simply carry no geo payload, mirroring the e69
    generated columns' NULL behavior.

    Payload-source invariant (do NOT "unify" these): the update/patch
    metadata-only branches call THIS helper on ``effective_details``
    because the ORM has not round-tripped the generated columns yet, while
    ``process_pending_embedding``, sleep reindex/undo, and the startup
    backfill read ``memory.location_lat/lon`` — the PG-validated source.
    The two derivations agree only because ``_apply_location`` ran first on
    caller-supplied details; consolidating the embed/backfill paths onto
    this details parser would resurrect the #1344 legacy-value divergence
    (PG lane and Qdrant lane disagreeing per row).
    """
    if not isinstance(details, dict):
        return None
    loc = details.get("location")
    if not isinstance(loc, dict):
        return None
    lat, lon = loc.get("lat"), loc.get("lon")
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    return {"lat": float(lat), "lon": float(lon)}


# The request fields ``_update_apply_fields`` writes onto the row. Named here so
# a dismissal-only update (#1504) can tell "no content changed" from "content
# changed", and so adding a field to that method forces a change here too.
_UPDATE_CONTENT_FIELDS = (
    "summary",
    "context_summary",
    "content",
    "details",
    "type",
    "importance",
    "tags",
    "context",
    "delivery_mode",
)


# ---------------------------------------------------------------- #1853 writes
DEDUPE_SUGGEST = "suggest"  # today: write, flag a near-duplicate on a later recall
DEDUPE_CHECK = "check"  # ask first: a candidate comes back, nothing is written
DEDUPE_OFF = "off"  # write, and never suggest for this memory
DEDUPE_MODES: tuple[str, ...] = (DEDUPE_SUGGEST, DEDUPE_CHECK, DEDUPE_OFF)
MAX_BATCH_ITEMS = 50
_DEDUPE_CHECK_K = 5  # hits read per duplicate check; the first live, permitted one counts


class DuplicateCandidateError(Exception):
    """``dedupe="check"`` found a live near-duplicate; nothing was written."""

    def __init__(self, candidate: dict[str, Any]):
        super().__init__(f"duplicate candidate {candidate.get('memory_id')}")
        self.candidate = candidate


class DedupeUnavailableError(Exception):
    """The duplicate check could not run (embedder down); nothing was written."""


class BatchItemError(Exception):
    """``remember_many`` failed on ``index``; the whole batch was rolled back."""

    def __init__(self, index: int, cause: BaseException):
        super().__init__(f"batch item {index}: {cause}")
        self.index = index
        self.cause = cause


class BatchCommittedError(Exception):
    """``remember_many`` was cancelled (a timeout) AFTER its commit (#1873).

    Every row is stored and has its embedding task; ``responses`` holds one
    per request, in order. The caller must not report a rollback, and the
    batch must not be sent again."""

    def __init__(self, responses: list[RememberResponse]):
        super().__init__(f"batch of {len(responses)} committed before the cancellation")
        self.responses = responses


@dataclass
class _PreparedRemember:
    """A ``remember`` row that is built and gated but not yet committed."""

    memory: Memory
    request: RememberRequest
    user_id: str
    workspace_id_str: str
    context_id_str: str
    tag_hints: list[WriteLintHint]
    embedding_scheduled: bool = False


def _recall_audit_metadata(
    responses: list[MemoryResponse],
    binding_row_filtered: int,
    selector: SelectorReport | None = None,
) -> dict[str, Any] | None:
    """``event_metadata`` of the recall success audit row (#1299, #2124, #2144).

    #1299: the enforce-mode row-filter count. #2124: the ids of the results
    that shipped their content — a Layer-3 read that reference() audits on its
    own row, so a recall that carries content says which memories it read
    (identifiers only, capped like every id list in the audit metadata).
    #2144: when the LLM selector applied, ``selector_applied`` and
    ``selector_dropped_count`` (counts only; the content ids above are the
    selected page's).
    """
    from services.agent_binding_service import ROW_FILTER_KIND
    from services.memory_access_event_writer import MAX_METADATA_MEMORY_IDS

    metadata: dict[str, Any] = {}
    if binding_row_filtered:
        metadata["filter_kind"] = ROW_FILTER_KIND
        metadata["binding_row_filtered_count"] = binding_row_filtered
    content_ids = [str(r.memory_id) for r in responses if r.content is not None]
    if content_ids:
        metadata["content_memory_ids"] = content_ids[:MAX_METADATA_MEMORY_IDS]
        metadata["content_count"] = len(content_ids)
    if selector is not None and selector.applied:
        metadata["selector_applied"] = True
        metadata["selector_dropped_count"] = selector.dropped or 0
    return metadata or None


def _without_dropped_rows(
    search_results: list[dict],
    memories: Mapping[str, Any],
    page: list[MemoryResponse],
    kept: list[MemoryResponse],
) -> list[dict]:
    """The candidate pool minus the page rows the #2144 selector dropped.

    Dropped = on the page before selection, not after. Candidates beyond the
    page stay: the selector never judged them.
    """
    dropped = {r.memory_id for r in page} - {r.memory_id for r in kept}
    return [
        sr
        for sr in search_results
        if (memory := memories.get(sr["id"])) is None or memory.id not in dropped
    ]


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
        that must defend the LIMIT: ``None`` → the settings default; otherwise
        coerce to int. Either value is then clamped to [1, _PINNED_LOAD_CAP_MAX]
        — the settings default too, so a misconfigured ``pinned_load_cap`` /
        ``guardrail_load_cap`` cannot reach the query. Clamping the lower bound
        to 1 is load-bearing — a 0 would emit ``LIMIT 0`` (empty set + a false
        truncated=true) and a negative would emit ``LIMIT -1`` (no cap at all,
        defeating the safety valve).
        """
        if cap is None:
            cap_int = default
        else:
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
        if memory_type != MEMORY_TYPE_TIME:
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

    @staticmethod
    def _apply_tool_trigger(details: dict | None) -> dict | None:
        """Validate + normalize ``details.tool_trigger`` (tool guardrail marking).

        ``_apply_location``'s sibling: an orthogonal gate that fires on the
        ``tool_trigger`` key's presence for any memory type, applied only to
        caller-supplied details so an untouched legacy row never 422s. The
        grammar and structural rules live in ``utils.tool_trigger``; the server
        compiles the patterns once and never runs them. An explicit
        ``"tool_trigger": null`` REMOVES the key (the unmark path) — ``details``
        is a PostgreSQL ``json`` column, so a stored JSON ``null`` would still
        satisfy the ``idx_memories_tool_trigger`` predicate.

        Raises:
            ValueError: ``invalid details.tool_trigger: <code>: <sentence>`` —
                the established "bad request" signal inside MemoryService; the
                ``<code>`` token is stable (``TOOL_TRIGGER_ERROR_CODES``).
        """
        from utils.tool_trigger import ToolTriggerValidationError, normalize_tool_trigger

        try:
            return normalize_tool_trigger(details)
        except ToolTriggerValidationError as exc:
            raise ValueError(f"invalid details.tool_trigger: {exc}") from exc

    @staticmethod
    def _touches_tool_trigger(
        existing_details: Any, new_details: Any, *, details_supplied: bool
    ) -> bool:
        """Does this write add, change or remove a guardrail marking?

        True when the stored row already carries ``details.tool_trigger`` (any
        edit or delete of a guardrail is a protection change — including a
        summary rewrite, or a wholesale ``details`` replace that drops the
        key) or when caller-supplied details name the key (adding, changing,
        or an explicit ``null`` to unmark). A stored ``tool_trigger: null`` or
        non-object legacy shape is "not a guardrail", matching
        ``Memory.is_tool_triggered`` and the SQL predicate.
        """
        if MemoryService._carries_tool_trigger(existing_details):
            return True
        return details_supplied and isinstance(new_details, dict) and "tool_trigger" in new_details

    @staticmethod
    def _carries_tool_trigger(details: Any) -> bool:
        """``Memory.is_tool_triggered`` spelled on the raw ``details`` value.

        For callers that hold the column value rather than a mapped row — and
        for the write-response paths, where unit tests build the row as a bare
        ``MagicMock`` (the #1523 trap: a mocked property is truthy, a mocked
        ``details`` is not a dict).
        """
        return isinstance(details, dict) and details.get("tool_trigger") is not None

    async def _require_guardrail_author(self, user_id: str, context_id: UUID | str | None) -> None:
        """Authorize a guardrail write: context EDITOR or above, user credential only.

        Memory writes otherwise authorize on workspace *membership*, which lets
        a workspace VIEWER edit ``details`` in a shared context. A tool guardrail
        is injected into every member's agent session by client hooks (with a
        deny capability for ``action="block"``), so marking, changing, removing
        or deleting one is held to the role-aware chokepoint
        ``PermissionService.check_context_write`` (workspace owner/admin, a
        ContextMember editor/owner, or the creator of a private context).

        An agent credential (``get_agent_scope()`` set) can never author a
        guardrail: the row records no agent identity, so a guardrail written by
        an automated agent that ingested untrusted content would be
        indistinguishable from a human one.

        Raises:
            ValueError: ``invalid details.tool_trigger:
                tool_trigger_requires_user_credential`` for an agent credential.
            AuthorizationError: the caller is below context EDITOR.
        """
        from auth.agent_scope import get_agent_scope
        from services.permission_service import PermissionService
        from utils.tool_trigger import ToolTriggerValidationError

        if get_agent_scope() is not None:
            raise ValueError(
                "invalid details.tool_trigger: "
                + str(
                    ToolTriggerValidationError(
                        "tool_trigger_requires_user_credential",
                        "tool guardrails are authored with a user credential; an agent "
                        "credential cannot add, change or remove details.tool_trigger",
                    )
                )
            )
        if context_id is None:
            raise ValueError("a tool guardrail write requires a context")
        # ALLOWED: forget / forget_bulk reach this for a guardrail row and must
        # keep working under the lock (the cleanup surface); the write paths
        # are gated upstream by _ensure_capacity.
        await PermissionService(self.db).check_context_write(
            user_id, UUID(str(context_id)), capacity=CapacityPolicy.ALLOWED
        )

    async def _may_delete_guardrail(self, user_id: str, memory: Any) -> bool:
        """``forget``'s guardrail gate, shared by the by-id and by-query branches.

        Deleting a guardrail silently stops it firing in every member's client
        hook, so it takes context EDITOR or above and a user credential —
        membership (what ``can_access_memory`` checks) is not enough. Returns
        False for a guardrail row the caller may not delete; the caller skips
        that row so ``forget`` keeps its contract (a target the caller may not
        delete is skipped, never a 403). Plain rows always pass.
        """
        if not memory.is_tool_triggered:
            return True
        from utils.exceptions import AuthorizationError

        try:
            await self._require_guardrail_author(user_id, memory.context_id)
        except (AuthorizationError, ValueError):
            logger.warning(
                "forget_guardrail_author_denied", memory_id=str(memory.id), user_id=user_id
            )
            return False
        return True

    @staticmethod
    def _reject_context_location(context: dict | None) -> None:
        """Enforce the "coordinates never in ``context``" rule (#1331, spec §4).

        ``memory.context`` JSONB is replicated verbatim into the Qdrant point
        payload, so a ``location`` key there would push coordinates into the
        second store the WHERE axis deliberately avoids — and the generated
        columns read only ``details.location``, so such a memory would also
        be invisible to recall_nearby. Documentation alone leaves that a
        silent double failure; reject it loudly at every write path that
        accepts caller-supplied context.

        Raises:
            ValueError: when the caller-supplied context carries 'location'.
        """
        if context is not None and isinstance(context, dict) and "location" in context:
            raise ValueError(
                "context.location is not allowed — put coordinates in "
                "details.location (context is replicated into the search-index "
                "payload store)"
            )

    async def _ensure_capacity(
        self,
        user_id: str,
        *,
        workspace_id: UUID | str | None = None,
        context_ids: list[UUID] | tuple[UUID | None, ...] = (),
        write_context_id: UUID | str | None = None,
    ) -> None:
        """Refuse when the target workspace is over its Free capacity (#1941).

        The second line behind the MCP dispatcher: every REST and MCP read or
        write that reaches this service is checked against the workspace that
        OWNS the data (the context's, not the caller's session workspace).
        ``forget``, listing, stats and export never call it.

        ``write_context_id`` (#1943): the context a content write targets —
        refused as well when that context has no writable slot on the plan.
        """
        from services.capacity_lock import (
            ensure_contexts_not_capacity_locked,
            ensure_not_capacity_locked,
        )
        from services.plan_suspension import ensure_context_writable

        if workspace_id is not None:
            await ensure_not_capacity_locked(self.db, workspace_id, user_id=user_id)
        if any(c is not None for c in context_ids):
            # One query for every context, deduplicated per workspace (#1941).
            await ensure_contexts_not_capacity_locked(self.db, context_ids, user_id=user_id)
        if write_context_id is not None:
            await ensure_context_writable(self.db, write_context_id)

    async def remember(
        self,
        request: RememberRequest,
        user_id: str,
        client: str = "unknown",
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,  # NEW: Workspace ID (Issue #146)
        key_workspace_id: UUID | None = None,  # Issue #963/#1281: pure key scope
        _skip_daily_quota: bool = False,  # Issue #1549: internal, see _upsert_by_external_id
        *,
        tags_normalize: bool = False,
        dedupe: str = DEDUPE_SUGGEST,
        tag_canonical: dict[str, list[tuple[str, int]]] | None = None,
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
            current_workspace_id: Caller-selected workspace, kept for call-site
                compatibility. Quotas are checked against the *context's*
                workspace (the one the row is written to), not this value.
            _skip_daily_quota: Issue #1549 — do not charge the daily
                memory-creation quota. Private: only ``_upsert_by_external_id``
                sets it, when the new row REPLACES an existing external_id
                (an update from the caller's point of view, not a creation).

        Returns:
            RememberResponse with memory_id and scope

        Example:
            >>> result = await service.remember(request, "kiyota", "claude-sonnet-4")
            >>> result.memory_id
            UUID('...')
            >>> result.scope
            'working'
        """
        prepared = await self._prepare_remember(
            request,
            user_id,
            client=client,
            current_context_id=current_context_id,
            current_workspace_id=current_workspace_id,
            key_workspace_id=key_workspace_id,
            _skip_daily_quota=_skip_daily_quota,
            tags_normalize=tags_normalize,
            dedupe=dedupe,
            tag_canonical=tag_canonical,
        )
        try:
            await self.memory_repo.create(prepared.memory)
            # #2043: the created event commits with the row.
            record_revision(
                self.db, prepared.memory, kind=REVISION_KIND_CREATED, actor_user_id=user_id
            )
            await self.db.commit()
            return await self._finish_remember(prepared)
        except Exception as e:
            await self.db.rollback()
            logger.error(
                "memory_creation_failed",
                memory_id=str(prepared.memory.id),
                user_id=user_id,
                error=str(e),
            )
            raise

    async def _prepare_remember(
        self,
        request: RememberRequest,
        user_id: str,
        *,
        client: str,
        current_context_id: UUID | None,
        current_workspace_id: UUID | None,
        key_workspace_id: UUID | None,
        _skip_daily_quota: bool,
        tags_normalize: bool,
        dedupe: str,
        isolation: tuple[Any, str | None, str | None] | None = None,
        tag_canonical: dict[str, list[tuple[str, int]]] | None = None,
    ) -> _PreparedRemember:
        """Everything ``remember`` does before the row is committed (#1853).

        ``isolation`` is the already-resolved ``(context, workspace_id_str,
        context_id_str)`` when the caller prepares several rows for one
        context (``remember_many``); ``tag_canonical`` the per-call fold map
        for ``tags_normalize``, shared so later items see earlier spellings.

        Context resolution and the write gate, the optional duplicate check,
        quotas, size, the details transforms and the row itself — but no
        ``create`` / ``commit``: ``remember`` commits one row, ``remember_many``
        several in one transaction. The caller owns the transaction.
        """
        # Single Collection Migration: Extract isolation params (optimized).
        # Issue #1275: remember is a WRITE — gate the declared context against
        # the agent binding (no-op for non-agent credentials).
        #
        # Resolved BEFORE the quota gates (#1549 review): the row is stamped
        # into the CONTEXT's workspace below, so that is the workspace whose
        # quotas must be checked and charged — not the caller-selected
        # ``current_workspace_id``, which may be unset (a bypass) or, for a
        # shared context the caller is also a member of, a different workspace
        # (the wrong counter). A workspace-scoped key or a binding denial is
        # still refused here, before any counter is touched.
        capacity_checked = isolation is not None  # remember_many checked the batch
        if isolation is None:
            isolation = await self._get_context_isolation_params(
                user_id,
                current_context_id,
                access="write",
                key_workspace_id=key_workspace_id,
                operation="remember",  # #1286 (P0-5): deny-capture audit identity
            )
        context, workspace_id_str, context_id_str = isolation

        # Validate required parameters
        if not workspace_id_str or not context_id_str:
            raise ValueError("remember() requires current_context_id")

        # #1941: after the write gate, so a context the caller cannot write
        # answers its uniform not-found, never the lock.
        if not capacity_checked:
            await self._ensure_capacity(
                user_id, workspace_id=workspace_id_str, write_context_id=context_id_str
            )

        # #1853: dedupe="check" asks BEFORE the quota is charged and the row
        # exists — a candidate above the suggestion threshold comes back to
        # the caller as a decision (DuplicateCandidateError), nothing is stored.
        if dedupe == DEDUPE_CHECK:
            await self._refuse_duplicate(
                request,
                user_id=user_id,
                workspace_id_str=workspace_id_str,
                context_id_str=context_id_str,
            )

        # Issue #149: Check quota before creating memory
        # Single Collection Migration: Memory count only (storage size removed)
        from services.quota_service import QuotaService

        target_workspace_id = UUID(workspace_id_str)
        quota_service = QuotaService(self.db)

        # Check memory quota (count-based only) — against the workspace the row
        # lands in; the resolution above is shared with the daily gate.
        can_create, error = await quota_service.check_memory_quota(
            target_workspace_id, raise_on_exceeded=True
        )

        # Issue #1549: reserve one creation against today's daily quota.
        # This is THE charge point for user-visible memory creation — MCP
        # remember, REST remember and the create half of the external_id
        # upsert all arrive here (see check_memories_per_day's docstring).
        if not _skip_daily_quota:
            await quota_service.check_memories_per_day(
                target_workspace_id, count=1, raise_on_exceeded=True
            )

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
            from utils.exceptions import ContentSizeExceededError

            raise ContentSizeExceededError(actual=content_size, limit=MAX_CONTENT_SIZE)

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
        # gate — fires on key presence for any type); coordinates must never
        # ride the Qdrant-replicated context JSONB.
        request.details = self._apply_location(request.details)
        # Tool guardrails: validate/normalize details.tool_trigger (orthogonal
        # gate, any type). Marking a memory as a guardrail is a protection
        # change every member's client hook acts on, so it also needs context
        # EDITOR or above and a user credential.
        request.details = self._apply_tool_trigger(request.details)
        if self._touches_tool_trigger(None, request.details, details_supplied=True):
            await self._require_guardrail_author(user_id, context_id_str)
        self._reject_context_location(request.context)

        # #1853: tags_normalize maps each tag onto the established spelling
        # that differs only mechanically (case / separators / simple plural,
        # never the numbers — #1871) and reports the mapping in lint.
        tag_hints: list[WriteLintHint] = []
        if tags_normalize and request.tags:
            request.tags, tag_hints = await self._normalize_tags_for_write(
                request.tags,
                workspace_id=UUID(workspace_id_str),
                context_id=UUID(context_id_str),
                user_id=user_id,
                canonical=tag_canonical,
            )

        # Create memory entity first with pending status
        memory = Memory(
            id=memory_id,
            user_id=user_id,
            workspace_id=UUID(workspace_id_str),  # NOT NULL since e95 (#1951); guarded above
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
            # #1853 dedupe="off": no suggestion later either — the wildcard
            # tombstone tells the post-embed detection this write was declared
            # a non-duplicate (is_dismissed reads it as "still rejected").
            supersede_candidate=unconditional_tombstone() if dedupe == DEDUPE_OFF else None,
            # #2043: explicit, so the version is known before the INSERT applies
            # the column default (the created revision and the response read it).
            version=1,
        )
        # #2044: the writer's agent / session go into the INSERT itself (the
        # created revision reads the same values and leaves the row clean).
        stamp_writer(memory, current_provenance())
        # Issue #886: pin-on-write. delivery_mode='always' pins straight to
        # persistent (so it is exempt from sleep consolidation — which only acts
        # on scope='working' — without waiting for a consolidation pass).
        self._apply_pin_on_write(memory)
        return _PreparedRemember(
            memory=memory,
            request=request,
            user_id=user_id,
            workspace_id_str=workspace_id_str,
            context_id_str=context_id_str,
            tag_hints=tag_hints,
        )

    async def _finish_remember(self, prepared: _PreparedRemember) -> RememberResponse:
        """Everything ``remember`` does after the row is committed (#1853):
        declared links, the embedding task, the access event and the response
        with its persistence block and lint."""
        memory = prepared.memory
        request = prepared.request
        user_id = prepared.user_id
        memory_id = memory.id
        workspace_id_str = prepared.workspace_id_str
        context_id_str = prepared.context_id_str

        logger.info(
            "memory_created_pending",
            memory_id=str(memory_id),
            user_id=user_id,
            type=request.type,
        )

        # Issue #215: Create declared_link edges (best-effort, after commit).
        # BEFORE the embedding task: the post-embed detection checks for an
        # existing supersedes edge, and a cached embedding makes that check
        # race the edge write if the task is started first.
        await self._create_declared_links(
            memory_id=memory_id,
            request=request,
            user_id=user_id,
            workspace_id=workspace_id_str,
            context_id=context_id_str,
        )

        self._schedule_embedding(prepared)

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

        # #1502: advisory recall-ability hints. Deliberately AFTER the
        # commit and the audit row — the memory is stored either way, and
        # lint_write swallows its own errors, so nothing here can turn a
        # successful write into a failure.
        return RememberResponse(
            memory_id=memory_id,
            scope=memory.scope,
            version=memory.version,  # #2043
            # #1505: say what 'working' means for durability instead of
            # leaving the caller to guess.
            persistence=persistence_info(
                memory.scope,
                pinned=memory.is_pinned,
                tool_triggered=self._carries_tool_trigger(memory.details),
            ),
            lint=prepared.tag_hints
            + await self._lint_write(
                workspace_id=UUID(workspace_id_str),
                context_id=UUID(context_id_str),
                user_id=user_id,
                summary=request.summary,
                tags=request.tags,
                memory_id=memory_id,
            ),
        )

    async def remember_many(
        self,
        requests: list[RememberRequest],
        *,
        user_id: str,
        client: str = "unknown",
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
        key_workspace_id: UUID | None = None,
        tags_normalize: bool = False,
        dedupe: str = DEDUPE_SUGGEST,
    ) -> list[RememberResponse]:
        """Store several memories in ONE transaction (#1853, ``atomic=true``).

        Every request is prepared and its row added first; one failure —
        including a ``dedupe="check"`` candidate — rolls everything back and
        is re-raised as :class:`BatchItemError` carrying the failing index, so
        nothing is written. Only after the single commit do the per-row
        post-commit steps run (embedding tasks, declared links, access
        events).

        Daily quota (#1873): the whole batch is reserved once, all or
        nothing (``QuotaService.reserve_memories_per_day``, the
        ``check_memories_per_day`` gate with ``count=len(requests)``), after
        the duplicate checks and before any row. A batch that does not fit
        raises :class:`QuotaExceededError` itself (not wrapped: no single
        item is at fault) and charges nothing; a batch that is rolled back
        later — a failing item, a failed commit, a cancellation before the
        commit — gives back exactly what it reserved, so a refused batch
        never uses up the day's budget.

        Cancellation (#1873): the commit is shielded, so a timeout either
        lands before it (rolled back, ``CancelledError`` propagates) or after
        it. After it, every remaining row still gets its embedding task and
        :class:`BatchCommittedError` carries the responses — the rows exist
        and the caller must say so.
        """
        # Resolved once for the whole batch (same write gate as remember; the
        # audit identity stays "remember" — the items are remembers).
        isolation = await self._get_context_isolation_params(
            user_id,
            current_context_id,
            access="write",
            key_workspace_id=key_workspace_id,
            operation="remember",
        )
        _context, workspace_id_str, context_id_str = isolation
        if not workspace_id_str or not context_id_str:
            raise ValueError("remember_batch() requires current_context_id")
        await self._ensure_capacity(
            user_id, workspace_id=workspace_id_str, write_context_id=context_id_str
        )
        tag_canonical = (
            await self.tag_canonical_map(
                workspace_id=UUID(workspace_id_str),
                context_id=UUID(context_id_str),
                user_id=user_id,
            )
            if tags_normalize
            else None
        )
        if dedupe == DEDUPE_CHECK:
            # All duplicate checks BEFORE any row or quota lock: the first
            # prepare takes the workspace row lock (check_memory_quota), and
            # an embedder round trip per item must not run under it. Items of
            # one batch are not compared with each other (none is in the
            # vector store yet).
            for index, request in enumerate(requests):
                try:
                    await self._refuse_duplicate(
                        request,
                        user_id=user_id,
                        workspace_id_str=workspace_id_str,
                        context_id_str=context_id_str,
                    )
                except Exception as exc:
                    raise BatchItemError(index, exc) from exc
            dedupe = DEDUPE_SUGGEST  # checked; the rows carry no tombstone

        from services.quota_service import QuotaService

        # #1873: one all-or-nothing reservation on the workspace the rows land
        # in. ``reservation`` is what was really added to the counter (None
        # when the counter was unreachable): a rollback releases that and
        # nothing else.
        quota_service = QuotaService(self.db)
        reservation = await quota_service.reserve_memories_per_day(
            UUID(workspace_id_str), len(requests)
        )

        prepared: list[_PreparedRemember] = []
        try:
            for index, request in enumerate(requests):
                try:
                    item = await self._prepare_remember(
                        request,
                        user_id,
                        client=client,
                        current_context_id=current_context_id,
                        current_workspace_id=current_workspace_id,
                        key_workspace_id=key_workspace_id,
                        _skip_daily_quota=True,  # reserved above, for the whole batch
                        tags_normalize=tags_normalize,
                        dedupe=dedupe,
                        isolation=isolation,
                        tag_canonical=tag_canonical,
                    )
                    await self.memory_repo.create(item.memory)
                    record_revision(  # #2043: in the batch's one transaction
                        self.db, item.memory, kind=REVISION_KIND_CREATED, actor_user_id=user_id
                    )
                except Exception as exc:
                    raise BatchItemError(index, exc) from exc
                prepared.append(item)
            cancelled = await self._commit_shielded()
        except BaseException:  # a timeout's CancelledError must roll back too
            await self.db.rollback()
            await self._release_daily_reservation(quota_service, reservation)
            raise
        responses: list[RememberResponse] = []
        for item in prepared:
            if cancelled:
                # No further await: the caller is being cancelled. The row is
                # committed, so it still gets its embedding task and an answer.
                self._schedule_embedding(item)
                responses.append(self._bare_response(item))
                continue
            try:
                responses.append(await self._finish_remember(item))
            except asyncio.CancelledError:
                cancelled = True
                self._schedule_embedding(item)
                responses.append(self._bare_response(item))
            except Exception as exc:  # the row is committed; the batch must still report it
                logger.error(
                    "remember_many_finish_failed",
                    memory_id=str(item.memory.id),
                    error=str(exc),
                    exc_info=True,
                )
                self._schedule_embedding(item)  # the row must still become searchable
                responses.append(self._bare_response(item))
        if cancelled:
            logger.warning(
                "remember_many_cancelled_after_commit", user_id=user_id, count=len(responses)
            )
            raise BatchCommittedError(responses)
        return responses

    async def _commit_shielded(self) -> bool:
        """Commit so that a cancellation cannot leave the outcome unknown (#1873).

        The commit runs in its own task behind ``asyncio.shield``; a
        cancellation that arrives meanwhile is held until the commit has
        finished. Returns True when one arrived — the transaction is committed
        all the same. A commit that fails raises its own error.
        """
        commit = asyncio.ensure_future(self.db.commit())
        cancelled = False
        while not commit.done():
            try:
                await asyncio.shield(commit)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:  # noqa: S110 - re-raised from commit.result() below
                pass
        commit.result()
        return cancelled

    @staticmethod
    async def _release_daily_reservation(quota_service: Any, reservation: Any) -> None:
        """Give a rolled-back batch's daily reservation back. Best-effort: it
        runs while another error is propagating and must not replace it."""
        try:
            await quota_service.release_memories_per_day(reservation)
        except Exception as exc:
            logger.warning("memories_per_day_release_failed", error=str(exc))

    def _schedule_embedding(self, prepared: _PreparedRemember) -> None:
        """Start the embedding task for a committed row, once."""
        if prepared.embedding_scheduled:
            return
        memory_id = prepared.memory.id
        task = asyncio.create_task(process_pending_embedding(memory_id))
        task.add_done_callback(
            functools.partial(_log_embedding_task_result, memory_id=str(memory_id))
        )
        prepared.embedding_scheduled = True

    def _bare_response(self, prepared: _PreparedRemember) -> RememberResponse:
        """The response for a committed row whose post-commit steps failed:
        id, scope and persistence only — no lint, nothing that needs the session."""
        memory = prepared.memory
        return RememberResponse(
            memory_id=memory.id,
            scope=memory.scope,
            version=memory.version,  # #2043
            persistence=persistence_info(
                memory.scope,
                pinned=memory.is_pinned,
                tool_triggered=self._carries_tool_trigger(memory.details),
            ),
            lint=list(prepared.tag_hints),
        )

    async def _refuse_duplicate(
        self,
        request: RememberRequest,
        *,
        user_id: str,
        workspace_id_str: str,
        context_id_str: str,
    ) -> None:
        """``dedupe="check"``: raise :class:`DuplicateCandidateError` when a live
        near-duplicate exists — unless the request already supersedes that very
        memory, which is the documented way to resolve the candidate."""
        candidate = await self._find_duplicate_candidate(
            user_id=user_id,
            workspace_id_str=workspace_id_str,
            context_id_str=context_id_str,
            summary=request.summary,
        )
        if candidate is None:
            return
        # Only a memory the caller can actually supersede resolves the
        # candidate this way: for another member's memory the write would
        # succeed without the edge (``_create_declared_links`` refuses the
        # target) and the duplicate would stay visible.
        if (
            request.supersedes is not None
            and str(request.supersedes) == candidate["memory_id"]
            and candidate.get("supersedable", True)
        ):
            return
        raise DuplicateCandidateError(candidate)

    async def tag_canonical_map(
        self, *, workspace_id: UUID, context_id: UUID, user_id: str
    ) -> dict[str, list[tuple[str, int]]]:
        """``normalize_tag`` fold → every stored spelling with that fold, as
        (spelling, count), most frequent first, for the caller's view of the
        context (the #1512 vocabulary cache).

        Every spelling is kept, not only the most frequent (#1871): the fold is
        not injective on tags that carry numbers (``v0.1.10`` and ``v0.11.0``
        share one), and a single slot per fold would make the rarer of two such
        tags unreachable. Built once per write call;
        ``_normalize_tags_for_write`` appends the spellings it stores so later
        tags of the same request or batch fold onto them."""
        from services.tag_resolution import fetch_vocabulary_cached
        from utils.tag_normalize import normalize_tag

        vocabulary = await fetch_vocabulary_cached(
            self.db, workspace_id=workspace_id, context_id=context_id, user_id=user_id
        )
        canonical: dict[str, list[tuple[str, int]]] = {}
        for stored, count in vocabulary.items():
            fold = normalize_tag(stored)
            if fold:
                canonical.setdefault(fold, []).append((stored, count))
        for spellings in canonical.values():
            # Stable: equally frequent spellings keep the vocabulary's order.
            spellings.sort(key=lambda pair: -pair[1])
        return canonical

    async def _normalize_tags_for_write(
        self,
        tags: list[str],
        *,
        workspace_id: UUID,
        context_id: UUID,
        user_id: str,
        canonical: dict[str, list[tuple[str, int]]] | None = None,
    ) -> tuple[list[str], list[WriteLintHint]]:
        """Map each tag onto the context's established spelling of it (#1853).

        Two tags are the same tag written differently when
        ``is_mechanical_variant`` holds: ``normalize_tag`` folds them to one
        form (NFKC, case, hiragana to katakana, separators, one word-final ``ー``
        after a katakana letter, a conservative plural strip) AND their
        numbers are the same (#1871). The fold alone is the rule recall's
        ``tags_normalize`` filter widens by, but it is not injective on
        numbered tags — ``v0.11.0`` / ``v0.1.10``, ``pr-123`` / ``pr-1-23`` —
        and a write, unlike a filter, would file the memory under the other
        identifier. Such a tag is stored as written, with no hint.
        Abbreviations and edit-distance variants are NOT mapped either; they
        stay ``tag_near_duplicate`` hints. Among several stored variants the
        most frequent wins. The vocabulary is the #1512 cache.
        """
        from utils.tag_normalize import is_mechanical_variant, normalize_tag

        if canonical is None:
            canonical = await self.tag_canonical_map(
                workspace_id=workspace_id, context_id=context_id, user_id=user_id
            )
        out: list[str] = []
        hints: list[WriteLintHint] = []
        for tag in tags:
            fold = normalize_tag(tag)
            spellings = canonical.setdefault(fold, []) if fold else []
            # Most frequent first; spellings stored earlier in this request or
            # batch follow with count 0.
            match = next(
                (pair for pair in spellings if is_mechanical_variant(tag, pair[0])),
                None,
            )
            if match is None or match[0] == tag:
                stored = tag
            else:
                stored, count = match
                hints.append(
                    WriteLintHint(
                        code="tag_normalized",
                        hint=(
                            f"tag '{tag}' was stored as '{stored}' ({count} memories), "
                            "the established spelling in this context."
                            if count
                            else f"tag '{tag}' was stored as '{stored}', the spelling used "
                            "earlier in this request."
                        ),
                        subject=tag,
                        replacement=stored,
                    )
                )
            if stored not in out:
                out.append(stored)
            # Within one request the first spelling wins too ('Foo' then 'foo');
            # a tag with other numbers joins the fold as a spelling of its own.
            if fold and match is None:
                spellings.append((stored, 0))
        return out, hints

    async def _find_duplicate_candidate(
        self,
        *,
        user_id: str,
        workspace_id_str: str,
        context_id_str: str,
        summary: str,
    ) -> dict[str, Any] | None:
        """The nearest live memory when it is above the supersede-suggestion
        threshold, else None (#1853 ``dedupe="check"``).

        Same embedder, collection and threshold as the post-embed detection
        in ``_create_knn_seed_edges``. The scope is what ``recall`` by the
        same caller searches (#1873, as ``SearchService`` derives it): every
        member's memories in a shared context; in a private one the caller's
        own and those of the accounts linked to it (#1784). A candidate
        another member wrote carries ``supersedable: False``: the caller can
        read it but ``supersedes`` only takes the caller's own memories
        (#1803), so that resolution is not offered for it. An embedder that
        cannot run (no key, spend cap, outage) is
        :class:`DedupeUnavailableError`: the caller decides, nothing is
        written silently.
        """
        from db.qdrant import search_memories_qdrant
        from services.context_routing import resolve_context_routing
        from services.embedding_service import EmbeddingService
        from utils.text import normalize_for_search

        is_shared_context = await self.context_service.is_context_shared(UUID(context_id_str))
        linked = await link_set_reads(self.db, user_id, not is_shared_context)
        owner_ids = sorted(linked) if linked and len(linked) > 1 else None
        try:
            collection, embed_svc = await resolve_context_routing(
                self.db, UUID(context_id_str), default_service=EmbeddingService(self.db)
            )
            vector = await embed_svc.embed(
                normalize_for_search(summary),
                user_id,
                context_id=UUID(context_id_str),
                workspace_id=UUID(workspace_id_str),
            )
            hits = await search_memories_qdrant(
                user_id=user_id,
                query_vector=vector,
                workspace_id=workspace_id_str,
                context_id=context_id_str,
                limit=_DEDUPE_CHECK_K,
                collection_name=collection,
                is_shared_context=is_shared_context,
                owner_ids=owner_ids,
            )
        except Exception as exc:
            raise DedupeUnavailableError(str(exc)) from exc
        from services.agent_binding_service import filter_memory_rows_by_binding

        # Nearest first; a point whose row is gone (soft-deleted, the sweep
        # not yet run) or that the agent binding denies is skipped for the
        # next hit above the threshold, not taken as "no duplicate".
        for hit in sorted(hits, key=lambda c: float(c["score"]), reverse=True):
            score = float(hit["score"])
            if score < _SUPERSEDE_SUGGEST_THRESHOLD:
                return None
            row = (
                await self.db.execute(
                    select(Memory).where(
                        Memory.id == UUID(str(hit["id"])), Memory.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                continue
            # #1299: an agent-bound credential denied this memory's type/source
            # must not learn its summary (or that it exists) through a write tool.
            kept, _denied = await filter_memory_rows_by_binding(
                self.db, [row], operation="remember", user_id=user_id
            )
            if not kept:
                continue
            candidate: dict[str, Any] = {
                "memory_id": str(row.id),
                "summary": row.summary,
                "similarity": round(score, 4),
            }
            # The ownership rule of ``_create_declared_links`` (#1803): the
            # caller's own memory, or in a private context a linked account's.
            own = row.user_id == user_id or (
                linked is not None
                and row.user_id in linked
                and await self._is_private_context(context_id_str)
            )
            if not own:
                candidate["supersedable"] = False
            return candidate
        return None

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
        # #1941: the capacity check runs after authorization — in
        # _update_in_place, and in the inner remember() of an upsert.
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
        from utils.text import normalize_for_search

        memory = await self._update_load_authorized(request.memory_id, user_id)
        # #1941: after the access check, so the lock is no existence oracle.
        await self._ensure_capacity(
            user_id, workspace_id=memory.workspace_id, write_context_id=memory.context_id
        )
        # #2043: re-read under a row lock, then compare the version — before any
        # field is applied, so a refused edit writes nothing.
        memory = await self._lock_for_edit(memory.id)
        check_expected_version(memory, request.expected_version)

        # Tool guardrails: any edit of a guardrail row, or a details write that
        # adds / changes / removes tool_trigger, needs context EDITOR or above
        # (membership alone is what can_access_memory checked). Before any
        # field is applied, so a deny leaves the row untouched.
        if self._touches_tool_trigger(
            memory.details, request.details, details_supplied=request.details is not None
        ):
            await self._require_guardrail_author(user_id, memory.context_id)

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

        # #1504/#1505: a dismissal-only call edits no content. Capture the
        # timestamp before _update_apply_fields stamps it unconditionally.
        updated_at_before = memory.updated_at
        edits_requested = any(
            getattr(request, field) is not None for field in _UPDATE_CONTENT_FIELDS
        )

        # #2059: whether a stored parallel verdict still hides the pair is a
        # question about what the caller could have READ — the texts before
        # this call's own edit — so it is answered before the fields apply.
        # A call that also re-embeds always tombstones: its own edit sets the
        # verdict aside and the re-embed re-judges the pair, so leaving the
        # rejected pair untombstoned would let it surface moments later.
        parallel_current = (
            await self._parallel_verdict_current(memory)
            if request.dismiss_supersede_candidate and not needs_reembed
            else False
        )

        self._update_guard_size(memory, request, normalized_summary, normalized_ctx_summary)
        before = versioned_snapshot(memory)
        effective_details = self._update_apply_fields(
            memory, request, normalized_summary, normalized_ctx_summary
        )
        # #1504: rejection path. Applied before the commit below so the tombstone
        # lands in the same transaction as any other field change in this call.
        # A dismissal is not a content edit, so when it is the ONLY thing this
        # call did, restore the timestamp _update_apply_fields unconditionally
        # stamped: updated_at is the documented staleness cue ("an old value
        # means the fact may be stale"), and rejecting a suggestion must not make
        # a stale memory read as freshly maintained (#1317's rule for the
        # detection half of this lifecycle).
        dismissed_target = self._apply_supersede_dismissal(
            memory, request, drop_baseline=needs_reembed, parallel_current=parallel_current
        )

        if request.dismiss_supersede_candidate and not edits_requested:
            memory.updated_at = updated_at_before

        # #2043: a changed versioned field is a new version and one revision row,
        # in this transaction (the dismissal / delivery_mode-only / no-op edits
        # change none and leave the version alone).
        record_edit(self.db, memory, before, actor_user_id=user_id)

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
            await self._update_sync_qdrant_payload(
                memory, request, normalized_ctx_summary, effective_details
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
            version=memory.version,  # #2043
            persistence=persistence_info(  # #1505
                memory.scope,
                pinned=memory.is_pinned,
                tool_triggered=self._carries_tool_trigger(memory.details),
            ),
            supersede_candidate_dismissed=dismissed_target,  # #1504
            # #1502: lint the memory's CURRENT state, not the patch — a partial
            # update leaves fields untouched, and what matters for recall is
            # what the memory now says.
            lint=await self._lint_write(
                workspace_id=memory.workspace_id,
                context_id=memory.context_id,
                user_id=user_id,
                summary=memory.summary,
                tags=memory.tags,
                memory_id=memory.id,
            ),
        )

    async def _parallel_verdict_current(self, memory: Any) -> bool:
        """#2059: whether ``memory``'s stored ``parallel`` verdict still hides it.

        The read path (``_resolve_supersede_candidates``) suppresses a parallel
        pair only while ``verdict_current`` holds for the target's and this
        memory's current texts; once either was edited the pair surfaces,
        unverified. The dismissal asks the same question so a suggestion the
        caller can see is always one it can reject. Reads one row (the
        target's summary and content head), and only for a parallel verdict.

        Returns:
            True only when the verdict is parallel and still current; a target
            that is gone (the pair cannot surface either) reads as False.
        """
        from services.supersede_verify import CONTENT_HEAD_CHARS, is_parallel, verdict_current

        stored = memory.supersede_candidate
        if not is_parallel(stored):
            return False
        try:
            target_id = UUID(str(stored["memory_id"]))
        except (KeyError, ValueError, TypeError):
            return False
        row = (
            await self.db.execute(
                select(
                    Memory.summary,
                    func.substr(Memory.content, 1, CONTENT_HEAD_CHARS).label("content_head"),
                ).where(Memory.id == target_id, Memory.deleted_at.is_(None))
            )
        ).one_or_none()
        if row is None:
            return False
        return verdict_current(
            stored,
            old_summary=row.summary,
            old_content_head=row.content_head,
            new_summary=memory.summary,
            new_content_head=memory.content,
        )

    @staticmethod
    def _apply_supersede_dismissal(
        memory: Any,
        request: UpdateMemoryRequest,
        *,
        drop_baseline: bool = False,
        parallel_current: bool = False,
    ) -> UUID | None:
        """#1504: tombstone the current supersede suggestion, if one is pending.

        Args:
            memory: The loaded update target.
            request: The update request.
            drop_baseline: Set when this same call also re-embeds the memory.
                The tombstone normally records the similarity at dismissal so a
                later MATERIAL change can re-surface the pair; but a re-embed in
                this very call is guaranteed to move that score, which would let
                the detector overwrite the tombstone moments after the response
                reported the dismissal as applied. Storing no baseline suppresses
                the pair unconditionally instead — the caller rejected these two
                memories knowing they were editing one of them.
            parallel_current: The stored verdict is ``parallel`` and still
                about both texts as the caller could read them
                (``_parallel_verdict_current``), so the pair was never shown.
                Defaults to False: without that proof an explicit rejection
                wins and the pair is tombstoned (#2059).

        Returns:
            The dismissed candidate's memory id, or None when the flag was not
            set or there was no live suggestion to reject. A no-op dismissal is
            NOT an error: the suggestion may have self-healed (target deleted)
            or been accepted between the read that showed it and this call, and
            failing the whole update over that would be hostile.
        """
        if not request.dismiss_supersede_candidate:
            return None

        from services.supersede_dismissal import build_tombstone, dismissed_entry
        from services.supersede_verify import is_parallel

        stored = memory.supersede_candidate
        if not isinstance(stored, dict) or not stored.get("memory_id"):
            # Already a tombstone, or nothing pending.
            if dismissed_entry(stored) is None:
                logger.info("supersede_dismiss_noop", memory_id=str(memory.id))
            return None
        if parallel_current and is_parallel(stored):
            # #2016: the judge set this pair aside, the verdict still describes
            # both texts, so the caller was never shown it — nothing to reject,
            # and the verdict stays so the pair is not re-judged. A STALE
            # parallel verdict surfaces on read (#2059) and is tombstoned below.
            logger.info("supersede_dismiss_noop", memory_id=str(memory.id), reason="parallel")
            return None

        target_id = str(stored["memory_id"])
        try:
            dismissed_id = UUID(target_id)
        except (ValueError, TypeError):
            # The column is server-written, but the sibling read path
            # (_resolve_supersede_candidates) already treats a malformed stored
            # value as reachable. A corrupt ADVISORY column must not fail the
            # caller's real edit, so drop the dismissal and leave the row alone.
            logger.warning(
                "supersede_dismiss_malformed_candidate",
                memory_id=str(memory.id),
                stored_target=target_id[:100],
            )
            return None
        # Reassign (JSON column, not a MutableDict — reassignment is what marks
        # it dirty), mirroring how detection and acceptance write this column.
        memory.supersede_candidate = build_tombstone(stored, drop_baseline=drop_baseline)
        logger.info(
            "supersede_suggestion_dismissed",
            memory_id=str(memory.id),
            candidate_memory_id=target_id,
            similarity=stored.get("similarity"),
        )
        return dismissed_id

    async def _lock_for_edit(self, memory_id: UUID, *, include_deleted: bool = False) -> Any:
        """Re-read an authorized edit target under ``FOR UPDATE`` (#2043).

        The edit paths authorize on an unlocked read, then lock the row here so
        the ``expected_version`` check, the diff of the versioned fields and the
        version bump all see the latest commit and hold until this edit
        commits. One row, so no lock-order hazard with Sleep's two-row lock.

        Args:
            memory_id: The target the caller was authorized for.
            include_deleted: PATCH reads tombstones (its 410 contract).

        Raises:
            NotFoundException: the row was hard-deleted, or (without
                ``include_deleted``) forgotten, since the authorized read.
        """
        memory = await self.memory_repo.get(
            memory_id, include_deleted=include_deleted, for_update=True
        )
        if not memory:
            raise NotFoundException("Memory", str(memory_id))
        return memory

    async def _update_load_authorized(self, memory_id: UUID, user_id: str) -> Any:
        """Load the update target, or 404 (MCP-reachable in-place update path).

        Unlike the PATCH sibling this does NOT distinguish tombstones: #1316
        keeps ``repo.get()`` on its soft-delete-excluding default here, so a
        deleted memory is uniformly "not found". Only #439's PATCH contract
        needs the 410.

        Raises:
            NotFoundException: no such memory, or the caller may not write it.
        """
        from services.permission_service import (
            CallerId,
            MemoryAuthorId,
            PermissionService,
        )

        memory = await self.memory_repo.get(memory_id)
        if not memory:
            raise NotFoundException("Memory", str(memory_id))

        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(memory.user_id),
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
            access="write",  # #1275: WRITE path — can_read-only binding must not permit mutation
            operation="update",  # #1286 (P0-5): deny-capture audit identity
            memory_id=memory_id,
            # #1301: id-addressed update of a read-denied row must be a
            # uniform 404 (reference/forget doctrine) — REST PATCH and this
            # MCP-reachable path stay in parity (#1291/#1292).
            memory_type=memory.type,
            memory_source_type=memory.source_type,
        )
        if not can_access:
            raise NotFoundException("Memory", str(memory_id))

        return memory

    @staticmethod
    def _update_guard_size(
        memory: Any,
        request: UpdateMemoryRequest,
        normalized_summary: str | None,
        normalized_ctx_summary: str | None,
    ) -> None:
        """Reject an update that would push the row past ``MAX_CONTENT_SIZE``.

        Sizes the row as it will be AFTER the update, using explicit ``is None``
        rather than truthy fallbacks (#1458). On this surface ``None`` means
        "leave this field alone", but ``""`` and ``{}`` are values a client can
        legitimately send — ``context_summary`` has no ``min_length`` and
        ``details`` is a free dict — and :meth:`_update_apply_fields` does write
        them. Under the old ``or`` fallbacks those fell through to the STORED
        value, so an update that CLEARED a large ``context_summary`` was
        measured as though it had not, and could be rejected for being too big
        while actually shrinking the row.

        Takes the normalized summaries rather than the raw request fields so the
        measurement matches what actually lands on the row. (Both are bounded by
        ``max_length`` 500 / 2000, so the difference cannot move a 1 MB verdict —
        it is for correctness, not headroom.) Mirrors ``_patch_guard_size``.

        Raises:
            QuotaExceededError: the post-update size exceeds the limit.
        """
        from config.constants import MAX_CONTENT_SIZE

        next_summary = normalized_summary if normalized_summary is not None else memory.summary
        next_ctx_summary = (
            normalized_ctx_summary if normalized_ctx_summary is not None else memory.context_summary
        )
        next_content = request.content if request.content is not None else memory.content
        next_details = request.details if request.details is not None else memory.details

        content_size = (
            len(next_summary if next_summary is not None else "")
            + len(next_ctx_summary if next_ctx_summary is not None else "")
            + len(next_content if next_content is not None else "")
            + len(str(next_details) if next_details is not None else "")
        )
        if content_size > MAX_CONTENT_SIZE:
            from utils.exceptions import ContentSizeExceededError

            raise ContentSizeExceededError(actual=content_size, limit=MAX_CONTENT_SIZE)

    def _update_apply_fields(
        self,
        memory: Any,
        request: UpdateMemoryRequest,
        normalized_summary: str | None,
        normalized_ctx_summary: str | None,
    ) -> Any:
        """Write the updated values onto the ORM row; return effective details.

        Only non-``None`` request fields are applied — ``None`` means "leave
        alone" on this surface. ``effective_details`` is returned because the
        Qdrant geo payload needs the post-normalization document.
        """
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
            # Tool guardrails: same caller-supplied-only rule (the role gate
            # ran in _update_in_place before any field was touched).
            effective_details = self._apply_tool_trigger(effective_details)
        self._reject_context_location(request.context)
        if request.details is not None or effective_type == MEMORY_TYPE_TIME:
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
        return effective_details

    async def _update_sync_qdrant_payload(
        self,
        memory: Any,
        request: UpdateMemoryRequest,
        normalized_ctx_summary: str | None,
        effective_details: Any,
    ) -> None:
        """Patch the Qdrant payload for a metadata-only update (no re-embed).

        Unlike the PATCH sibling this does NOT swallow Qdrant errors: the caller
        commits AFTER this returns, so a raise here leaves PG uncommitted and
        the two stores consistent. That ordering is the difference and is left
        intact.
        """
        payload_updates, delete_keys = metadata_payload_patch(
            request, normalized_ctx_summary, effective_details
        )
        if not (payload_updates or delete_keys):
            return

        # Sync updated_at to Qdrant for date range filtering (Issue #78)
        payload_updates["updated_at"] = to_utc_iso(utcnow())
        await self._sync_metadata_payload(memory, payload_updates, delete_keys)

    async def _sync_metadata_payload(
        self,
        memory: Any,
        payload_updates: dict[str, Any],
        delete_keys: list[str],
    ) -> None:
        """Merge a metadata-only edit into ``memory``'s own point (#2100).

        ``metadata_patch_target`` decides the point and the keys; a point
        another live row names too is left as it is.
        """
        target = await metadata_patch_target(
            self.db, memory.id, memory.point_id, payload_updates, delete_keys
        )
        if target is None:
            return
        collection = await resolve_collection_name(self.db, memory.context_id)
        await update_memory_payload_in_qdrant(
            memory_id=target.point_id,
            payload_updates=target.updates,
            collection_name=collection,
            delete_keys=target.delete_keys or None,
            # #2015: a context that never opted in has no chunk points.
            cascade_chunks=target.own_point
            and await chunk_cascade_needed(self.db, memory.context_id),
        )

    async def _patch_load_authorized(self, memory_id: UUID, user_id: str) -> Any:
        """Load the PATCH target, or raise the right not-available error (#439).

        The ORDER of the three gates is the security contract and must not be
        rearranged: a non-member has to be unable to tell a soft-deleted memory
        (would-be 410) from a UUID that never existed (404). Returning the
        tombstone first would let anyone who guesses or harvests a UUID confirm
        "this memory was once real" — a GDPR-shaped leak. 410 is reserved for
        callers who already passed the permission check.

        Raises:
            NotFoundException: no such memory, or the caller may not write it.
            MemoryGoneError: the caller is authorized and the memory is a
                tombstone (the #439 contract the route turns into a 410).
        """
        from services.permission_service import (
            CallerId,
            MemoryAuthorId,
            PermissionService,
        )

        # #1316: patch is the ONE by-id path that needs tombstones.
        memory = await self.memory_repo.get(memory_id, include_deleted=True)
        if not memory:
            raise NotFoundException("Memory", str(memory_id))

        perm_service = PermissionService(self.db)
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(memory.user_id),
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
            access="write",  # #1275: WRITE path — can_read-only binding must not permit mutation
            operation="update",  # #1286 (P0-5): deny-capture audit identity
            memory_id=memory_id,
            # #1301: the PATCH response echoes full L3 content — a no-op PATCH
            # on a read-denied row would read it back. Threading type/source
            # makes the id-addressed op uniformly 404 on denied rows, the same
            # doctrine as reference/forget (#1299).
            memory_type=memory.type,
            memory_source_type=memory.source_type,
        )
        if not can_access:
            raise NotFoundException("Memory", str(memory_id))

        if memory.deleted_at is not None:
            raise MemoryGoneError("Memory", str(memory_id))

        return memory

    @staticmethod
    def _patch_guard_size(
        memory: Any,
        request: PatchMemoryRequest,
        provided_fields: set[str],
        normalized_summary: str | None,
    ) -> None:
        """Reject a patch that would push the row past ``MAX_CONTENT_SIZE``.

        Skipped entirely on metadata-only patches: ``tags`` / ``importance`` /
        ``type`` cannot move the row across the byte limit, so the four
        ``len()`` calls would be pure waste on the most common PATCH shape.

        Raises:
            QuotaExceededError: the post-patch size exceeds the limit.
        """
        if not ({"summary", "content", "details"} & provided_fields):
            return

        from config.constants import MAX_CONTENT_SIZE

        # Compute the post-patch size from the would-be values. Use explicit
        # `is None` rather than truthy fallback so empty-but-provided values
        # like `details = {}` count as themselves (`len("{}") = 2`) instead of
        # being collapsed to 0.
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
            from utils.exceptions import ContentSizeExceededError

            raise ContentSizeExceededError(actual=content_size, limit=MAX_CONTENT_SIZE)

    def _patch_apply_fields(
        self,
        memory: Any,
        request: PatchMemoryRequest,
        provided_fields: set[str],
        normalized_summary: str | None,
    ) -> Any:
        """Write the patched values onto the ORM row; return effective details.

        The caller needs ``effective_details`` afterwards for the Qdrant geo
        payload, so it is returned rather than only assigned — that value is the
        post-normalization document, which is not always what lands on
        ``memory.details`` (a non-``details`` patch on a non-time memory leaves
        the column untouched).
        """
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
        # No context guard here: PatchMemoryRequest has no context field.
        if "details" in provided_fields:
            effective_details = self._apply_location(effective_details)
            # Tool guardrails: same caller-supplied-only rule (the role gate
            # ran in patch_memory before any field was touched).
            effective_details = self._apply_tool_trigger(effective_details)
        if "details" in provided_fields or effective_type == MEMORY_TYPE_TIME:
            # Explicit null clears the column; non-null replaces it. A
            # type="time" patch always (re)writes the normalized details.
            memory.details = effective_details

        memory.updated_at = utcnow()
        return effective_details

    async def _patch_invalidate_edges(
        self,
        memory: Any,
        user_id: str,
        memory_workspace_id: str | None,
        memory_context_id: str | None,
    ) -> None:
        """Drop neural edges anchored on a re-embedded memory (#439).

        Edge weights were computed against the previous embedding; the next
        sleep run rebuilds them against the new vector. A NULL workspace/context
        skips invalidation but warns, so operators can spot pre-Migration-063
        rows that would otherwise drift silently.

        ``NeuralEdgeRepository.delete_node_edges`` issues the DELETE through
        ``self.db.execute(...)`` but does NOT commit internally — without the
        explicit commit here the DELETE would never persist (the memory commit
        already closed the prior transaction, so this one lands in a fresh
        implicit transaction that is discarded on session close). Best-effort:
        commit on success, roll back on failure so a partial DELETE cannot leak
        into the next statement on this session.
        """
        if not (memory_workspace_id and memory_context_id):
            logger.warning(
                "memory_patch_edges_invalidation_skipped",
                memory_id=str(memory.id),
                user_id=user_id,
                reason="missing_workspace_or_context_id",
            )
            return

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

    async def _patch_sync_qdrant_payload(
        self,
        memory: Any,
        request: PatchMemoryRequest,
        provided_fields: set[str],
        effective_details: Any,
        user_id: str,
    ) -> None:
        """Push a metadata-only patch into the Qdrant payload (#439).

        Runs AFTER the PG commit and only logs on failure: the PG write stays
        durable and the mismatch is visible, rather than rolling back a
        successful patch because a secondary store hiccuped.
        """
        payload_updates: dict[str, object] = {}
        delete_keys: list[str] = []
        if "tags" in provided_fields:
            payload_updates["tags"] = request.tags
        # Mirror the PG-side None-guard: explicit-null for NOT-NULL columns
        # (importance/type) is silently skipped on both sides to keep
        # PG ↔ Qdrant payloads consistent.
        if "importance" in provided_fields and request.importance is not None:
            payload_updates["importance"] = request.importance
        if "type" in provided_fields and request.type is not None:
            payload_updates["type"] = request.type
        # WHERE axis (#1332): a details patch changes the geo payload —
        # sync or clear so filters.near never matches a stale coordinate.
        if "details" in provided_fields:
            loc = location_payload_from_details(effective_details)
            if loc is not None:
                payload_updates["location"] = loc
            else:
                delete_keys.append("location")

        if not (payload_updates or delete_keys):
            return

        payload_updates["updated_at"] = to_utc_iso(utcnow())
        try:
            await self._sync_metadata_payload(memory, payload_updates, delete_keys)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "memory_patch_qdrant_payload_failed",
                memory_id=str(memory.id),
                user_id=user_id,
                error=str(exc),
                exc_info=exc,
            )

    async def _patch_build_response(self, memory: Any, user_id: str) -> ReferenceResponse:
        """Build the PATCH response from the in-scope ORM row (#439).

        Deliberately NOT ``self.reference(...)``: that would re-fetch the row,
        re-run the permission check, bump access-stats (a write — wrong for
        PATCH) and issue a second commit. The declared_link refs still come from
        ``_fetch_declared_link_refs``, which has none of those side effects.
        A row without a context_id skips the link fetch and returns empty link
        arrays (the helper's signature requires non-null FKs; workspace_id is
        NOT NULL since e95, #1951).
        """
        if memory.context_id:
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

        # #1403: surface the liveness-guarded suggestion here too. It lives in its
        # own column, so replacing `details` on this patch never drops it, and the
        # client sees the current suggestion in the patch response. operation="update"
        # matches the PATCH surface's MAE vocabulary so a shadow-mode binding
        # would_deny is attributed to the right operation (not "reference").
        supersede_candidate = (
            await self._resolve_supersede_candidates(
                {memory.id: memory}, user_id=user_id, operation="update"
            )
        ).get(memory.id)

        return ReferenceResponse(
            memory_id=memory.id,
            summary=memory.summary,
            context_summary=memory.context_summary,
            content=memory.content,
            details=memory.details,
            supersede_candidate=supersede_candidate,
            type=memory.type,
            scope=memory.scope,
            importance=memory.importance,
            tags=memory.tags or [],
            context=memory.context,
            created_at=memory.created_at,
            updated_at=memory.updated_at or memory.created_at,
            version=memory.version,  # #2043
            client=memory.client,
            source_uri=memory.source_uri,
            source_type=memory.source_type,
            outgoing_links=outgoing_links,
            outgoing_has_more=outgoing_has_more,
            incoming_links=incoming_links,
            incoming_has_more=incoming_has_more,
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
        from utils.text import normalize_for_search

        memory = await self._patch_load_authorized(memory_id, user_id)
        # #1941: after the access check, so the lock is no existence oracle.
        await self._ensure_capacity(
            user_id, workspace_id=memory.workspace_id, write_context_id=memory.context_id
        )
        # #2043: lock, then the version check — before any field is applied.
        # A forget that committed since the authorized read is still a 410.
        memory = await self._lock_for_edit(memory.id, include_deleted=True)
        if memory.deleted_at is not None:
            raise MemoryGoneError("Memory", str(memory_id))
        check_expected_version(memory, request.expected_version)

        # `model_fields_set` is the set of field names the client EXPLICITLY
        # sent (including those set to None), so `{"details": null}` puts
        # "details" in the set and a body that simply omits `details` does
        # not. Cheaper than `model_dump(exclude_unset=True)` for large
        # `details` payloads — no deep serialization, just a name set.
        provided_fields = request.model_fields_set

        # Tool guardrails: mirrors _update_in_place — any patch of a guardrail
        # row (including ``{"details": null}``, which drops the marking) or a
        # details patch naming tool_trigger needs context EDITOR or above.
        if self._touches_tool_trigger(
            memory.details, request.details, details_supplied="details" in provided_fields
        ):
            await self._require_guardrail_author(user_id, memory.context_id)

        normalized_summary = (
            normalize_for_search(request.summary) if "summary" in provided_fields else None
        )

        needs_reembed = False
        if normalized_summary is not None and normalized_summary != memory.summary:
            needs_reembed = True
        if "content" in provided_fields and request.content != memory.content:
            needs_reembed = True

        self._patch_guard_size(memory, request, provided_fields, normalized_summary)
        before = versioned_snapshot(memory)
        effective_details = self._patch_apply_fields(
            memory, request, provided_fields, normalized_summary
        )
        record_edit(self.db, memory, before, actor_user_id=user_id)  # #2043

        memory_workspace_id = str(memory.workspace_id)
        memory_context_id = str(memory.context_id) if memory.context_id else None

        if needs_reembed:
            memory.embedding_status = "pending"
            await self.db.flush()
            await self.db.commit()

            task = asyncio.create_task(process_pending_embedding(memory.id))
            task.add_done_callback(
                functools.partial(_log_embedding_task_result, memory_id=str(memory.id))
            )

            await self._patch_invalidate_edges(
                memory, user_id, memory_workspace_id, memory_context_id
            )
        else:
            # Metadata-only path: PG commit first, qdrant after. Qdrant
            # failure is logged (not raised) for drift visibility — the PG
            # write stays durable. Forget uses single-commit-at-end; this
            # deviation is by Issue #439's design.
            await self.db.flush()
            await self.db.commit()

            await self._patch_sync_qdrant_payload(
                memory, request, provided_fields, effective_details, user_id
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

        return await self._patch_build_response(memory, user_id)

    async def _upsert_by_external_id(
        self,
        request: UpdateMemoryRequest,
        user_id: str,
        client: str = "unknown",
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
    ) -> UpdateMemoryResponse:
        """Upsert by external_id: remember new, then forget old if exists.

        #1803: in a private context the external_id names a row of the owner,
        and the owner is the link set (#1784) — so either linked account's
        upsert replaces the same row instead of each keeping its own. A shared
        context keeps matching the caller's own rows only. Every matching row
        is replaced, not only the newest: accounts linked before #1803 may
        each hold one, and one left behind would never be matched again.
        """
        matches = await self.memory_repo.list_by_resource_id(
            resource_id=request.external_id,
            context_id=current_context_id,
            user_id=user_id,
            include_linked=await self._is_private_context(current_context_id),
        )
        existing = matches[0] if matches else None

        # Build details with resource_id preserved (copy to avoid mutating request)
        details = {**(request.details or {}), "resource_id": request.external_id}

        # Tool guardrails: replacing a guardrail row is a protection change
        # (the replacement may drop the marking). Gate it here, loudly, rather
        # than letting the inner forget() skip the old row silently and report
        # "replaced" with two live rows. The inner remember() gates a NEW
        # marking on its own.
        if any(
            self._touches_tool_trigger(match.details, None, details_supplied=False)
            for match in matches
        ):
            await self._require_guardrail_author(user_id, matches[0].context_id)

        # #1519: forward the caller's pin — without it a pinned external_id row
        # was replaced by an unpinned one and left load_pinned() silently.
        # UpdateMemoryRequest's None means "unchanged", so on a replacement the
        # row being replaced supplies the value; only a brand-new external_id
        # (no existing row) falls through to remember()'s default.
        delivery_mode = request.delivery_mode
        if delivery_mode is None and existing is not None:
            delivery_mode = existing.delivery_mode

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
            **({"delivery_mode": delivery_mode} if delivery_mode is not None else {}),
        )

        # #1286 (P0-5) audit note: the upsert path intentionally emits NO
        # operation="update" event — the inner remember()/forget() calls each
        # emit their own row, which describes exactly what happened
        # physically (a new row created, the old row soft-deleted).
        #
        # #1549: a REPLACEMENT is an update to the caller (the memory count
        # does not grow), so only a brand-new external_id is charged against
        # the daily memory-creation quota.
        result = await self.remember(
            remember_request,
            user_id=user_id,
            client=client,
            current_context_id=current_context_id,
            current_workspace_id=current_workspace_id,
            _skip_daily_quota=existing is not None,
        )

        # Only forget old memory after new one is successfully created.
        # #1301: this delete is system maintenance (replacing the row the
        # caller addressed by external_id), not an agent read — the #1299
        # per-memory read filter must not silently no-op it, or a denied-type
        # existing row survives as a live duplicate alongside the new row.
        #
        # #1803: ``existing`` may be a linked account's row in a private
        # context. forget() still authorizes it through can_access_memory
        # (same owner + context EDITOR), but the skipped row filter means an
        # agent binding's type filter does not stop the replacement — as it
        # never did for the caller's own row: the link makes it the same owner.
        operation = "created"
        for match in matches:
            await self.forget(
                ForgetRequest(memory_id=match.id),
                user_id=user_id,
                current_context_id=current_context_id,
                _skip_binding_row_filter=True,
            )
            operation = "replaced"

        return UpdateMemoryResponse(
            memory_id=result.memory_id,
            operation=operation,
            re_embedded=True,
            scope=result.scope,
            version=result.version,  # #2043: the new row's version (1)
            # #1505/#1519: remember() already built the block from the row it
            # actually wrote (pinned flag included) — reuse it like lint below.
            persistence=result.persistence,
            # #1502: the upsert delegates to remember(), which already linted the
            # same summary/tags — carry that through rather than re-reading the
            # vocabulary a second time for one write.
            lint=result.lint,
        )

    async def _load_readable_memory(self, memory_id: UUID, user_id: str) -> Memory:
        """The live memory ``user_id`` may read, the gate of ``reference`` and
        ``list_revisions`` (#2043): a tombstone, a missing row and a denied
        read are the same ``NotFoundException``; the capacity lock is checked
        after the access check, so it is no existence oracle (#1941).
        """
        # #1316: repo.get() excludes soft-deleted rows by default — a forgotten
        # memory must not be readable by direct-id fetch during the retention
        # window, and a tombstone is indistinguishable from never-existed.
        memory = await self.memory_repo.get(memory_id)

        if not memory:
            raise NotFoundException("Memory", str(memory_id))

        if not await self._can_read_memory(
            memory, user_id, memory_id=memory_id, memory_type=memory.type
        ):
            raise NotFoundException("Memory", str(memory_id))

        # #1941: after the access check, so the lock is no existence oracle.
        await self._ensure_capacity(user_id, workspace_id=memory.workspace_id)
        return memory

    async def _can_read_memory(
        self, memory: Memory, user_id: str, *, memory_id: UUID, memory_type: str
    ) -> bool:
        """The read gate of ``reference`` and ``list_revisions``, with the
        binding's per-memory filter (#1299) judged on ``memory_type``: the
        live row's, or an earlier version's for ``reference(version=N)``
        (#2116) — one chokepoint, so the two cannot drift apart."""
        # Issue #XXX: Team collaboration - verify access permission
        from services.permission_service import CallerId, MemoryAuthorId, PermissionService

        return await PermissionService(self.db).can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(memory.user_id),
            workspace_id=memory.workspace_id,
            context_id=memory.context_id,
            # #1286 (P0-5): deny-capture audit identity. list_revisions reads
            # the same memory's metadata, so it audits as a reference read
            # (memory_access_events.operation has no value of its own for it).
            operation="reference",
            memory_id=memory_id,
            memory_type=memory_type,  # #1299: per-memory type/source filter
            memory_source_type=memory.source_type,
        )

    async def list_revisions(
        self,
        memory_id: UUID,
        user_id: str,
        *,
        before_id: int | None = None,
        limit: int = 20,
        context_id: UUID | None = None,
    ) -> MemoryRevisionsResponse:
        """One page of a memory's revision log, newest first (#2043).

        Same gate as ``reference`` (a forgotten memory answers not-found, so
        its log is unreadable too). Records no access stats: the log is
        metadata, not an adoption of the memory.

        Args:
            memory_id: Memory UUID.
            user_id: The caller.
            before_id: Keyset cursor — the ``id`` of the last row of the
                previous page; None for the newest page.
            limit: Rows per page.
            context_id: The context the caller named (MCP); a memory of
                another context is not found, so the envelope's context
                fields always describe the memory's own context.

        Raises:
            NotFoundException: No such live memory, no access, or not in
                ``context_id``.
        """
        from services.identity_link_service import linked_user_ids
        from services.memory_versions import list_revision_rows, revision_item

        memory = await self._load_readable_memory(memory_id, user_id)
        if context_id is not None and memory.context_id != context_id:
            raise NotFoundException("Memory", str(memory_id))
        rows, has_more = await list_revision_rows(
            self.db, memory.id, before_id=before_id, limit=limit
        )
        caller_ids = await linked_user_ids(self.db, user_id)
        unreadable = await self._unreadable_edits(memory, user_id, rows)
        revisions = []
        for row in rows:
            readable = row.version not in unreadable
            item = MemoryRevisionItem(
                **revision_item(row, caller_ids=caller_ids, readable=readable)
            )
            item._cursor = str(row.id)
            revisions.append(item)
        return MemoryRevisionsResponse(
            memory_id=memory.id,
            current_version=memory.version,
            revisions=revisions,
            has_more=has_more,
            next_cursor=str(rows[-1].id) if has_more and rows else None,
        )

    async def _unreadable_edits(self, memory: Memory, user_id: str, rows: list[Any]) -> set[int]:
        """The versions of the ``updated`` events in ``rows`` that connect a
        version the caller may not read (#2169).

        An ``updated`` event at version K is the diff from K-1 to K, so it
        names its changed fields only when ``reference`` would serve both
        versions: each version's type passes the read gate under the #2116
        rule (the live type keeps the live verdict, an unavailable type is
        denied). The gate runs once per distinct historical type — never per
        event — so a denial writes one audit row, and none runs when no edit
        in reach changed the type.
        """
        from services.memory_versions import type_changes_above, types_at_versions

        edits = [row.version for row in rows if row.kind == REVISION_KIND_UPDATED]
        if not edits:
            return set()
        changes = await type_changes_above(self.db, memory.id, min(edits))
        if not changes:
            return set()
        # A type change above the version read means the live row changed
        # after it was read: from the newest such change up the type is
        # unknown, so those versions read as unavailable (denied).
        live_type = None if any(v > memory.version for v, _ in changes) else memory.type
        types = types_at_versions(live_type, changes, {v for k in edits for v in (k - 1, k)})
        verdicts: dict[str | None, bool] = {memory.type: True, None: False}

        async def readable(memory_type: str | None) -> bool:
            if memory_type not in verdicts:
                verdicts[memory_type] = await self._can_read_memory(
                    memory, user_id, memory_id=memory.id, memory_type=memory_type
                )
            return verdicts[memory_type]

        return {k for k in edits if not (await readable(types[k - 1]) and await readable(types[k]))}

    async def reference(
        self,
        memory_id: UUID,
        user_id: str,
        *,
        record_access: bool = True,
        version: int | None = None,
    ) -> ReferenceResponse:
        """Get full memory details (Layer 3).

        Args:
            memory_id: Memory UUID
            user_id: User ID (for access control)
            record_access: Record the read in the memory's access stats
                (access_count, the #1046 adoption signal reference_count,
                last_used_at). The MCP handler passes False for a
                continuation page (it records a continuation that is the
                session's first read via ``record_adoption``, #2139) and for a
                call that returns no Layer-3 field (#1685). The access check
                and the access audit event run either way.
            version: An earlier version to read (#2043): the versioned fields
                are rebuilt from the revision log, ``historical`` is true and
                the links / supersede suggestion (which describe the memory
                now) are left empty. None or the current version reads the
                memory as it is. A historical read records no access stats.
                An agent binding's per-memory type filter is judged on both
                the live type and version N's (#2116).

        Returns:
            ReferenceResponse with full details

        Raises:
            NotFoundException: If memory not found, not readable, or (#2116)
                ``version``'s type is outside the caller's binding filter
            UnknownVersionError: ``version`` is not a version of the memory
                (a ``ValidationError``: 422 / ``validation_error``).
        """
        memory = await self._load_readable_memory(memory_id, user_id)
        if version is not None and version != memory.version:
            return await self._reference_historical(memory, user_id, version)

        # Snapshot ``updated_at`` before bumping access stats. #1317 removed
        # the column's ``onupdate=func.now()``, so ``update_access_stats`` no
        # longer touches ``updated_at`` at all — the DB value is now correct
        # by construction. The snapshot is retained as a cheap guard against
        # any future in-session dirtying, and reading it here keeps the
        # semantics explicit: an access bump is not a meaningful edit, so the
        # dialog's "Updated At" reflects the last real change, not "now".
        snapshot_updated_at = memory.updated_at or memory.created_at
        snapshot_version = memory.version  # #2043, read with the row (as updated_at)

        # Update access stats. reference() is the canonical *adoption* signal
        # (#1046): the agent deliberately fetched Layer-3 detail, so this bumps
        # reference_count in addition to access_count. Surfacing call sites
        # (recall return, explore spread) below leave count_as_adoption False.
        if record_access:
            await self.memory_repo.update_access_stats(
                memory_id, client="api", count_as_adoption=True
            )
            await self.db.commit()

        await self._audit_reference_read(memory, user_id)

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

        # #1403: resolve the (liveness-guarded, enriched) supersede suggestion for
        # this single memory — the deliberate Layer-3 fetch is exactly where a
        # client decides whether to confirm the supersession.
        supersede_candidate = (
            await self._resolve_supersede_candidates(
                {memory.id: memory}, user_id=user_id, operation="reference"
            )
        ).get(memory.id)

        return ReferenceResponse(
            memory_id=memory.id,
            summary=memory.summary,
            context_summary=memory.context_summary,
            content=memory.content,
            details=memory.details,
            # #1403: liveness-guarded supersede suggestion (its own server-only
            # column — never mixed into the client-writable details blob).
            supersede_candidate=supersede_candidate,
            type=memory.type,
            scope=memory.scope,
            importance=memory.importance,
            tags=memory.tags or [],
            context=memory.context,
            created_at=memory.created_at,
            updated_at=snapshot_updated_at,
            version=snapshot_version,
            client=memory.client,
            source_uri=memory.source_uri,
            source_type=memory.source_type,
            outgoing_links=outgoing_links,
            outgoing_has_more=outgoing_has_more,
            incoming_links=incoming_links,
            incoming_has_more=incoming_has_more,
        )

    async def _audit_reference_read(
        self, memory: Memory, user_id: str, *, version: int | None = None
    ) -> None:
        """Log and audit one reference read (the current or an earlier version)."""
        if version is None:
            logger.info("memory_referenced", memory_id=str(memory.id), user_id=user_id)
        else:
            logger.info(
                "memory_referenced",
                memory_id=str(memory.id),
                user_id=user_id,
                version=version,
                historical=True,
            )

        # #1278/#1281 item 7: audit the Layer-3 adoption read (no-op unless
        # verified agent identity).
        from services.memory_access_event_writer import emit_memory_access_event

        await emit_memory_access_event(
            operation="reference",
            outcome="success",
            workspace_id=memory.workspace_id,
            user_id=user_id,
            context_id=memory.context_id,
            memory_id=memory.id,
        )

    async def _reference_historical(
        self, memory: Memory, user_id: str, version: int
    ) -> ReferenceResponse:
        """``reference(version=N)`` for an earlier version (#2043)."""
        from services.memory_versions import load_version

        # UnknownVersionError (a ValidationError) propagates: 422 / validation_error.
        historical = await load_version(self.db, memory, version)
        # #2116: the live-row gate judged the binding's per-memory type filter
        # (#1299) on today's ``type``. When version N had another type, the
        # same gate judges that one too, with the live-row denial's uniform
        # not-found. ``source_type`` is not versioned, so the live value is
        # the historical one. Same type: the live verdict stands. An
        # unavailable type (never today: erasure marks only ``details``) is
        # denied, as the filter cannot be judged without it.
        historical_type = historical.values["type"]
        if historical_type is None or (
            historical_type != memory.type
            and not await self._can_read_memory(
                memory, user_id, memory_id=memory.id, memory_type=historical_type
            )
        ):
            raise NotFoundException("Memory", str(memory.id))
        await self._audit_reference_read(memory, user_id, version=version)

        values = historical.values
        return ReferenceResponse(
            memory_id=memory.id,
            summary=values["summary"],
            context_summary=values["context_summary"],
            content=values["content"],
            details=values["details"],
            type=values["type"],
            scope=memory.scope,
            importance=values["importance"],
            tags=values["tags"] or [],
            context=memory.context,
            created_at=memory.created_at,
            updated_at=historical.at or memory.created_at,
            version=historical.version,
            historical=True,
            current_version=memory.version,
            unavailable_fields=list(historical.unavailable) or None,
            client=memory.client,
            source_uri=memory.source_uri,
            source_type=memory.source_type,
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

    async def _is_private_context(self, context_id: UUID | str | None) -> bool:
        """Whether ``context_id`` is a private context, the one kind the caller
        reads as a linked owner (#1784).

        Asks for ``is_private`` being true rather than "not shared": a missing,
        deleted or NULL context is treated as private for access, but here a
        True widens the match to the link set, so anything unclear stays on
        the caller's own rows. False without a context.
        """
        if context_id is None:
            return False
        uuid = context_id if isinstance(context_id, UUID) else UUID(str(context_id))
        result = await self.db.execute(
            select(Context.is_private).where(
                Context.id == uuid,
                Context.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none() is True

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
            # #1803: a target must be the caller's own memory — and in a
            # private context the owner is the link set (#1784), so a memory
            # an account linked to the caller wrote there counts too. A shared
            # context keeps the caller's own memories only. Probed once, and
            # only when a lookup actually runs.
            own_target_cache: list[ColumnElement[bool]] = []

            async def own_target() -> ColumnElement[bool]:
                if not own_target_cache:
                    own_target_cache.append(
                        owned_by(Memory.user_id, user_id)
                        if await self._is_private_context(context_id)
                        else Memory.user_id == user_id
                    )
                return own_target_cache[0]

            # Direct links by memory ID (batch-validate targets exist in same scope)
            requested_ids = [t for t in (request.linked_memory_ids or []) if t != memory_id]
            if requested_ids:
                result = await self.db.execute(
                    select(Memory.id).where(
                        Memory.id.in_(requested_ids),
                        await own_target(),
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
                        await own_target(),
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
                        await own_target(),
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
                    # #1403: count declared-supersedes adoption. The supersedes
                    # edge is the dominant stale-suppression lever (F3 factorial:
                    # +0.78, update_success@10 0.76 -> 1.00) and the one mechanism
                    # users must explicitly invoke — emit a dedicated event so its
                    # adoption is visible in telemetry, not buried in the generic
                    # declared_links_created count.
                    logger.info(
                        "declared_supersedes_created",
                        user_id=user_id,
                        memory_id=str(memory_id),
                        superseded_memory_id=str(supersedes_target),
                        context_id=context_id,
                    )
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
            # dict() over a Row sequence infers dict[bytes, bytes]; the rows
            # are (dst_id, src_id) UUID pairs. Cast rather than switching to
            # .tuples() so the executed call is unchanged (#1442).
            shadow_map: dict[UUID, UUID] = cast(
                dict[UUID, UUID],
                dict(rows.all()),  # type: ignore[arg-type]
            )

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

    async def _apply_supersede_fold(
        self,
        search_results: list[dict],
        memories: dict,
        *,
        include_superseded: bool,
        user_id: str,
        trusted_only: bool,
        shadow_map: Mapping[UUID, UUID] | None = None,
        resolved: dict[UUID, SupersedeCandidate | None] | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[UUID, list[UUID]]:
        """#2123: fold judge-verified older versions of a fact under the newer one.

        Shadowing (#1208) removes a memory only when an explicit ``supersedes``
        edge names it. Agents rarely write that edge, so one fact updated a few
        times through ``remember()`` came back once per version. This is the
        same subtraction without an edge, from the strongest signal the store
        has short of one: a ``supersede_candidate`` the deployment's judge
        verified (``verified is True``, which holds only while the verdict is
        about both memories' current texts and at or above the confidence
        floor). A cosine-only candidate never folds: similarity alone does not
        tell an update from a sibling fact.

        1. Only pool memories whose stored candidate carries a ``supersedes``
           verdict are resolved (one batched query; none at all on a
           deployment without the judge). With ``resolved`` (#2131) the
           answer for every judged id — ``None`` where no hint survives — is
           written there, so the page builder resolves only the shipped rows
           the fold did not. An empty answer (nothing survived, or the
           resolver failed open) writes nothing. ``filters`` (#2027) are the
           recall's effective row filters, so a hint the page reuses honours
           them exactly as one it resolves itself.
        2. A verified candidate is a directed link newer → older. A pool row
           reachable from another pool row along links is folded; links are
           followed transitively (v3→v2→v1 folds v2 and v1 under v3), but
           only in the direction a verdict gave: two newer memories that each
           supersede the same older one both stay (no verdict compared them),
           and the older one folds under the newer of the two.
        3. A version shadowing just removed (``shadow_map``, dst → src) still
           links: through its edge and its own verified candidate. Accepting
           v3's suggestion with ``create_edge`` shadows v2, and v1 — linked to
           v2 only — must stay folded under v3 rather than resurface.
        4. Each head (a row no other pool row reaches; the newest by
           ``created_at`` when several reach a folded row) takes the best
           position of its group in the pool and that row's ranking scores, so
           the list stays monotonic and ``confidence`` (computed from the pool)
           matches it. The folded rows leave ``search_results`` in place —
           like shadowing, before the re-rank, the selection plan and the
           top-k slice, so #1306's registered pool is the shipped one (the
           page is as full as the pool allows; with neural off the pool is
           ``k`` and a fold shortens the page, as shadowing does).

        Runs with the reranker on or off alike; only the pool order differs.
        ``include_superseded=True`` folds nothing (audit reads, and
        ``forget(query=)``, must see every version). Rows in a link cycle with
        no head outside it stay unfolded. Fail-open: an error leaves the pool
        untouched. The grouping lives in :mod:`services.supersede_fold`
        (#2131); this shell keeps the resolver call and the fail-open.

        Returns:
            Head id → folded member ids, newest first.
        """
        if include_superseded:
            return {}
        try:
            nodes = fold_nodes(search_results, memories, shadow_map)
            if len(nodes.pool) < 2 or not nodes.judged:
                return {}
            candidates = await self._resolve_supersede_candidates(
                nodes.judged,
                user_id=user_id,
                operation="recall",
                trusted_only=trusted_only,
                filters=filters,
            )
            if resolved is not None and candidates:
                # #2131: every judged id, None where no hint survives, so the
                # page builder skips them all rather than asking again. Only
                # from a non-empty answer: the resolver fails open to {}, and
                # a failed lookup must leave the page to resolve as before.
                for memory_id in nodes.judged:
                    resolved[memory_id] = candidates.get(memory_id)
            links = fold_links(nodes, candidates, shadow_map)
            if not links:
                return {}
            return fold_linked_versions(search_results, memories, nodes.pool, links)
        except Exception as e:
            logger.warning("supersede_fold_failed", error=str(e))
            return {}

    async def _resolve_supersede_candidates(
        self,
        memories: Mapping[Any, Memory],
        *,
        user_id: str,
        operation: str,
        trusted_only: bool = False,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[UUID, SupersedeCandidate]:
        """#1403: liveness-guarded supersede suggestions for a page of memories.

        For every memory carrying a stored ``supersede_candidate`` (a server-only
        column — never client-writable), return an enriched, still-actionable
        ``SupersedeCandidate`` keyed by the SOURCE memory id, but only when the
        candidate target still exists, is not soft-deleted, is in the SAME
        (workspace, context) as the source memory, AND survives the same
        agent-binding row filter (#1299) every other recall lane applies.

        The (workspace, context) match is defense-in-depth: the column is written
        only from a context-scoped k-NN neighbour, so the target is same-context
        by construction — the check guarantees the read-path enrichment can never
        surface a memory's ``summary`` across a workspace/context the caller is
        not already reading (the source memory). The binding filter closes the
        narrower gap where an agent-scoped credential is restricted (by type /
        source) to a subset of an otherwise-visible context. Mirrors
        ``_apply_supersede_shadowing``'s self-healing liveness JOIN: a suggestion
        whose target is gone silently stops surfacing (the stored value is cleared
        on acceptance at edge-creation time).

        ``trusted_only`` (a recall under ``filters={"trust_tier": "trusted"}``,
        which every agent-bootstrap recall is) also drops a connector-sourced
        target, as the recall filter drops a connector-sourced row (#887): the
        suggestion carries the target's summary, so it must not bring back
        what the filter kept out. The context half of that filter holds by
        construction — the target shares the (already trusted) source's
        context.

        ``filters`` (#2027) are the recall's effective row filters (after the
        ``tags_normalize`` widening): ``type``, ``scope``, ``source_type``,
        ``source_uri_prefix``, ``tags`` and ``importance`` join the one target
        query (:func:`services.memory_listing.recall_row_predicates`), so a
        target the filter keeps out is not returned and its suggestion is
        dropped like a deleted target's — the source row stays. Time windows
        are not applied: a target is older than its source by construction.
        ``None`` (reference, update_memory) applies no row filter.

        Fail-open, per item: a malformed stored value (or a Pydantic coercion
        error) drops only that one suggestion; any wider error returns ``{}`` — a
        suggestion is a read-path enhancement and must never blank
        recall()/reference().
        """
        from pydantic import ValidationError as PydanticValidationError

        from services.agent_binding_service import filter_memory_rows_by_binding

        try:
            from services.memory_listing import recall_row_predicates
            from services.supersede_verify import (
                CONTENT_HEAD_CHARS,
                is_parallel,
                is_verified,
                min_confidence_from_env,
                stored_verification,
                verdict_current,
            )

            pending: dict[UUID, dict] = {}
            for memory in memories.values():
                cand = memory.supersede_candidate
                if not isinstance(cand, dict):
                    continue
                raw_target = cand.get("memory_id")
                sim = cand.get("similarity")
                if not raw_target or not isinstance(sim, (int, float)):
                    continue
                try:
                    target_id = UUID(str(raw_target))
                except (ValueError, TypeError):
                    continue
                pending[memory.id] = {"target_id": target_id, "candidate": cand, "source": memory}

            if not pending:
                return {}

            target_ids = {p["target_id"] for p in pending.values()}
            rows = await self.db.execute(
                select(
                    Memory.id,
                    Memory.summary,
                    Memory.workspace_id,
                    Memory.context_id,
                    Memory.type,
                    Memory.source_type,
                    # #2016: the head of the text the judge saw, to tell a
                    # stale verdict apart without reading whole contents.
                    func.substr(Memory.content, 1, CONTENT_HEAD_CHARS).label("content_head"),
                ).where(
                    Memory.id.in_(target_ids),
                    Memory.deleted_at.is_(None),
                    # #2027: the recall's row filters; an off-filter target
                    # is "gone" for this read.
                    *recall_row_predicates(filters),
                )
            )
            # Same agent-binding row filter every other recall lane enforces
            # (#1299): a binding-restricted target must not leak its summary
            # through the suggestion. Non-agent credentials → structural no-op.
            kept_rows, _denied = await filter_memory_rows_by_binding(
                self.db, list(rows.all()), operation=operation, user_id=user_id
            )
            live = {row.id: row for row in kept_rows}

            # #2016: the floor is env-only and applied here, at read time, so
            # a raised floor applies to stored verdicts too (a single-field
            # read: the resolver must not inherit from_env()'s validation).
            min_confidence = min_confidence_from_env()

            resolved: dict[UUID, SupersedeCandidate] = {}
            for source_id, p in pending.items():
                target = live.get(p["target_id"])
                if target is None:
                    continue  # self-healing: target gone → drop the suggestion
                if trusted_only and target.source_type == SOURCE_TYPE_CONNECTOR:
                    continue  # the trusted filter's connector exclusion (#887)
                source = p["source"]
                if (target.workspace_id, target.context_id) != (
                    source.workspace_id,
                    source.context_id,
                ):
                    continue  # never enrich across a context the caller isn't reading
                cand = p["candidate"]
                try:
                    # #2016 / #2059: a verdict about a text that has since
                    # been edited — the old memory's or this one's — is about
                    # text that no longer exists, so neither the parallel
                    # suppression nor ``verified`` may rest on it. The same
                    # rule decides whether a dismissal has anything to reject.
                    # (No verdict, nothing to be current: the source's text
                    # is not read at all.)
                    current = stored_verification(cand) is not None and verdict_current(
                        cand,
                        old_summary=target.summary,
                        old_content_head=getattr(target, "content_head", None),
                        new_summary=source.summary,
                        new_content_head=source.content,
                    )
                    if current and is_parallel(cand):
                        # The judge said these are two facts, not an update —
                        # kept in the column so the pair is not re-judged,
                        # never surfaced.
                        continue
                    verified = current and is_verified(cand, min_confidence=min_confidence)
                    if not verified and float(cand["similarity"]) < _SUPERSEDE_SUGGEST_THRESHOLD:
                        # #2123: a pair below 0.85 is stored only on the
                        # judge's word (judge-mode threshold). Once that word
                        # no longer confirms it — a low confidence, a raised
                        # floor, an edit of either text — it is an unverified
                        # suggestion below the precision 0.85 promises.
                        continue
                    resolved[source_id] = SupersedeCandidate(
                        memory_id=target.id,
                        summary=target.summary,
                        similarity=float(cand["similarity"]),
                        detected_at=cand.get("detected_at"),
                        # only a confirming verdict is a key on the wire.
                        verified=True if verified else None,
                    )
                except (PydanticValidationError, ValueError, TypeError):
                    continue  # malformed stored value → drop just this suggestion
            return resolved
        except Exception as e:
            logger.warning("supersede_candidate_resolve_failed", error=str(e))
            return {}

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
            # their original order (they are outside the experiment). A
            # candidate the reranker could not score is floored below the
            # scored ones and stays after them (#2062, #2084, see
            # _rerank_sort_key).
            order_before = [x for x in (_sid(r) for r in scoped) if x is not None]
            scoped.sort(key=lambda r: _rerank_sort_key(r, _adjusted(r)), reverse=True)
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
            # A candidate the reranker could not score is floored below the
            # scored ones and stays after them (#2062, #2084, see
            # _rerank_sort_key).
            search_results.sort(key=lambda r: _rerank_sort_key(r, _adjusted(r)), reverse=True)
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
                    # #1572: an omitted use_rerank (None) follows the context
                    # config, as SearchService.hybrid_search resolves it — the
                    # evidence records the applied policy, never "unspecified".
                    "use_rerank": (
                        request.use_rerank
                        if request.use_rerank is not None
                        else bool(getattr(effective_search_config, "use_rerank", False))
                    ),
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

    async def _recall_prepare(
        self,
        request: RecallRequest,
        *,
        current_context_id: UUID,
        current_workspace_id: UUID,
        context_workspace_id: UUID | None,
        context_ids: list[UUID] | None,
    ) -> _RecallPlan:
        """Resolve the effective query parameters before any search work happens.

        Folds together four small resolutions that every later phase depends on:
        the #708 paying workspace, the neural feature flag, the #81 search scope,
        the #1220 one-shot search-config prefetch, and the #1212 search-mode
        resolution.

        Args:
            request: The incoming recall request.
            current_context_id: The single declared context.
            current_workspace_id: Caller's active workspace.
            context_workspace_id: Context owner workspace (#708 Option A).
            context_ids: #81 cross-context list, or None.

        Returns:
            A _RecallPlan; ``plan.request`` is a copy with a concrete
            ``search_mode`` and MUST be used downstream in place of ``request``.
        """
        # #708 Option A: route embedding cost (key + spend cap + paid_by +
        # search + graph) to the context's owner workspace. Rate-limit gating
        # stays on the caller upstream — do NOT change it here. The H1 BYOK gate
        # is deferred until we know hybrid_search will actually fire (after the
        # analysis_cluster short-circuit), so no-cost reads (empty cluster, etc.)
        # do not get falsely 404'd.
        is_shared_context_read = (
            context_workspace_id is not None and context_workspace_id != current_workspace_id
        )
        effective_workspace_id = (
            context_workspace_id if is_shared_context_read else current_workspace_id
        )

        # Check if Neural Memory is enabled
        neural_enabled = os.getenv("ENABLE_NEURAL_MEMORY", "false").lower() == "true"

        # Issue #81: Cross-context recall — pass list of context IDs to search service
        search_context_id: str | list[str] = (
            [str(cid) for cid in context_ids] if context_ids else str(current_context_id)
        )

        # #1220 (advisor rec): ONE read-only config fetch threads into both the
        # router below and the reinforce re-rank later — previously each issued
        # its own get_by_context SELECT per recall (the #341 class of duplicated
        # hot-path reads). Fail-open: a config read failure must never break
        # recall (None = routing off; reinforce re-reads).
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
        resolved_request = request.model_copy(
            update={
                "search_mode": await self._resolve_search_mode(
                    request,
                    current_context_id,
                    cross_context=bool(context_ids),
                    config=search_config,
                )
            }
        )

        return _RecallPlan(
            request=resolved_request,
            effective_workspace_id=effective_workspace_id,  # type: ignore[arg-type]
            is_shared_context_read=is_shared_context_read,
            neural_enabled=neural_enabled,
            search_context_id=search_context_id,
            search_config=search_config,
        )

    async def _empty_recall_response(
        self,
        *,
        request: RecallRequest,
        selection_config: RecallSelectionConfig | None,
        search_config: Any,
        context_id: UUID,
        degradation: dict[str, Any] | None = None,
        timings: RecallTimings | None = None,
        user_id: str | None = None,
        context_count: int = 1,
    ) -> RecallResponse:
        """Build the zero-candidate ``recall()`` response.

        Shared by both no-result exits — the #496 empty-``analysis_cluster``
        short-circuit and the no-search-hits exit — so the empty-recall contract
        (#1047 "none" confidence, opt-in empty hints, #1306 evidence over an
        empty pool) is written exactly once instead of drifting between two
        copies.

        Args:
            request: The recall request (read for ``include_explore_hints``).
            selection_config: #1306 evaluation seam config, or None.
            search_config: Prefetched per-context search config (may be None).
            context_id: Context the evidence is stamped against.
            timings: #1989 per-stage clock of the recall; when given, the
                ``recall_completed`` line is logged here with ``results=0``
                (an empty page is still one recall, and a cold embedding or
                a wide full-text scan that found nothing is still a tail).
            user_id: Caller, for that log line.
            context_count: Contexts the recall spanned, for that log line.

        Returns:
            An empty RecallResponse carrying the full empty-recall contract.
        """
        selection_evidence: dict[str, Any] | None = None
        if selection_config is not None:
            _, selection_evidence = await self._build_selection_evidence(
                selection_config,
                eligible_ids=(),
                request=request,
                search_config=search_config,
                context_id=context_id,
            )
        response = RecallResponse(
            results=[],
            explore_hints=[] if request.include_explore_hints else None,
            confidence=self._compute_recall_confidence([]),  # #1047: "none"
            selection_evidence=selection_evidence,
            # #1515: a degraded search can legitimately return nothing, and that
            # is exactly when the caller most needs to know the semantic arm was
            # missing — otherwise an empty result reads as "nothing is stored".
            degraded=(degradation or {}).get("degraded"),
            degraded_reason=(degradation or {}).get("reason"),
        )
        if timings is not None:
            self._log_recall_completed(
                timings,
                user_id=user_id,
                degradation=degradation,
                results=0,
                candidates=0,
                k=request.k,
                context_count=context_count,
            )
        return response

    @staticmethod
    def _log_recall_completed(
        timings: RecallTimings,
        *,
        user_id: str | None,
        degradation: dict[str, Any] | None,
        results: int,
        candidates: int,
        k: int,
        context_count: int,
    ) -> None:
        """The one ``recall_completed`` line (#1989), shared by both exits so
        the empty page and the full page log the same shape."""
        logger.info(
            "recall_completed",
            user_id=user_id,
            degraded=(degradation or {}).get("degraded"),
            **timings.log_fields(
                results=results, candidates=candidates, k=k, context_count=context_count
            ),
        )

    async def _recall_check_agent_bindings(
        self,
        *,
        current_context_id: UUID,
        context_ids: list[UUID] | None,
        user_id: str,
    ) -> None:
        """#1291: apply the subtractive agent-binding read gate at the service layer.

        The MCP handler gates this path via ``_resolve_context_for_read``, but the
        REST ``/memory/recall`` route calls this service method directly and so
        bypassed the filter before #1291. Gating HERE means an agent-bound
        credential cannot read a binding-denied context via ANY caller.

        No-op for non-agent credentials (``get_agent_scope()`` is None → one
        contextvar read, no DB query). Covers the single declared context and
        each entry on the #81 cross-context list — exactly the set searched
        downstream.

        Args:
            current_context_id: The single declared context.
            context_ids: #81 cross-context list, or None for single-context.
            user_id: Caller identity, recorded on deny-capture audit rows.

        Raises:
            NotFoundException: when any searched context is binding-denied. The
                uniform "Context" 404 matches the MCP path's response so the
                deny does not leak existence.
        """
        from services.agent_binding_service import agent_binding_permits

        for gated_cid in context_ids if context_ids else [current_context_id]:
            if not await agent_binding_permits(
                self.db,
                gated_cid,
                "read",
                operation="recall",  # #1286 (P0-5): deny-capture audit identity
                user_id=user_id,
            ):
                raise NotFoundException("Context", str(gated_cid))

    async def _recall_resolve_cluster_filter(
        self, request: RecallRequest, *, effective_workspace_id: UUID
    ) -> list[UUID] | None:
        """Issue #496: resolve ``filters.analysis_cluster`` to its member memory ids.

        Pre-resolving the cluster lets recall both (a) short-circuit an empty
        cluster before paying for the embedding + search round-trip and (b)
        expand the candidate pool to cover the whole cluster — without which a
        small cluster may end up with zero overlap with Qdrant's top-N
        candidates. The actual ``Memory.id IN ...`` filter is applied at the PG
        SELECT during hydration; rerank / hybrid scoring see only cluster members.

        Args:
            request: The recall request carrying ``filters``.
            effective_workspace_id: #708-aware paying workspace. Passing it means
                a stolen ``run_id`` from a foreign workspace resolves to nothing
                (same shape as cluster-not-found), so recall short-circuits to
                empty without leaking existence.

        Returns:
            ``None`` when the filter is absent (no cluster scoping), otherwise
            the cluster's member ids — possibly an empty list, which the caller
            turns into an immediate empty response.

        Raises:
            ValidationError: on a malformed filter. Deliberately
                ``ValidationError`` (a ``MemoryCloudException`` subclass) rather
                than a plain ``ValueError``: only the former is caught by the
                global ``memory_cloud_exception_handler``, so a client sending
                ``analysis_cluster: "abc"`` gets a 422 with the structured
                ``{error, message, details}`` envelope instead of an internal 500.
        """
        if not request.filters:
            return None
        cluster_filter = request.filters.get("analysis_cluster")
        if not cluster_filter:
            return None

        from services.analysis import query_service as _analysis_query_service

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
        member_ids = await _analysis_query_service.get_memory_ids_in_cluster(
            self.db,
            workspace_id=effective_workspace_id,
            run_id=cluster_run_id,
            cluster_index=cluster_index_int,
        )
        # Normalize "cluster unknown" (None) and "cluster empty" ([]) to the
        # same empty list — both mean "no candidates" to the caller, and the
        # non-None return still signals that cluster scoping is active.
        return member_ids or []

    async def _recall_enforce_shared_byok(
        self,
        request: RecallRequest,
        *,
        user_id: str,
        current_context_id: UUID,
        current_workspace_id: UUID,
        context_workspace_id: UUID,
    ) -> None:
        """#708 Option A H1 gate, deferred until a search will actually fire.

        Probes the source workspace for a BYOK key applicable to this context
        BEFORE the embedding API call is issued, so the ``OPENAI_API_KEY`` env
        fallback in ``_get_user_api_key`` cannot silently bypass PR #711's
        BYOK-only spend cap.

        The probe MUST use the per-context embedding model — the same source of
        truth ``SearchService.hybrid_search`` uses to pick its embed client.
        Probing with the global default would falsely deny self-hosted-backed
        contexts (provider mismatch) and falsely pass OpenAI-backed contexts when
        the platform default is self-hosted (the gate would skip entirely).

        Called only after the ``analysis_cluster`` short-circuit, so no-cost
        reads (empty cluster, etc.) are never falsely 404'd.

        Args:
            request: The recall request (read for the resolved ``search_mode``).
            user_id: Caller identity, for the deny log line.
            current_context_id: Context being read.
            current_workspace_id: Caller's own workspace.
            context_workspace_id: Context owner workspace — the one that pays.

        Raises:
            NotFoundException: uniform 404 when the source workspace has no
                applicable BYOK key. Deliberately indistinguishable from
                "context does not exist" so the deny leaks neither existence nor
                configuration state (CWE-639 / OWASP A01).
        """
        from repositories.config_repository import ContextSearchConfigRepository

        ctx_config = await ContextSearchConfigRepository(self.db).create_or_get(current_context_id)
        byok_embed_svc = EmbeddingService(self.db, model=ctx_config.embedding_model)
        # Only gate when the embedding API call would actually fire and produce a
        # charge against the source workspace. ``keyword`` mode skips
        # ``embed_with_usage`` entirely (BM25-only); a self-hosted backend is
        # free/local (no platform-key fallback path).
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

    async def _recall_hydrate_memories(
        self,
        request: RecallRequest,
        *,
        memory_ids: list[str],
        cluster_memory_ids: list[UUID] | None,
    ) -> dict[str, Memory]:
        """Hydrate the search hits from PostgreSQL, keyed by every id they may carry.

        ``memory_ids`` are Qdrant point ids. For ``remember()``-written memories
        the point id equals ``Memory.id`` (see ``add_memory_to_qdrant`` /
        ``summary_embedding_id == id``). Resource-projected memories
        (``ResourceIndexer._apply_upsert``) instead store the point under
        ``uuid5(resource_id:doc_id:vN) != Memory.id`` and record the link in
        ``Memory.summary_embedding_id`` (the authoritative "Qdrant point id"
        column). Matching on ``Memory.id`` alone silently drops every resource hit
        during hydration → recall returns 0 in all modes (Issue #972).

        So the SELECT resolves via either identifier, and the returned dict is
        keyed by BOTH the row id and the point id — keying by both (rather than
        only the point id) keeps the lookup robust when ``summary_embedding_id``
        is unset or divergent. For normal memories the two coincide (one key);
        across rows they never collide because both id spaces are globally unique
        UUIDs.

        Also applies the #214 source post-filters, the #887 trust-tier read
        filter and the #496 cluster scope.

        Args:
            request: The recall request carrying ``filters``.
            memory_ids: Qdrant point ids from the hybrid search.
            cluster_memory_ids: #496 cluster scope, or None when not scoped.
                Guaranteed non-empty here — the caller short-circuits an empty
                cluster before reaching hydration.

        Returns:
            Point-id / row-id → Memory, excluding soft-deleted rows.
        """
        pg_conditions = [
            or_(
                Memory.id.in_(memory_ids),
                Memory.summary_embedding_id.in_(memory_ids),
            ),
            Memory.deleted_at.is_(None),
        ]
        if request.filters:
            # Issue #214: source_uri_prefix and source_type post-filters
            if prefix := request.filters.get("source_uri_prefix"):
                escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pg_conditions.append(Memory.source_uri.like(f"{escaped}%", escape="\\"))
            if stype := request.filters.get("source_type"):
                pg_conditions.append(Memory.source_type == stype)
            # Issue #887: trust-tier read filter. ``trusted`` excludes
            # external-origin memories from behaviour-influencing reads (OWASP
            # LLM01/LLM03 — external content is data, not instructions).
            if _is_trusted_recall(request):
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
        if cluster_memory_ids is not None:
            pg_conditions.append(Memory.id.in_(cluster_memory_ids))

        result = await self.db.execute(select(Memory).where(*pg_conditions))
        memories: dict[str, Memory] = {}
        for m in result.scalars().all():
            memories[str(m.id)] = m
            if m.summary_embedding_id is not None:
                memories[str(m.summary_embedding_id)] = m
        return memories

    async def _recall_filter_rows_by_binding(
        self,
        search_results: list[dict],
        memories: dict[str, Memory],
        *,
        user_id: str,
    ) -> tuple[dict[str, Memory], int]:
        """#1299: per-memory type/source binding filter over the FULL candidate pool.

        Applied at the service layer (REST, MCP, share-key and the bootstrap
        recall lane all pass through here) BEFORE shadowing, rerank and the
        top-k slice, so a denied row can never survive into the slice.

        Enforcement is subtractive: the candidate pool is already bounded to ~k
        by hybrid_search's own top-k truncation, so removing denied rows MAY
        return fewer than k results — there is no backfill (matching the
        deletion / supersede-shadowing precedent). Shadow mode keeps every row
        and records the per-context would_deny aggregate inside the helper.

        Args:
            search_results: Candidate pool, pruned in place.
            memories: Hydrated point-id → Memory map.
            user_id: Caller identity for the binding lookup.

        Returns:
            (surviving memory map, number of rows filtered out).
        """
        if not memories:
            return memories, 0

        from services.agent_binding_service import filter_memory_rows_by_binding

        unique_rows = list({m.id: m for m in memories.values()}.values())
        kept_rows, binding_row_filtered = await filter_memory_rows_by_binding(
            self.db, unique_rows, operation="recall", user_id=user_id
        )
        if not binding_row_filtered:
            return memories, 0

        kept_ids = {m.id for m in kept_rows}
        search_results[:] = [
            r for r in search_results if r["id"] not in memories or memories[r["id"]].id in kept_ids
        ]
        return {key: m for key, m in memories.items() if m.id in kept_ids}, binding_row_filtered

    async def _recall_apply_selection_plan(
        self,
        search_results: list[dict],
        memories: dict[str, Memory],
        *,
        selection_config: RecallSelectionConfig,
        request: RecallRequest,
        search_config: Any,
        context_id: UUID,
    ) -> dict[str, Any]:
        """#1306: stamp selection evidence and reorder the pool to match the plan.

        Every subtractive gate (trusted context/source, agent row binding,
        deletion, cluster scope, supersede shadowing and folding) has already run by this
        point, so the pool seen here is precisely the authorized eligible pool
        used by this query.

        Args:
            search_results: Candidate pool, reordered in place.
            memories: Hydrated point-id → Memory map.
            selection_config: The evaluation seam config.
            request: The recall request (``k`` bounds the plan).
            search_config: Prefetched per-context search config (may be None).
            context_id: Context the evidence is stamped against.

        Returns:
            The identity-only evidence dict.
        """
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

        # The over-fetch upstream (neural k*4, cluster buffers) can exceed
        # candidate_pool_k, but the stamped policy promises a bounded registered
        # pool. Truncate on the EVIDENCE side only, in production rank order —
        # eligible_results / search_results stay untouched so response building
        # and neural co-activation keep seeing exactly what production saw.
        registered_pool = tuple(eligible_ids[: selection_config.candidate_pool_k])

        plan, selection_evidence = await self._build_selection_evidence(
            selection_config,
            eligible_ids=registered_pool,
            request=request,
            search_config=search_config,
            context_id=context_id,
        )
        by_memory_id = {str(memories[result["id"]].id): result for result in eligible_results}
        selected_set = set(plan.selected_ids)
        selected_results = [by_memory_id[memory_id] for memory_id in plan.selected_ids]
        unselected_results = [
            result for memory_id, result in by_memory_id.items() if memory_id not in selected_set
        ]
        # The normal response builder and neural co-activation path continue to
        # consume ``search_results[:k]``; reorder only, never synthesize.
        search_results[:] = selected_results + unselected_results
        return selection_evidence

    async def _recall_build_responses(
        self,
        request: RecallRequest,
        search_results: list[dict],
        memories: dict[str, Memory],
        *,
        shadow_map: dict[UUID, UUID],
        contradiction_map: dict[UUID, list[UUID]],
        user_id: str,
        fold_map: Mapping[UUID, list[UUID]] | None = None,
        content_trusted_only: bool = False,
        resolved_candidates: Mapping[UUID, SupersedeCandidate | None] | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> list[MemoryResponse]:
        """Build the page that ships: supersede hints and response rows.

        Read-only (#1985): the access-stat bump and auto-promotion run after the
        recall commits, in :meth:`_recall_record_access`.

        Args:
            request: The recall request (``k`` is the page size).
            search_results: Ranked candidate pool.
            memories: Hydrated point-id → Memory map.
            shadow_map: #1208 dst → superseding src annotations.
            contradiction_map: #1208 memory → contradicting opponents.
            user_id: Caller identity for the supersede-candidate lookup.
            fold_map: #2123 head id → folded older versions, newest first.
            content_trusted_only: #2124 only trusted rows (a trusted context,
                not connector-ingested) may carry content.
            resolved_candidates: #2131 the fold's resolution (memory id →
                surviving hint or ``None``); those rows are not resolved again.
            filters: #2027 the row filters the recall searched with (after the
                ``tags_normalize`` widening); a suggestion's target is held to
                them. ``None`` applies no row filter, as in the fold.

        Returns:
            Up to ``request.k`` MemoryResponse rows, skipping hits with no
            hydrated PostgreSQL row.
        """
        # #1403: resolve liveness-guarded supersede suggestions for exactly the
        # page that ships — one batched query, run AFTER the top-k slice +
        # rerank so discarded candidates cost nothing (empty/fast when none
        # carry one).
        # #2131: rows the fold already resolved (same caller, same operation,
        # same trust filter) reuse its answer; only the rest are queried.
        page = search_results[: request.k]
        page_memories = {sr["id"]: memories[sr["id"]] for sr in page if sr["id"] in memories}
        resolved = resolved_candidates or {}
        pending = {pid: m for pid, m in page_memories.items() if m.id not in resolved}
        supersede_candidates: dict[UUID, SupersedeCandidate] = {
            m.id: candidate
            for m in page_memories.values()
            if (candidate := resolved.get(m.id)) is not None
        }
        if pending:
            supersede_candidates.update(
                await self._resolve_supersede_candidates(
                    pending,
                    user_id=user_id,
                    operation="recall",
                    trusted_only=_is_trusted_recall(request),
                    filters=filters,
                )
            )
        fold_map = fold_map or {}
        # #2124: the first ``full`` eligible shipped rows carry their content (a
        # folded group's content is its head's — the head is the row that
        # ships). With ``content_trusted_only`` (the MCP tool's defaulted full)
        # only trusted rows are eligible: external content is not inlined into
        # the agent's context without a deliberate request, and a row passed
        # over leaves its slot to the next trusted one.
        full = effective_full(request.full, request.k)
        trusted_context_ids: set[UUID] | None = None
        if full and content_trusted_only and not _is_trusted_recall(request):
            trusted_context_ids = await self._recall_trusted_context_ids(
                {m.context_id for m in page_memories.values()}
            )
        content_rows = 0

        responses: list[MemoryResponse] = []
        for search_result in page:
            memory = memories.get(search_result["id"])

            if not memory:
                continue

            content: str | None = None
            content_next_offset: int | None = None
            eligible = trusted_context_ids is None or (
                memory.source_type != SOURCE_TYPE_CONNECTOR
                and memory.context_id in trusted_context_ids
            )
            if content_rows < full and eligible and memory.content:
                content, content_next_offset = content_slice(memory.content)
                content_rows += 1

            # Issue #1047: snapshot updated_at as read. #1317 removed the
            # column's onupdate and #1985 moved the access-stats write to a
            # separate session after the commit, so nothing dirties the row
            # here; the snapshot stays as a cheap guard.
            snapshot_updated_at = memory.updated_at

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
                    # #1403: near-duplicate this memory may supersede (or None).
                    supersede_candidate=supersede_candidates.get(memory.id),
                    # #2123: older versions folded under this row, if any.
                    folded=(
                        FoldedVersions(count=len(folded_ids), memory_ids=folded_ids)
                        if (folded_ids := fold_map.get(memory.id))
                        else None
                    ),
                    version=memory.version,  # #2043
                    content=content,
                    content_truncated=True if content_next_offset is not None else None,
                    content_next_offset=content_next_offset,
                )
            )
        return responses

    async def _recall_apply_llm_selector(
        self,
        request: RecallRequest,
        responses: list[MemoryResponse],
        *,
        llm_selector: bool,
        search_config: Any,
        cross_context: bool,
        selection_config: RecallSelectionConfig | None,
        degradation: dict[str, Any],
        user_id: str,
        context_id: UUID,
        effective_workspace_id: UUID,
        timings: RecallTimings,
    ) -> tuple[list[MemoryResponse], SelectorReport | None]:
        """Run the #2144 LLM selector over the built page when it applies.

        Checked cheapest first; any miss returns the page unchanged and no
        report (the envelope then has no ``selector`` key):

        1. the caller opted in (``llm_selector`` — only the MCP recall tool);
        2. a single-context recall whose prefetched search config has
           ``llm_selector_enabled`` stored on;
        3. at least one row carries content (``full`` >= 1) — selecting among
           summaries alone saves nothing;
        4. not ``include_superseded`` (audit mode shows the raw page), not the
           #1306 evaluation seam, not a degraded (keyword-only) search;
        5. the plan gate says the flag is effective — otherwise
           ``{applied: false, reason: "inactive"}`` (also when the plan lookup
           itself failed: ``llm_selector_effective`` fails closed and logs
           ``llm_selector_status_load_failed``).

        The stage itself (:func:`services.recall_selector.run_selector`) never
        raises; its failures come back as ``{applied: false, reason}`` with the
        page unchanged. It is timed as the ``select`` stage.

        Returns:
            The page to ship and the report (None when the stage did not apply).
        """
        if not llm_selector or cross_context:
            return responses, None
        if getattr(search_config, "llm_selector_enabled", False) is not True:
            return responses, None
        if effective_full(request.full, request.k) < 1 or not any(r.content for r in responses):
            return responses, None
        if request.include_superseded or selection_config is not None:
            return responses, None
        if degradation.get("degraded"):
            return responses, None

        from config.settings import get_settings
        from services.llm_selector_policy import llm_selector_effective
        from services.recall_selector import run_selector

        with timings.stage("select"):
            if not await llm_selector_effective(self.db, context_id, stored=True):
                logger.info("recall_selector_inactive", context_id=str(context_id))
                return responses, SelectorReport(applied=False, reason="inactive")
            try:
                result = await run_selector(
                    self.db,
                    query=request.query,
                    responses=responses,
                    user_id=user_id,
                    workspace_id=str(effective_workspace_id),
                    context_id=str(context_id),
                    settings=get_settings(),
                    # The page is read-only (#1985) and recall commits right
                    # after this stage: commit before the round trip so the
                    # connection is not held idle in transaction (#2070 rule).
                    release_db_before_call=True,
                )
            except Exception as exc:  # noqa: BLE001 — the stage must never fail a recall
                logger.warning(
                    "recall_selector_failed_open",
                    reason="provider_error",
                    error_type=type(exc).__name__,
                )
                return responses, SelectorReport(applied=False, reason="provider_error")
        return result.responses, result.report

    async def _recall_trusted_context_ids(self, context_ids: set[UUID]) -> set[UUID]:
        """The ids among ``context_ids`` whose context is trusted (#2124).

        The same server-side signal as the ``trust_tier="trusted"`` filter
        (``Context.trust_tier``); one query over the page's contexts.
        """
        if not context_ids:
            return set()
        result = await self.db.execute(
            select(Context.id).where(
                Context.id.in_(context_ids), Context.trust_tier == CONTEXT_TRUST_TIER_TRUSTED
            )
        )
        return set(result.scalars().all())

    def _isolated_session(self) -> AsyncSession:
        """A fresh session on the request session's engine (#1985).

        Post-commit statistics writes use it so that their row locks and any
        failure stay out of the request transaction. The caller commits.
        The recipe lives in ``db.base.isolated_session``.
        """
        from db.base import isolated_session

        return isolated_session(self.db)

    @staticmethod
    def _promotion_due(memory: Memory, stats: AccessStats) -> bool:
        """Whether a just-surfaced memory qualifies for persistent scope.

        Count, clients and scope come from the locked, updated row (#1985), so
        concurrent recalls by different clients add up; importance and age
        come from the row as read (no read path writes them).

        Args:
            memory: The row as the recall read it.
            stats: The row as this recall's access-stat write left it.
        """
        if stats.scope == "persistent":
            return False
        return should_promote_to_persistent(
            access_count=stats.access_count,
            age_days=(utcnow() - memory.created_at).days,
            importance=memory.importance,
            accessed_by_clients=list(stats.accessed_by_clients or []),
        )

    async def record_adoption(self, memory_id: UUID) -> bool:
        """Count one adoption (#1046) of a memory whose read was already checked.

        Issue #2139: the MCP reference handler calls this for a continuation
        page that is the session's first read of the memory, after the page
        was built. Same bump as an offset-less ``reference()`` (access_count,
        reference_count, last_used_at), in an isolated session so its row lock
        and any failure stay out of the request transaction. Best-effort: a
        failure is logged as ``access_stats_update_failed`` (``error_type``
        only — a driver error's text can carry the DSN) and never raised.

        Args:
            memory_id: The memory read.

        Returns:
            True when the bump landed; False when the row was skipped (locked
            by another transaction, deleted) or the write failed.
        """
        try:
            async with self._isolated_session() as stats_db:
                updated = await MemoryRepository(stats_db).bulk_update_access_stats(
                    [memory_id], "api", count_as_adoption=True
                )
                await stats_db.commit()
        except Exception as exc:  # noqa: BLE001 — the page is delivered; never fail it
            logger.warning(
                "access_stats_update_failed",
                error_type=type(exc).__name__,
                memory_count=1,
            )
            return False
        return memory_id in updated

    async def _recall_record_access(
        self,
        responses: list[MemoryResponse],
        memories: dict[str, Memory],
        *,
        client: str,
    ) -> None:
        """Bump access stats for the shipped page and promote what qualifies.

        Issue #1985: runs after the recall commit in an isolated session. The
        bump is one lock-ordered statement over the page and promotion is
        another, so two recalls over overlapping pages queue instead of
        deadlocking. Best-effort: any failure is logged as
        ``access_stats_update_failed`` (with traceback) and the recall still
        returns its page.
        Each bumped row's response shows the scope the locked row has, and a
        row this call promoted shows ``scope="persistent"`` (#1985).

        Args:
            responses: The page being returned (mutated for promoted rows).
            memories: Hydrated point-id → Memory map (read state).
            client: Client name recorded in ``accessed_by_clients``.
        """
        if not responses:
            return
        rows = {memory.id: memory for memory in memories.values()}
        page_ids = [response.memory_id for response in responses]
        try:
            async with self._isolated_session() as stats_db:
                repo = MemoryRepository(stats_db)
                updated = await repo.bulk_update_access_stats(page_ids, client)
                due = [
                    memory_id
                    for memory_id, stats in updated.items()
                    if memory_id in rows and self._promotion_due(rows[memory_id], stats)
                ]
                promoted = (
                    set(await repo.bulk_promote_to_persistent(due, skip_locked=True))
                    if due
                    else set()
                )
                await stats_db.commit()
        except Exception as exc:  # noqa: BLE001 — the page is committed; never fail it
            # Any failure here (database, refused connection, or a bug) is
            # logged with its traceback and the recall still returns its page.
            logger.warning(
                "access_stats_update_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                memory_count=len(page_ids),
                exc_info=True,
            )
            return
        # The scope as the locked row has it (a concurrent promotion shows),
        # then this call's own promotions. Rows the write skipped as locked
        # keep the scope the recall read.
        for response in responses:
            if response.memory_id in promoted:
                response.scope = "persistent"
            elif response.memory_id in updated:
                response.scope = updated[response.memory_id].scope
        if promoted:
            logger.info("memories_promoted", memory_ids=[str(mid) for mid in sorted(promoted)])

    async def _recall_run_hebbian_learning(
        self,
        request: RecallRequest,
        search_results: list[dict],
        memories: dict[str, Memory],
        *,
        user_id: str,
        current_context_id: UUID,
        effective_workspace_id: UUID,
    ) -> None:
        """Best-effort Hebbian graph update — builds the graph ``explore()`` reads.

        Issue #120: the neural graph is for ``explore()`` only. ``recall()``
        itself ranks on pure hybrid-search scores (no UnifiedScorer); Hebbian
        learning still runs here so the graph keeps accumulating co-activation
        evidence from real queries.

        Fail-open: any error is logged and swallowed — a graph problem must never
        break a recall. Issue #1985: runs after the recall commit in its own
        session (committed here), so a failed edge write cannot abort the
        recall's transaction, and edge rows are locked in (src, dst) order by
        ``HebbianLearner.apply_updates``.

        Args:
            request: The recall request (``query`` keys the #983 event dedup).
            search_results: Ranked candidate pool.
            memories: Hydrated point-id → Memory map.
            user_id: Graph owner.
            current_context_id: Context whose embedding keys the #982 edge gate.
            effective_workspace_id: #708-aware workspace for graph mutations.
        """
        try:
            async with self._isolated_session() as graph_db:
                await self._recall_learn_on_session(
                    graph_db,
                    request,
                    search_results,
                    memories,
                    user_id=user_id,
                    current_context_id=current_context_id,
                    effective_workspace_id=effective_workspace_id,
                )
                await graph_db.commit()
        except Exception as exc:
            logger.warning("hebbian_update_failed", error=str(exc))

    async def _recall_learn_on_session(
        self,
        graph_db: AsyncSession,
        request: RecallRequest,
        search_results: list[dict],
        memories: dict[str, Memory],
        *,
        user_id: str,
        current_context_id: UUID,
        effective_workspace_id: UUID,
    ) -> None:
        """The Hebbian pass of :meth:`_recall_run_hebbian_learning` on ``graph_db``.

        Args:
            graph_db: The isolated session every graph read and write uses.
            request: The recall request.
            search_results: Ranked candidate pool.
            memories: Hydrated point-id → Memory map.
            user_id: Graph owner.
            current_context_id: Context whose embedding keys the #982 edge gate.
            effective_workspace_id: #708-aware workspace for graph mutations.
        """
        from neural.co_activation import CoActivationTracker
        from neural.config import NeuralMemoryConfig
        from neural.hebbian import HebbianLearner
        from neural.models import ActivationState, NeuralMemoryNode
        from repositories.graph import GraphRepository
        from services.graph_service import GraphService

        # #1985: one learning pass per graph owner at a time. Apply and prune
        # are two phases, so no per-phase sort gives concurrent passes one lock
        # order; a pass that finds another running is skipped, not queued.
        if not await try_lock_graph_learning(graph_db, user_id):
            logger.info("hebbian_update_skipped_busy", user_id=user_id)
            return

        config = await NeuralMemoryConfig.from_db(graph_db)
        graph_repo = GraphRepository(graph_db)
        await graph_repo.get_or_create(user_id)

        graph_service = GraphService(
            user_id=user_id,
            db=graph_db,
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
            # A candidate the reranker could not score is kept, at its
            # floored hybrid_score: at most the weakest scored row (#2084).
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
            ActivationState(node_id=nid, activation=min(1.0, max(0.0, score_map.get(nid, 0.0))))
            for nid in nodes_dict
        ]

        # Co-activation tracking with semantic gating
        embedding_map = {nid: node.embedding for nid, node in nodes_dict.items() if node.embedding}
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
            # must not abort the Hebbian session's transaction (which
            # still has the neural graph writes below to commit). The lookup
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
                    ctx_search_cfg = await ContextSearchConfigRepository(edge_db).get_by_context(
                        current_context_id
                    )
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
                edge_threshold if edge_threshold is not None else config.min_similarity_for_edge
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

    async def _recall_finalize(
        self,
        request: RecallRequest,
        search_results: list[dict],
        responses: list[MemoryResponse],
        *,
        user_id: str,
        current_context_id: UUID,
        effective_workspace_id: UUID,
        neural_enabled: bool,
        binding_row_filtered: int,
        selection_evidence: dict[str, Any] | None,
        cross_context: bool = False,
        degradation: dict[str, Any] | None = None,
        timings: RecallTimings | None = None,
        context_count: int = 1,
        selector: SelectorReport | None = None,
    ) -> RecallResponse:
        """Assemble the final response: hints, tags, confidence, audit row.

        Args:
            request: The recall request.
            search_results: The full candidate pool (wider than the returned page
                when neural over-fetch is on) — the confidence distribution is
                computed over all of it.
            responses: The page being returned.
            user_id: Caller identity.
            current_context_id: Context that was searched.
            effective_workspace_id: #708-aware search workspace.
            neural_enabled: Whether the neural graph is available to hint from.
            binding_row_filtered: #1299 subtracted-row count, for the audit row.
            selection_evidence: #1306 evidence dict, or None.
            cross_context: True when the recall spanned several contexts, which
                disables the single-context tag hints (#1503).
            timings: #1989 per-stage clock; this whole method is its
                ``finalize`` stage and ``recall_completed`` is logged from it
                last, after the audit row. ``None`` (direct callers) logs no
                line, like :meth:`_empty_recall_response`.
            context_count: Contexts the recall spanned, for the log line.
            selector: #2144 the LLM selector's report, or None.

        Returns:
            The completed RecallResponse.
        """
        with timings.stage("finalize") if timings is not None else contextlib.nullcontext():
            response = await self._recall_finalize_response(
                request,
                search_results,
                responses,
                user_id=user_id,
                current_context_id=current_context_id,
                effective_workspace_id=effective_workspace_id,
                neural_enabled=neural_enabled,
                binding_row_filtered=binding_row_filtered,
                selection_evidence=selection_evidence,
                cross_context=cross_context,
                degradation=degradation,
                selector=selector,
            )
        # #1989: one line per recall, after everything the caller waits for.
        if timings is not None:
            self._log_recall_completed(
                timings,
                user_id=user_id,
                degradation=degradation,
                results=len(responses),
                candidates=len(search_results),
                k=request.k,
                context_count=context_count,
            )
        return response

    async def _recall_finalize_response(
        self,
        request: RecallRequest,
        search_results: list[dict],
        responses: list[MemoryResponse],
        *,
        user_id: str,
        current_context_id: UUID,
        effective_workspace_id: UUID,
        neural_enabled: bool,
        binding_row_filtered: int,
        selection_evidence: dict[str, Any] | None,
        cross_context: bool,
        degradation: dict[str, Any] | None,
        selector: SelectorReport | None = None,
    ) -> RecallResponse:
        """The body of :meth:`_recall_finalize`, timed as one stage there.

        Builds the page: explore hints (opt-in), related tags, the
        ``memory_access_events`` audit row, the relevance confidence over the
        full candidate pool, and the empty-result tag suggestions. Arguments
        are :meth:`_recall_finalize`'s, which documents them.

        Returns:
            The completed RecallResponse.
        """
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

        # Issue #1047/#1052: relevance confidence from the full candidate pool's
        # score distribution (search_results, which holds up to candidates_k >> k
        # when neural is on). ``candidate_scores`` are the normalized hybrid scores
        # (ranking order); ``semantic_scores`` are the RAW per-memory cosines that
        # carry absolute match strength for absence detection (#1052). See
        # _compute_recall_confidence.
        candidate_scores = [
            score
            for r in search_results
            if (score := r.get("hybrid_score", r.get("score"))) is not None
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
        from services.memory_access_event_writer import emit_memory_access_event

        await emit_memory_access_event(
            operation="recall",
            outcome="success",
            workspace_id=effective_workspace_id,
            user_id=user_id,
            context_id=current_context_id,
            result_count=len(responses),
            query=request.query,
            extra_metadata=_recall_audit_metadata(responses, binding_row_filtered, selector),
        )

        return RecallResponse(
            results=responses,
            selector=selector,
            related_tags=related_tags,
            explore_hints=explore_hints,
            # #1515: None (and therefore omitted) unless the search degraded.
            degraded=(degradation or {}).get("degraded"),
            degraded_reason=(degradation or {}).get("reason"),
            confidence=self._compute_recall_confidence(
                candidate_scores, semantic_scores=semantic_scores or None
            ),
            selection_evidence=selection_evidence,
            # #1503: only on an empty tag-filtered recall — see the helper.
            # #2144: not when the selector emptied the page — the filter did
            # match; the rows were judged irrelevant.
            tag_suggestions=(
                None
                if selector is not None and selector.applied and selector.dropped
                else await self._tag_suggestions_for_empty_result(
                    request,
                    responses,
                    user_id=user_id,
                    workspace_id=effective_workspace_id,
                    context_id=current_context_id,
                    cross_context=cross_context,
                )
            ),
        )

    async def _lint_write(
        self,
        *,
        workspace_id: UUID | None,
        context_id: UUID | None,
        user_id: str,
        summary: str | None,
        tags: list[str] | None,
        memory_id: UUID | None = None,
    ) -> list[WriteLintHint]:
        """#1502: advisory recall-ability hints for a completed write.

        Returns no hints when the write cannot be located in a context (the
        vocabulary comparison would be meaningless) or when nothing is worth
        saying.

        Total by construction. ``lint_write`` guards its own body, but the
        ``import`` below is outside that guard and this runs AFTER the write has
        committed — in ``remember()`` it sits inside the ``try`` whose handler
        rolls back and logs ``memory_creation_failed``, so an escaping exception
        would report a stored memory as failed and invite a duplicating retry.
        """
        if workspace_id is None or context_id is None or not summary:
            return []

        try:
            from services.write_lint import lint_write

            return await lint_write(
                self.db,
                workspace_id=workspace_id,
                context_id=context_id,
                user_id=user_id,
                summary=summary,
                tags=tags,
                memory_id=memory_id,
            )
        except Exception as e:  # noqa: BLE001 — advisory; never fail a committed write
            logger.warning("write_lint_unavailable", error=str(e))
            return []

    @staticmethod
    def _requested_tag_filter(filters: dict | None) -> list[str]:
        """The non-empty string tags a caller filtered on (#1503).

        Mirrors ``db.qdrant._build_tag_filter_conditions``'s own validation, so
        a filter that builder ignores never triggers expansion or suggestions.
        """
        if not filters:
            return []
        raw = filters.get("tags")
        if not isinstance(raw, list):
            return []
        return [t for t in raw if isinstance(t, str) and t]

    async def _tag_suggestions_for_empty_result(
        self,
        request: RecallRequest,
        responses: list,
        *,
        user_id: str,
        workspace_id: UUID,
        context_id: UUID,
        cross_context: bool,
    ) -> dict[str, list[str]] | None:
        """#1503: near-miss tags for a tag-filtered recall that found nothing.

        Deliberately narrow. It fires only on a ZERO-result recall that actually
        filtered on tags, so the extra vocabulary query never rides a successful
        recall, and it cannot be mistaken for "these tags also matched" on a
        partial result.

        Skipped entirely for a cross-context recall: the vocabulary read covers
        ONE context, so suggesting from the primary alone would describe a
        different search than the one the caller ran.
        """
        if responses or cross_context:
            return None
        tags = self._requested_tag_filter(request.filters)
        if not tags:
            return None

        from services.tag_resolution import suggest_tags

        suggestions = await suggest_tags(
            self.db,
            workspace_id=workspace_id,
            context_id=context_id,
            user_id=user_id,
            tags=tags,
        )
        return suggestions or None

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
        *,
        capacity_gate: bool = True,
        content_trusted_only: bool = False,
        llm_selector: bool = False,
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
            content_trusted_only: #2124 only trusted rows may carry content
                (the MCP tool's defaulted ``full``).
            llm_selector: #2144 the caller allows the LLM selector stage. Only
                the MCP recall tool passes True; the stage then runs when the
                context's flag is effective (see
                :meth:`_recall_apply_llm_selector`).

        Returns:
            RecallResponse with search results
        """
        logger.info(
            "recall_request",
            user_id=user_id,
            # #1721 (Directory 1.D): length + keyed hash, never the query text.
            **query_log_fields(request.query),
            k=request.k,
            use_rerank=request.use_rerank,
        )
        # #1989: one clock per recall; every stage below reports into it and
        # _recall_finalize / _empty_recall_response log recall_completed from it.
        timings = RecallTimings()
        context_count = len(context_ids) if context_ids else 1

        # Single Collection Migration: Validate required parameters
        if not current_workspace_id or not current_context_id:
            raise ValueError("recall() requires current_workspace_id and current_context_id")
        if selection_config is not None and not _is_trusted_recall(request):
            raise ValueError("selection evidence requires trusted-tier recall")

        # #1291: recall does NOT pass through
        # ``PermissionService.resolve_context_for_workspace_read`` — the gate is
        # applied at the service layer so it covers every caller. See the helper.
        await self._recall_check_agent_bindings(
            current_context_id=current_context_id,
            context_ids=context_ids,
            user_id=user_id,
        )

        # Resolve the effective query parameters once (#708 paying workspace,
        # neural flag, #81 search scope, #1220 config prefetch, #1212 search
        # mode). ``plan.request`` supersedes the caller's request from here on.
        plan = await self._recall_prepare(
            request,
            current_context_id=current_context_id,
            current_workspace_id=current_workspace_id,
            context_workspace_id=context_workspace_id,
            context_ids=context_ids,
        )
        request = plan.request
        # #1941: after the binding gate and scope resolution. ``forget(query=...)``
        # finds its candidates through recall and must keep working on a locked
        # workspace — it passes False.
        if capacity_gate:
            await self._ensure_capacity(
                user_id, context_ids=(current_context_id, *(context_ids or ()))
            )
        effective_workspace_id = plan.effective_workspace_id
        is_shared_context_read = plan.is_shared_context_read
        neural_enabled = plan.neural_enabled
        search_config = plan.search_config

        # Issue #496: pre-resolve the ``analysis_cluster`` filter. A non-None
        # return means cluster scoping is active; an empty list means the
        # cluster is empty or unknown, so short-circuit before paying for the
        # embedding + search round-trip.
        cluster_memory_ids = await self._recall_resolve_cluster_filter(
            request, effective_workspace_id=effective_workspace_id
        )
        if cluster_memory_ids is not None and not cluster_memory_ids:
            return await self._empty_recall_response(
                request=request,
                selection_config=selection_config,
                search_config=search_config,
                context_id=current_context_id,
                timings=timings,
                user_id=user_id,
                context_count=context_count,
            )

        # #708 Option A H1 gate (deferred): we now know hybrid_search will
        # actually fire (the analysis_cluster short-circuit above did not
        # trigger), so the BYOK probe can run without falsely 404'ing no-cost
        # reads.
        if is_shared_context_read:
            await self._recall_enforce_shared_byok(
                request,
                user_id=user_id,
                current_context_id=current_context_id,
                current_workspace_id=current_workspace_id,
                context_workspace_id=context_workspace_id,  # type: ignore[arg-type]
            )

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
        # #1503: opt-in tolerance for writer-side tag drift. Widens the filter to
        # the stored spellings that are MECHANICAL variants (case / separators /
        # plural) of what was asked for; exact semantics stay the default. Only
        # single-context recall — the vocabulary read is per (workspace, context)
        # and the caller has authorized exactly this one.
        effective_filters = request.filters
        tag_filter = self._requested_tag_filter(request.filters)
        if request.filters and TAG_GROUPS_FILTER_KEY in request.filters:
            # #2071: internal, set only from the widening below.
            effective_filters = {
                k: v for k, v in request.filters.items() if k != TAG_GROUPS_FILTER_KEY
            }
        if (
            tag_filter
            and request.filters.get("tags_normalize")
            and current_context_id
            and not context_ids  # one context's vocabulary cannot widen a search over many
        ):
            from services.tag_resolution import expand_tag_filter

            # #2096: the stores match at most MAX_TAG_GROUPS requested tags, so
            # only those are widened; a tag past the cap never joins a group.
            widened = list(dict.fromkeys(tag_filter))[:MAX_TAG_GROUPS]
            expanded, added = await expand_tag_filter(
                self.db,
                workspace_id=effective_workspace_id,
                context_id=current_context_id,
                user_id=user_id,
                tags=widened,
            )
            if added:
                effective_filters = {**effective_filters, "tags": expanded}
                logger.info(
                    "recall_tag_filter_expanded",
                    requested=len(widened),
                    expanded=len(expanded),
                )
            if request.filters.get("tags_match") == "all":
                # #2058 / #2071: 'all' ANDs one group per requested tag; the flat
                # list cannot tell which entries were asked for. Set even when
                # nothing was added, so listed spellings of one tag still merge.
                effective_filters = {
                    **effective_filters,
                    TAG_GROUPS_FILTER_KEY: tag_filter_groups(widened, expanded),
                }
            elif added:
                # #2096: the builders cut a flat 'any' list to 50 entries, so the
                # added spellings pushed later requested tags out. They OR the
                # groups instead, capped per requested tag.
                effective_filters = {
                    **effective_filters,
                    TAG_GROUPS_FILTER_KEY: tag_filter_groups_any(widened, added),
                }

        # #1515: hybrid_search reports here when it had to serve the request
        # without the semantic arm. Stays empty on the happy path.
        degradation: dict[str, Any] = {}

        search_results = await self.search_service.hybrid_search(
            query=request.query,
            user_id=user_id,
            workspace_id=str(effective_workspace_id),
            context_id=plan.search_context_id,
            k=candidates_k,
            use_rerank=request.use_rerank,
            filters=effective_filters,
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
            degradation=degradation,
            timings=timings,
        )

        # Get full memory data from PostgreSQL
        memory_ids = [r["id"] for r in search_results]

        if not memory_ids:
            return await self._empty_recall_response(
                request=request,
                selection_config=selection_config,
                search_config=search_config,
                context_id=current_context_id,
                degradation=degradation,
                timings=timings,
                user_id=user_id,
                context_count=context_count,
            )

        # Fetch memories from PostgreSQL (exclude soft-deleted), applying the
        # #214 source post-filters, the #887 trust-tier filter and the #496
        # cluster scope. Keyed by both the row id and the Qdrant point id (#972).
        with timings.stage("hydrate"):
            memories = await self._recall_hydrate_memories(
                request,
                memory_ids=memory_ids,
                cluster_memory_ids=cluster_memory_ids,
            )

        # #1299: per-memory type/source binding filter over the FULL candidate
        # pool, before shadowing / rerank / the top-k slice.
        memories, binding_row_filtered = await self._recall_filter_rows_by_binding(
            search_results, memories, user_id=user_id
        )

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
        # #2123: judge-verified older versions of a fact without an edge fold
        # under the newer one — same altitude as shadowing, so the re-rank,
        # the selection plan and the top-k slice see the folded pool.
        # #2131: the fold's resolver answer, handed to the page builder so a
        # judged row is not resolved twice.
        resolved_candidates: dict[UUID, SupersedeCandidate | None] = {}
        fold_map = await self._apply_supersede_fold(
            search_results,
            memories,
            include_superseded=request.include_superseded,
            user_id=user_id,
            trusted_only=_is_trusted_recall(request),
            shadow_map=shadow_map,
            resolved=resolved_candidates,
            # #2027: the filters the rows matched with, widened tags included.
            filters=effective_filters,
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
            # #1306: every subtractive gate has run, so the pool is exactly the
            # authorized eligible pool for this query. Stamps evidence and
            # reorders search_results in place.
            selection_evidence = await self._recall_apply_selection_plan(
                search_results,
                memories,
                selection_config=selection_config,
                request=request,
                search_config=search_config,
                context_id=current_context_id,
            )

        with timings.stage("finalize"):
            responses = await self._recall_build_responses(
                request,
                search_results,
                memories,
                shadow_map=shadow_map,
                contradiction_map=contradiction_map,
                user_id=user_id,
                fold_map=fold_map,
                content_trusted_only=content_trusted_only,
                resolved_candidates=resolved_candidates,
                filters=effective_filters,
            )

        # #2144: the opt-in LLM selector over the page that would ship — after
        # fold, rerank, the selection plan and the #2124 content rule, before
        # the commit, so access stats and the audit row describe the selected
        # page. None (no ``selector`` key) unless the stage ran or was refused.
        built_page = responses
        responses, selector_report = await self._recall_apply_llm_selector(
            request,
            responses,
            llm_selector=llm_selector,
            search_config=search_config,
            cross_context=bool(context_ids),
            selection_config=selection_config,
            degradation=degradation,
            user_id=user_id,
            context_id=current_context_id,
            effective_workspace_id=effective_workspace_id,
            timings=timings,
        )
        # Rows the selector judged irrelevant do not feed the Hebbian pass.
        # confidence keeps reading the whole pool: it is the search's signal,
        # and selector.dropped reports what the model removed.
        hebbian_pool = search_results
        if selector_report is not None and selector_report.applied and selector_report.dropped:
            hebbian_pool = _without_dropped_rows(search_results, memories, built_page, responses)

        # Hebbian learning: build graph for explore() (best-effort, does not affect recall)
        # #1515: the `!= "keyword"` clause already says keyword-only results must
        # not feed the graph — a degraded hybrid recall IS keyword-only, so it has
        # to be excluded on the same grounds. The semantic gates downstream fail
        # OPEN without embeddings: co_activation and hebbian both skip their cosine
        # check (and with it the #983 repetition gate) when a node has no vector,
        # and a raw BM25 score clamps to full activation. The resulting edges are
        # written into the persistent graph and outlive the outage.
        with timings.stage("commit"):
            await self.db.commit()

        # #1985: the statistics writes run AFTER the read commits, each in its
        # own session, so a recall never holds memory or edge row locks while
        # it waits for another one, and a failure there cannot undo or fail
        # the page it already has. They do delay the response (#1989
        # post_commit_ms).
        client = request.filters.get("client", "api") if request.filters else "api"
        with timings.stage("post_commit"):
            await self._recall_record_access(responses, memories, client=client)

        if neural_enabled and request.search_mode != "keyword" and not degradation.get("degraded"):
            with timings.stage("post_commit"):
                await self._recall_run_hebbian_learning(
                    request,
                    hebbian_pool,
                    memories,
                    user_id=user_id,
                    current_context_id=current_context_id,
                    effective_workspace_id=effective_workspace_id,
                )

        return await self._recall_finalize(
            request,
            search_results,
            responses,
            user_id=user_id,
            current_context_id=current_context_id,
            effective_workspace_id=effective_workspace_id,
            neural_enabled=neural_enabled,
            binding_row_filtered=binding_row_filtered,
            selection_evidence=selection_evidence,
            cross_context=bool(context_ids),
            degradation=degradation,
            timings=timings,
            context_count=context_count,
            selector=selector_report,
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
        await self._ensure_capacity(user_id, workspace_id=workspace_id_str)

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

    @staticmethod
    def _guardrail_item(row: Any, user_id: str, *, tool_trigger: Any, l2: bool) -> GuardrailItem:
        """Project one partial row onto the shared ``GuardrailItem`` shape.

        ``tool_trigger`` arrives as the projected ``details->'tool_trigger'``
        JSON element for the tool-triggered lane (``None`` for pinned rows);
        a driver that hands the ``json`` element back as text is decoded here
        so the item always carries an object. The SQL predicate selects any
        non-NULL element, so a legacy non-object value is possible too — a bare
        JSON string such as ``"Bash"`` reaches this code as the ``str`` ``Bash``
        and does not decode. That is "not a trigger" (``tool_trigger: null``,
        which the contract tells consumers to skip), never an aborted read.
        ``l2`` keeps ``context_summary`` on pinned items only.
        """
        import json

        if isinstance(tool_trigger, str):
            try:
                tool_trigger = json.loads(tool_trigger)
            except ValueError:  # json.JSONDecodeError
                tool_trigger = None
        return GuardrailItem(
            memory_id=row.id,
            summary=row.summary,
            context_summary=row.context_summary if l2 else None,
            type=row.type,
            importance=row.importance,
            delivery_mode=row.delivery_mode,
            tool_trigger=tool_trigger if isinstance(tool_trigger, dict) else None,
            source_type=row.source_type,
            authored_by_caller=row.user_id == user_id,
            created_at=row.created_at,
            updated_at=row.updated_at or row.created_at,
        )

    async def load_guardrails(
        self,
        user_id: str,
        current_context_id: UUID | None = None,
        current_workspace_id: UUID | None = None,
        cap: int | str | None = None,
        key_workspace_id: UUID | None = None,  # Issue #963/#1281: pure key scope
    ) -> LoadGuardrailsResponse:
        """Deterministically load a context's guardrail set for a client-side hook.

        ``load_pinned``'s twin (same isolation gate, cap clamp, binding row
        filter and audit emission), serving two independently capped lanes:

        * ``pinned`` — ``list_pinned(trusted_only=True)`` verbatim, bounded by
          ``settings.pinned_load_cap``, so the pinned half is byte-identical to
          the agent-bootstrap pinned lane;
        * ``tool_triggered`` — ``list_tool_triggered`` (rows carrying
          ``details.tool_trigger``), bounded by ``cap`` (default
          ``settings.guardrail_load_cap``).

        Two lanes, two caps: one union query with one cap would let a large
        pinned set evict every guardrail from a capped response (and let a
        member evict them on purpose with high-importance pins). ``cap``
        therefore applies to the tool-triggered list only. The top-level
        ``total_available`` / ``truncated`` / ``cap`` keep one unambiguous
        meaning (sum / either lane / the tool-triggered cap) and the per-lane
        fields say which protection is incomplete. A row that is both pinned
        and tool-triggered appears in both lists.

        The trust gate is unconditional on both lanes — there is no parameter
        to turn it off from any surface. The server never compiles or runs a
        stored pattern here; ``tool_trigger`` is returned as data. Plain SQL:
        no embedding, no vector search, no Hebbian write.

        ``total_available`` counts are the repo (pre-binding) counts, as in
        ``load_pinned``: the context's set size, while the binding filter
        narrows what THIS credential receives. ``version`` is computed AFTER
        the filter, so it is per-credential.

        Args:
            user_id: Caller user ID.
            current_context_id: Bound context (the guardrail set is per-context).
            current_workspace_id: Bound workspace (isolation scope).
            cap: Optional override for the tool-triggered cap (defaults to settings).
            key_workspace_id: Pure API-key workspace scope, or ``None``.

        Returns:
            LoadGuardrailsResponse with both ordered lists, caps and flags.
        """
        from config.settings import get_settings
        from utils.tool_trigger import GUARDRAIL_FORMAT, guardrail_version

        context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id,
            current_context_id,
            key_workspace_id=key_workspace_id,
            operation="load_guardrails",
        )
        if not workspace_id_str or not context_id_str:
            raise ValueError("load_guardrails() requires current_context_id")
        # #1941: no capacity check — guardrails stay available on a locked
        # workspace (see mcp_server/tools/_capacity_gate.py).

        settings = get_settings()
        pinned_cap = self._clamp_pinned_cap(None, settings.pinned_load_cap)
        guardrail_cap = self._clamp_pinned_cap(cap, settings.guardrail_load_cap)
        ws_uuid, ctx_uuid = UUID(workspace_id_str), UUID(context_id_str)

        pinned_rows, pinned_total = await self.memory_repo.list_pinned(
            ws_uuid, ctx_uuid, pinned_cap, trusted_only=True
        )
        tool_rows, tool_total = await self.memory_repo.list_tool_triggered(
            ws_uuid, ctx_uuid, guardrail_cap
        )
        pinned_truncated = pinned_total > pinned_cap
        tool_truncated = tool_total > guardrail_cap

        from services.agent_binding_service import filter_memory_rows_by_binding

        pinned_rows, pinned_filtered = await filter_memory_rows_by_binding(
            self.db, list(pinned_rows), operation="load_guardrails", user_id=user_id
        )
        tool_rows, tool_filtered = await filter_memory_rows_by_binding(
            self.db, list(tool_rows), operation="load_guardrails", user_id=user_id
        )
        if pinned_truncated or tool_truncated:
            # Never a silent truncation: a client hook fails open on it.
            logger.warning(
                "guardrail_load_capped",
                context_id=context_id_str,
                workspace_id=workspace_id_str,
                pinned_total_available=pinned_total,
                pinned_cap=pinned_cap,
                tool_triggered_total_available=tool_total,
                cap=guardrail_cap,
            )

        pinned_items = [
            self._guardrail_item(m, user_id, tool_trigger=None, l2=True) for m in pinned_rows
        ]
        tool_items = [
            self._guardrail_item(m, user_id, tool_trigger=m.tool_trigger, l2=False)
            for m in tool_rows
        ]
        version = guardrail_version(
            [
                [str(i.memory_id), i.summary, i.importance, i.delivery_mode, i.tool_trigger]
                for i in (*pinned_items, *tool_items)
            ]
        )

        # Append-only audit (no-op unless verified agent identity); the
        # enforce-mode row-filter counts ride the success row.
        from services.agent_binding_service import ROW_FILTER_KIND
        from services.memory_access_event_writer import emit_memory_access_event

        filtered = pinned_filtered + tool_filtered
        await emit_memory_access_event(
            operation="load_guardrails",
            outcome="success",
            workspace_id=ws_uuid,
            user_id=user_id,
            context_id=ctx_uuid,
            result_count=len(pinned_items) + len(tool_items),
            extra_metadata=(
                {"filter_kind": ROW_FILTER_KIND, "binding_row_filtered_count": filtered}
                if filtered
                else None
            ),
        )

        return LoadGuardrailsResponse(
            format=GUARDRAIL_FORMAT,
            version=version,
            pinned=pinned_items,
            tool_triggered=tool_items,
            total_available=pinned_total + tool_total,
            truncated=pinned_truncated or tool_truncated,
            cap=guardrail_cap,
            pinned_cap=pinned_cap,
            pinned_total_available=pinned_total,
            pinned_truncated=pinned_truncated,
            tool_triggered_total_available=tool_total,
            tool_triggered_truncated=tool_truncated,
        )

    async def _delete_memory_point(self, user_id: str, memory: Memory) -> None:
        """Remove ``memory``'s vector point, unless another live row still owns it.

        A resource-ingested memory's point id is ``uuid5(resource:doc:version)``
        (#1829): the same document indexed into two contexts that share one
        collection yields ONE point for TWO rows. Deleting it under the other
        row would leave that memory unsearchable, so the point goes only when
        no other live row names it. Memories the API wrote have their own
        point (``point_id == id``) and skip the check.
        """
        point_id = await self._point_to_delete(memory)
        if point_id is None:
            return
        del_collection = await resolve_collection_name(self.db, memory.context_id)
        await delete_memory_from_qdrant(user_id, point_id, collection_name=del_collection)

    async def _point_to_delete(self, memory: Memory) -> UUID | None:
        """``memory``'s vector point id, or ``None`` when another live row owns it too."""
        from services.resource_indexer import unshared_point_id

        return await unshared_point_id(self.db, memory.id, memory.point_id)

    async def _soft_delete_memory(
        self,
        user_id: str,
        memory: Memory,
        workspace_id: str,
        context_id: str,
        *,
        delete_point: bool = True,
    ) -> UUID | None:
        """Delete one memory the caller may delete: ``forget``'s by-id side effects.

        Soft-deletes the row, removes its search point and its neural edges
        (3-level isolation). Shared by ``forget(memory_id=...)`` and
        ``forget_bulk`` so the two can never drift. The caller commits.

        With ``delete_point=False`` the point is not deleted here: its id is
        returned (``None`` when another live row still owns it) so a batch
        caller can remove a whole chunk's points in one request.
        """
        # Soft delete in PostgreSQL (set deleted_at, deleted_by)
        memory.deleted_at = utcnow()
        memory.deleted_by = user_id
        # #2044: the forgetting writer's agent / session, in the same UPDATE.
        provenance = current_provenance()
        stamp_writer(memory, provenance)
        # touch=False: the delete is dated by deleted_at, not
        # updated_at — else changes_since lists an "updated" event
        # after the "forgotten" one (#1924).
        await self.memory_repo.update(memory.id, memory, touch=False)
        # #2043: the forgotten event, at the version it was forgotten at (a
        # forget is not an edit: no bump). ``update`` flushed the UPDATE — which
        # waits for, then holds, the row lock — and refreshed the row, so
        # ``memory.version`` is the committed one even if an edit landed after
        # forget's unlocked read.
        record_revision(
            self.db,
            memory,
            kind=REVISION_KIND_FORGOTTEN,
            actor_user_id=user_id,
            at=memory.deleted_at,
            provenance=provenance,
        )

        # Hard delete from Qdrant (remove from search index) — by the
        # row's point id, not its row id (#1829).
        point_to_delete: UUID | None = None
        if delete_point:
            await self._delete_memory_point(user_id, memory)
        else:
            point_to_delete = await self._point_to_delete(memory)

        # Clean up neural memory edges with 3-level isolation
        from repositories.neural_edge import NeuralEdgeRepository

        edge_repo = NeuralEdgeRepository(self.db)
        edges_deleted = await edge_repo.delete_node_edges(
            user_id=user_id,
            node_id=memory.id,
            workspace_id=workspace_id,
            context_id=context_id,
        )
        if edges_deleted > 0:
            logger.info(
                "neural_edges_cleaned",
                memory_id=str(memory.id),
                edges_deleted=edges_deleted,
                user_id=user_id,
            )
        return point_to_delete

    async def _soft_delete_chunk(
        self,
        user_id: str,
        ids: list[UUID],
        workspace_id: str,
        context_id: str,
        *,
        writer: str = REVISION_WRITER_API,
    ) -> tuple[list[UUID], list[str]]:
        """``_soft_delete_memory`` for a chunk, in a fixed number of statements.

        One UPDATE stamps ``deleted_at`` / ``deleted_by`` and the writer's
        agent / session (#2044) — nothing else: ``updated_at`` stays, #1924 —
        on the chunk's still-live rows of the context; one multi-row INSERT
        logs their ``forgotten`` events (#2043; ``writer`` is ``rollback``
        when ``rollback_session`` forgets what a session created); one SELECT
        keeps every shared point another live row still owns (#1829); one
        DELETE drops the deleted nodes' neural edges with forget's (user,
        workspace, context) isolation. Returns the deleted ids and the point
        ids to remove from the index. The caller deletes the points and
        commits.
        """
        from sqlalchemy import insert, update

        from repositories.neural_edge import NeuralEdgeRepository

        if not ids:
            return [], []
        deleted_at = utcnow()
        provenance = current_provenance()
        rows = (
            await self.db.execute(
                update(Memory)
                .where(
                    Memory.id.in_(ids),
                    Memory.context_id == UUID(context_id),
                    Memory.deleted_at.is_(None),
                )
                .values(
                    deleted_at=deleted_at,
                    deleted_by=user_id,
                    agent_id=provenance.agent_id,
                    session_id=provenance.session_id,
                )
                .returning(Memory.id, Memory.summary_embedding_id, Memory.version)
                .execution_options(synchronize_session=False)
            )
        ).all()
        deleted = [row.id for row in rows]
        if rows:
            # #2043: the rows' forgotten events, at the versions the UPDATE
            # returned (it holds the row locks, so they are the committed ones).
            await self.db.execute(
                insert(MemoryRevision).values(
                    [
                        revision_values(
                            memory_id=row.id,
                            context_id=UUID(context_id),
                            version=row.version,
                            kind=REVISION_KIND_FORGOTTEN,
                            actor_user_id=user_id,
                            at=deleted_at,
                            writer=writer,
                            provenance=provenance,
                        )
                        for row in rows
                    ]
                )
            )
        points = {row.id: (row.summary_embedding_id or row.id) for row in rows}
        shared = {p for mid, p in points.items() if p != mid}
        kept: set[UUID | None] = set()
        if shared:
            kept = set(
                (
                    await self.db.execute(
                        select(Memory.summary_embedding_id).where(
                            Memory.summary_embedding_id.in_(shared),
                            Memory.deleted_at.is_(None),
                        )
                    )
                ).scalars()
            )
        await NeuralEdgeRepository(self.db).delete_nodes_edges(
            user_id=user_id, node_ids=deleted, workspace_id=workspace_id, context_id=context_id
        )
        return deleted, [str(p) for p in dict.fromkeys(points.values()) if p not in kept]

    async def forget_bulk(
        self,
        user_id: str,
        *,
        context_id: UUID,
        created_before: datetime | None = None,
        created_after: datetime | None = None,
        memory_type: str | None = None,
        tags: list[str] | None = None,
        dry_run: bool = True,
        key_workspace_id: UUID | None = None,
        chunk_size: int = FORGET_BULK_CHUNK,
        max_rows: int = FORGET_BULK_MAX_PER_REQUEST,
        after: tuple[datetime, UUID] | None = None,
    ) -> tuple[int, bool, tuple[datetime, UUID] | None]:
        """Delete every live memory in one context that matches the filters (#1941).

        The permission model is ``forget``'s, applied to each candidate: the
        context gate (``_get_context_isolation_params`` with ``access="write"``,
        uniform 404), then ``can_access_memory(access="write")`` — evaluated
        once per distinct (author, type, source) group, since the answer
        depends on nothing else — and the guardrail author gate. A row the
        caller may not delete is skipped, exactly as forget skips it, and is
        not counted.

        Not gated by the capacity lock: deleting is how a locked workspace
        gets back under its cap.

        Args:
            user_id: Caller.
            context_id: The context to delete from.
            created_before / created_after: ``created_at`` bounds (exclusive /
                inclusive), naive UTC.
            memory_type: Exact ``type``.
            tags: Rows carrying ANY of these tags.
            dry_run: Count only.
            key_workspace_id: Pure key scope (#963), as for forget.
            chunk_size: Rows deleted and committed per batch.
            max_rows: Rows one request deletes at most; the caller repeats
                the request while ``remaining`` is true.
            after: Resume point from the previous request's ``next_after``
                (``(created_at, id)`` of the last row it deleted), so rows
                this caller may not delete are not rescanned every request.
                Ignored on a dry run.

        Returns:
            ``(count, remaining, next_after)``: on a dry run the FULL matched
            count, ``False`` and ``None``; otherwise the number deleted by
            this request, whether matching rows are left, and where the next
            request resumes (``None`` when nothing remains).

        Each chunk is committed, its search points removed in one request
        (collection resolved once), and audited on its own — an interrupted
        request leaves an audit row for every chunk it did delete.
        """
        from services.permission_service import CallerId, MemoryAuthorId, PermissionService

        _context, workspace_id_str, context_id_str = await self._get_context_isolation_params(
            user_id,
            context_id,
            access="write",
            key_workspace_id=key_workspace_id,
            operation="forget",
        )
        if not workspace_id_str or not context_id_str:
            raise NotFoundException("Context", str(context_id))

        conditions = [Memory.context_id == context_id, Memory.deleted_at.is_(None)]
        if created_before is not None:
            conditions.append(Memory.created_at < created_before)
        if created_after is not None:
            conditions.append(Memory.created_at >= created_after)
        if memory_type is not None:
            conditions.append(Memory.type == memory_type)
        if tags:
            conditions.append(Memory.tags.overlap(tags))
        perm = PermissionService(self.db)
        allowed_groups: dict[tuple[str, str, str | None], bool] = {}
        guardrail_ok: bool | None = None

        async def permitted(author: str, mtype: str, source: str | None, guard: bool) -> bool:
            """forget's per-memory decision, which depends only on these four."""
            nonlocal guardrail_ok
            group = (author, mtype, source)
            if group not in allowed_groups:
                allowed_groups[group] = await perm.can_access_memory(
                    user_id=CallerId(user_id),
                    memory_user_id=MemoryAuthorId(author),
                    workspace_id=UUID(workspace_id_str),
                    context_id=context_id,
                    access="write",
                    operation="forget",
                    memory_type=mtype,
                    memory_source_type=source,
                )
            if not allowed_groups[group]:
                return False
            if guard:
                if guardrail_ok is None:
                    from utils.exceptions import AuthorizationError

                    try:
                        await self._require_guardrail_author(user_id, context_id)
                        guardrail_ok = True
                    except (AuthorizationError, ValueError):
                        guardrail_ok = False
                return guardrail_ok
            return True

        is_guardrail = tool_triggered_predicate().label("is_guardrail")
        if dry_run:
            # Counted per permission group in SQL: no row is materialised.
            groups = (
                await self.db.execute(
                    select(
                        Memory.user_id,
                        Memory.type,
                        Memory.source_type,
                        is_guardrail,
                        func.count().label("n"),
                    )
                    .where(*conditions)
                    .group_by(Memory.user_id, Memory.type, Memory.source_type, is_guardrail)
                )
            ).all()
            matched = 0
            for g in groups:
                if await permitted(g.user_id, g.type, g.source_type, bool(g.is_guardrail)):
                    matched += int(g.n)
            return matched, False, None

        # A real delete reads at most max_rows + 1 permitted candidates, a page
        # at a time (keyset on created_at, id) so rows the permission check
        # drops do not shrink the batch.
        victims: list[tuple[UUID, datetime]] = []
        page = max_rows + 1
        # ``after`` starts at the caller's resume point, then follows each page.
        while len(victims) <= max_rows:
            stmt = (
                select(
                    Memory.id,
                    Memory.created_at,
                    Memory.user_id,
                    Memory.type,
                    Memory.source_type,
                    is_guardrail,
                )
                .where(*conditions)
                .order_by(Memory.created_at, Memory.id)
                .limit(page)
            )
            if after is not None:
                stmt = stmt.where(tuple_(Memory.created_at, Memory.id) > tuple_(*after))
            rows = (await self.db.execute(stmt)).all()
            for row in rows:
                if await permitted(row.user_id, row.type, row.source_type, bool(row.is_guardrail)):
                    victims.append((row.id, row.created_at))
            if len(rows) < page:
                break
            after = (rows[-1].created_at, rows[-1].id)

        from db.qdrant import delete_chunk_points, delete_points_from_qdrant
        from services.memory_access_event_writer import (
            MAX_METADATA_MEMORY_IDS,
            emit_memory_access_event,
        )

        remaining = len(victims) > max_rows
        victims = victims[:max_rows]
        next_after = (victims[-1][1], victims[-1][0]) if remaining and victims else None
        collection = await resolve_collection_name(self.db, context_id)
        deleted_total = 0
        for start in range(0, len(victims), chunk_size):
            chunk = [vid for vid, _ in victims[start : start + chunk_size]]
            deleted, points = await self._soft_delete_chunk(
                user_id, chunk, workspace_id_str, context_id_str
            )
            # Points first, then the commit — single forget's order: a failed
            # point delete raises before this chunk's rows are committed, so
            # no committed row is left with a live search point. Earlier
            # chunks stay committed and audited.
            try:
                await delete_points_from_qdrant(points, collection)
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
            # #2015: their content chunk points too. Fail-soft — the rows are
            # committed tombstones by now, and the orphan sweep removes the
            # chunks of a tombstoned memory after the grace period.
            try:
                await delete_chunk_points(deleted, collection)
            except Exception as exc:
                logger.warning(
                    "content_chunks_delete_failed",
                    collection=collection,
                    memories=len(deleted),
                    error_class=type(exc).__name__,
                )
            deleted_total += len(deleted)
            await emit_memory_access_event(
                operation="forget",
                outcome="success",
                workspace_id=UUID(workspace_id_str),
                user_id=user_id,
                context_id=context_id,
                memory_id=deleted[0] if len(deleted) == 1 else None,
                result_count=len(deleted),
                extra_metadata=(
                    {"memory_ids": [str(mid) for mid in deleted[:MAX_METADATA_MEMORY_IDS]]}
                    if len(deleted) > 1
                    else None
                ),
            )

        logger.info(
            "memories_soft_deleted_in_bulk",
            context_id=context_id_str,
            count=deleted_total,
            remaining=remaining,
            user_id=user_id,
        )
        return deleted_total, remaining, next_after

    async def forget(
        self,
        request: ForgetRequest,
        user_id: str,
        current_context_id: UUID | None = None,
        key_workspace_id: UUID | None = None,  # Issue #963/#1281: pure key scope
        *,
        current_workspace_id: UUID | None = None,
        _skip_binding_row_filter: bool = False,
    ) -> ForgetResponse:
        """Delete memory (single or multiple via query).

        Issue #82: Context-based multi-collection support.

        Args:
            request: Forget request (memory_id or query)
            user_id: User ID
            current_context_id: Current context UUID (Issue #82)
            _skip_binding_row_filter: INTERNAL maintenance callers only
                (#1301) — the upsert replacement delete. Skips the #1299
                per-memory type/source read filter while keeping the
                context-level write gate, so replacing a denied-type row
                cannot silently no-op and leave a live duplicate per
                external_id. Never expose through transport layers.

        Returns:
            ForgetResponse with deleted count and IDs
        """
        if _skip_binding_row_filter and not request.memory_id:
            raise ValueError(
                "_skip_binding_row_filter only covers the memory_id branch; "
                "forget(query=...) resolves victims through recall(), where "
                "the flag cannot propagate — combining them would silently "
                "skip the rows the maintenance delete was meant to clean up."
            )
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
                    # #1299: per-memory type/source filter; #1301 lets the
                    # internal upsert replacement delete opt out (context-level
                    # gate above still applies).
                    memory_type=None if _skip_binding_row_filter else memory.type,
                    memory_source_type=None if _skip_binding_row_filter else memory.source_type,
                )

                if not can_access:
                    logger.warning(
                        "forget_access_denied",
                        memory_id=str(request.memory_id),
                        user_id=user_id,
                    )
                    # Return empty response instead of error for security
                    return ForgetResponse(deleted_count=0, memory_ids=[])
                # Tool guardrails: context EDITOR or above (see
                # _may_delete_guardrail); a deny is the silent deleted_count=0.
                if not await self._may_delete_guardrail(user_id, memory):
                    return ForgetResponse(deleted_count=0, memory_ids=[])
                # Migration 063: Get workspace_id/context_id from memory directly
                # CRITICAL: a row without a context cannot be placed (workspace_id is NOT NULL, e95)
                if not memory.context_id:
                    raise ValueError(
                        f"Memory {memory.id} has no context_id (workspace_id is NOT NULL "
                        "since e95, #1951); the row cannot be placed in a collection."
                    )

                memory_workspace_id = str(memory.workspace_id)
                memory_context_id = str(memory.context_id)
                deleted_workspace_ids.add(memory.workspace_id)
                deleted_context_ids.add(memory.context_id)

                await self._soft_delete_memory(
                    user_id, memory, memory_workspace_id, memory_context_id
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
                full=0,  # #2124: only the ids are used
            )

            # Issue #82: Pass project ID to recall
            # recall() requires the workspace as well as the context: the
            # caller's session workspace (what every other recall caller
            # passes — recall routes a shared read from it), else the
            # context's own when the caller has none.
            search_response = await self.recall(
                recall_request,
                user_id,
                current_context_id,
                current_workspace_id=(
                    current_workspace_id or (UUID(workspace_id_str) if workspace_id_str else None)
                ),
                capacity_gate=False,
            )

            # #1515: the pin above says the router must never choose the
            # candidate set of a destructive operation — and degradation would
            # do exactly that, underneath the pin. A keyword-only candidate set
            # is a materially DIFFERENT set of memories, and this loop hard-
            # deletes Qdrant points and neural edges, neither of which has a
            # recovery path. Read paths trade precision for availability; a
            # delete must not. Restore the pre-#1515 failure instead.
            if search_response.degraded:
                raise ExternalServiceError(
                    "search",
                    "Refusing to delete by query: the search was degraded "
                    f"({search_response.degraded_reason}), so the candidate set "
                    "is keyword-only and not the set this query would normally "
                    "match. Retry once the embedding provider and vector store "
                    "are healthy.",
                )

            # Resolve and validate every match BEFORE any side effect, so a
            # request either deletes all of its matches or touches nothing: a
            # legacy row with NULL workspace/context (the by-id path refuses
            # it too) must not abort the sweep after earlier rows lost their
            # search points but before the commit.
            victims: list[Memory] = []
            for memory_response in search_response.results:
                memory = await self.memory_repo.get(memory_response.memory_id)
                if not memory:
                    continue
                # Tool guardrails: the same author gate as the by-id branch
                # — a query sweep must not delete a guardrail a member may
                # not delete one by one. Skipped rows are simply not counted.
                if not await self._may_delete_guardrail(user_id, memory):
                    continue
                if not memory.context_id:
                    raise ValueError(
                        f"Memory {memory.id} has no context_id (workspace_id is NOT NULL "
                        "since e95, #1951); the row cannot be placed in a collection."
                    )
                victims.append(memory)

            for memory in victims:
                # The by-id path's helper: same #1924 stamp, same shared
                # point rule, same 3-level edge isolation.
                await self._soft_delete_memory(
                    user_id, memory, str(memory.workspace_id), str(memory.context_id)
                )
                deleted_ids.append(memory.id)
                deleted_workspace_ids.add(memory.workspace_id)
                deleted_context_ids.add(memory.context_id)

            logger.info(
                "memories_soft_deleted_by_query",
                **query_log_fields(request.query),  # #1721: no query text
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

            # Delete from Qdrant — by the row's point id (#1829)
            await self._delete_memory_point(user_id, memory)

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
        # #1401: "explore" is now in the MAE_OPERATIONS vocabulary (e74_1401),
        # so thread it (with the requested memory_id) here too. This seed gate
        # authorizes the seed's OWN stored context — a second deny surface
        # distinct from the declared-context pre-gate in handle_explore — so an
        # enforce-mode binding / #1299 row-filter deny here must persist its
        # audit row, exactly like the reference / forget-by-id siblings.
        can_access = await perm_service.can_access_memory(
            user_id=CallerId(user_id),
            memory_user_id=MemoryAuthorId(seed_memory.user_id),
            workspace_id=seed_memory.workspace_id,
            context_id=seed_memory.context_id,
            operation="explore",
            memory_id=request.memory_id,
            memory_type=seed_memory.type,  # #1299: per-memory type/source filter
            memory_source_type=seed_memory.source_type,
        )

        if not can_access:
            # Two-arg form — byte-identical message to the not-found raise
            # above (uniform 404; no existence oracle, and no doubled
            # "not found not found" from the constructor's own suffix).
            raise NotFoundException("Memory", str(request.memory_id))

        # #1941: after the access check, so the lock is no existence oracle.
        await self._ensure_capacity(user_id, workspace_id=seed_memory.workspace_id)

        # Migration 063: Get workspace_id and context_id directly from seed memory
        # CRITICAL: the seed needs a context to be placed (workspace_id is NOT NULL, e95)
        if not seed_memory.context_id:
            raise ValueError(
                f"Seed memory {seed_memory.id} has no context_id (workspace_id is NOT NULL "
                "since e95, #1951); the row cannot be placed in a collection."
            )

        if not current_context_id:
            current_context_id = seed_memory.context_id
        if not current_workspace_id:
            current_workspace_id = seed_memory.workspace_id

        # 2. Load user's graph
        graph_repo = GraphRepository(self.db)
        await graph_repo.get_or_create(user_id)

        # 3. Convert to GraphService with 3-level isolation. #1834: inside a
        # private context the graph is read for the whole identity-link set
        # (the accounts that own it together), so a seed written by a linked
        # account is in the graph and the spread crosses its edges; shared
        # contexts and every write stay per account.
        owner_ids = await link_set_reads(
            self.db, user_id, await self._is_private_context(current_context_id)
        )
        graph_service = GraphService(
            user_id=user_id,
            db=self.db,
            workspace_id=str(current_workspace_id) if current_workspace_id else None,
            context_id=str(current_context_id) if current_context_id else None,
            owner_ids=owner_ids,
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
            user_id=graph_service.read_owner,
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

        # #1985: one lock-ordered statement for the seed and the whole page, so
        # concurrent explores (and recalls) over overlapping rows queue instead
        # of deadlocking on per-row locks taken in activation order.
        await self.memory_repo.bulk_update_access_stats(
            [seed_memory.id, *(related.memory_id for related in related_memories)],
            client="api",
        )
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
            if context_id:
                # Private context: the creator's, an account linked to it
                # included (#1784). The caller's access to the context was
                # checked by the route.
                base_conditions.append(owned_by(Memory.user_id, user_id))
            else:
                # No context: the caller's own id only — a link never reaches
                # into a workspace the caller is not a member of.
                base_conditions.append(Memory.user_id == user_id)

        # Single Collection Migration: Filter by workspace_id and context_id
        if workspace_id:
            base_conditions.append(Memory.workspace_id == UUID(workspace_id))
        if context_id:
            base_conditions.append(Memory.context_id == UUID(context_id))

        # #1301: per-memory type/source binding filter, SQL form. Every
        # aggregate below shares base_conditions, so an enforce-mode agent
        # sees counts over exactly the rows it can read — by_type must not be
        # an existence oracle over denied types. Covers the REST /memory/stats
        # route and MCP get_context_info by construction (service layer,
        # #1291/#1292 parity). None for non-agent / shadow scopes.
        from services.agent_binding_service import binding_memory_sql_predicate

        binding_predicate = await binding_memory_sql_predicate(self.db)
        if binding_predicate is not None:
            base_conditions.append(binding_predicate)

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

        # #1834: in a private context the results include a linked account's
        # memories, so their degrees count the link set's edges as well.
        degree_owner: str | frozenset[str] = (
            await link_set_reads(self.db, user_id, await self._is_private_context(context_id))
            or user_id
        )

        degree_map: dict[UUID, int] = {}
        for resp in top_n:
            try:
                in_deg, out_deg = await edge_repo.get_node_degree(degree_owner, resp.memory_id)
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


# #1403: near-duplicate threshold above which the nearest k-NN neighbor of a
# freshly-embedded memory is surfaced as a supersede candidate — the user is
# likely storing an updated version of an existing fact. Well above the
# calibrated per-model seed threshold so the signal fires only on true
# near-duplicates, not merely related memories.
#
# Operating point 0.85 (option-B de-risk, 2026-07-20): on the frozen
# update_slice_v2 corpus (100 ground-truth v1→v2 supersede pairs, measured with
# the same embedder the k-NN seeding uses), the "top-1 neighbor >= tau" detector
# holds precision == recall == 1.000 across the whole band tau = 0.60–0.88
# (true v1 is the top-1 neighbor for 100/100 v2 memories; mean margin 0.249).
# 0.85 is chosen conservatively within that band — not the precision×recall-
# optimal midpoint — to preserve precision when real-world distractors sit
# closer than the minimal-edit corpus pairs, trading a little recall for trust.
# Unlike the seed threshold (D4 calibration/operator-override chain), this is a
# fixed first-pass heuristic; graduate it to a per-model calibrated /
# config-driven value if the adoption telemetry (supersede_candidate_detected
# vs supersede_suggestion_accepted) shows it mis-fires across embedding models
# with different score distributions.
_SUPERSEDE_SUGGEST_THRESHOLD = 0.85


def _supersede_clear_floor(memory: Memory, detect_threshold: float) -> float:
    """The threshold a stored suggestion was admitted under (#2059, #2123).

    A re-embed whose nearest neighbour falls a clear margin below it clears
    the suggestion. A candidate stored at 0.85 or above (unverified, or from
    before the judge was on) answers to 0.85 even in judge mode; one stored
    below 0.85 — only ever on the judge's word — to the judge-mode threshold.
    """
    stored = memory.supersede_candidate
    similarity = stored.get("similarity") if isinstance(stored, dict) else None
    if isinstance(similarity, (int, float)) and similarity >= _SUPERSEDE_SUGGEST_THRESHOLD:
        return _SUPERSEDE_SUGGEST_THRESHOLD
    return detect_threshold


def _supersede_detect_threshold(config: Any) -> float:
    """The post-embed detection threshold for this deployment (#2123).

    ``_SUPERSEDE_SUGGEST_THRESHOLD`` unless the judge is on
    (``supersede_verify_mode == "llm"``): then
    ``supersede_suggest_threshold_llm``, never above the unverified threshold.
    A pair the lower threshold admits is stored only with the judge's answer
    (see :func:`_detect_supersede_candidate`), so the precision of an
    UNVERIFIED candidate still rests on 0.85. ``remember(dedupe="check")``
    keeps 0.85 regardless.
    """
    from services.supersede_verify import VERIFY_MODE_LLM

    if getattr(config, "supersede_verify_mode", None) != VERIFY_MODE_LLM:
        return _SUPERSEDE_SUGGEST_THRESHOLD
    value = getattr(config, "supersede_suggest_threshold_llm", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _SUPERSEDE_SUGGEST_THRESHOLD
    return min(float(value), _SUPERSEDE_SUGGEST_THRESHOLD)


# #2006: (model_name, dimensions) pairs already warned about having no
# ``knn_seed`` calibration row although the D3 bootstrap gate is crossed, so
# the WARNING fires once per process per embedder instead of on every ingest.
_KNN_SEED_UNCALIBRATED_WARNED: set[tuple[str, int]] = set()

# #2010: the last observed value of ``supersede_suggest_enabled`` in this
# process. One INFO line is logged each time the switch is observed off after
# having been on (or at the first ingest), so an operator sees the switch take
# effect without a line per ingest, and a later off→on→off toggle logs again.
_SUPERSEDE_SUGGEST_LAST_SEEN: bool | None = None

# #2023: (model_name, dimensions) pairs whose ``knn_seed_skip_no_consumer``
# (seeding on but uncalibrated, suggestions off: the ingest does no neighbour
# work at all) was already logged at INFO. The first ingest per embedder per
# process logs at INFO so the "nothing happened" state is visible at
# LOG_LEVEL=info; later ones stay at DEBUG to keep it off every ingest.
_KNN_SEED_NO_CONSUMER_REPORTED: set[tuple[str, int]] = set()


async def _source_text_moved(db: AsyncSession, memory: Memory, embedded: str | None = None) -> bool:
    """Re-read ``memory``'s text and candidate column; True when the text moved.

    #2059: the embedding task loaded ``memory`` before embedding it and the
    session does not expire it on commit, so the object still holds the text
    this run embedded. ``update_memory`` can commit a new text (and start a
    second embedding run) while this one is between its search and its write
    — the judge's round trip is seconds long. When the text moved, the newer
    run owns the column: this run's result is about text that no longer
    exists. The column is re-read in the same round trip so the caller can
    re-check a dismissal or an accept committed meanwhile.

    ``embedded`` is the fingerprint of the text this run embedded, taken
    before any earlier re-read in the same run refreshed ``memory`` (#2085).
    Without it the fingerprint comes from ``memory`` as it stands now.
    """
    from services.supersede_verify import text_fingerprint

    if embedded is None:
        embedded = text_fingerprint(memory.summary, memory.content)
    await db.refresh(memory, attribute_names=["supersede_candidate", "summary", "content"])
    return text_fingerprint(memory.summary, memory.content) != embedded


async def _clear_supersede_candidate_without_neighbour(db: AsyncSession, *, memory: Memory) -> None:
    """Clear a live suggestion the re-embed no longer supports (#2059).

    The column holds "the nearest neighbour scored at or above
    ``_SUPERSEDE_SUGGEST_THRESHOLD`` at the last embed". When this embed finds
    no such neighbour — typically an edit moved the memory away from the
    pair, and also a reindex under an embedder that scores the pair lower —
    a stored candidate (and any verdict about the memory's earlier text)
    would otherwise keep surfacing. A tombstone is never cleared: it is the
    caller's rejection (#1504) or ``remember(dedupe="off")``'s permanent
    opt-out, and must hold the next time the pair is a neighbour.
    """
    from services.supersede_dismissal import dismissed_entry

    def live_target(stored: Any) -> str | None:
        """The target id of a live (non-tombstone) suggestion, else None."""
        if not isinstance(stored, dict) or dismissed_entry(stored) is not None:
            return None
        target = stored.get("memory_id")
        return str(target) if target else None

    from sqlalchemy import update
    from sqlalchemy.orm.attributes import set_committed_value

    if live_target(memory.supersede_candidate) is None:
        return
    if await _source_text_moved(db, memory):
        logger.info("supersede_candidate_skipped_source_edited", memory_id=str(memory.id))
        return
    target_id = live_target(memory.supersede_candidate)
    if target_id is None:
        return  # a dismissal or an accept committed meanwhile
    # Conditional on the row still naming that target: a dismissal committed
    # after the re-read above (a tombstone has no top-level memory_id) or a
    # newer candidate must not be wiped. Not a user edit: updated_at stays
    # (#1317), as for the detection write.
    result = await db.execute(
        update(Memory)
        .where(
            Memory.id == memory.id,
            Memory.supersede_candidate["memory_id"].as_string() == target_id,
        )
        .values(supersede_candidate=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if int(getattr(result, "rowcount", 0) or 0) == 0:
        return
    set_committed_value(memory, "supersede_candidate", None)
    logger.info(
        "supersede_candidate_cleared_no_neighbour",
        memory_id=str(memory.id),
        candidate_memory_id=target_id,
    )


async def _supersede_write_preempted(
    db: AsyncSession,
    *,
    memory: Memory,
    edge_repo: Any,
    target_id_str: str,
    raw_top_score: float,
    workspace_id_str: str,
    context_id_str: str,
) -> bool:
    """Re-read the row right before a detection write; True to skip it (#2069).

    The detector decided on ``memory`` as the embedding task loaded it. An
    edit (#2059: the edit's own run owns the column), a dismissal tombstone or
    an accepted ``supersedes`` edge committed since then by ``update_memory``
    / ``create_edge`` must win over this write. The edge is read after the
    row: an accept that lands in between clears the column, and only the edge
    tells that apart from "no candidate". Leaves the re-read column on
    ``memory`` for :func:`_store_supersede_candidate`.
    """
    from models.memory import EDGE_TYPE_SUPERSEDES
    from services.supersede_dismissal import is_dismissed

    memory_id_str = str(memory.id)
    if await _source_text_moved(db, memory):
        logger.info(
            "supersede_candidate_skipped_source_edited",
            memory_id=memory_id_str,
            candidate_memory_id=target_id_str,
        )
        return True
    if is_dismissed(memory.supersede_candidate, target_id=target_id_str, similarity=raw_top_score):
        logger.info(
            "supersede_candidate_suppressed_by_dismissal",
            memory_id=memory_id_str,
            candidate_memory_id=target_id_str,
            similarity=round(raw_top_score, 4),
        )
        return True
    accepted = await edge_repo.get_edge(
        memory.user_id,
        memory.id,
        UUID(target_id_str),
        workspace_id=workspace_id_str,
        context_id=context_id_str,
    )
    return accepted is not None and accepted.edge_type == EDGE_TYPE_SUPERSEDES


async def _store_supersede_candidate(
    db: AsyncSession, *, memory: Memory, candidate: dict[str, Any]
) -> bool:
    """Write ``candidate`` while the row is still as the re-read saw it (#2069).

    Run right after :func:`_supersede_write_preempted`, whose re-read
    ``memory`` holds. The UPDATE applies only while, at the write:

    - the column still holds that value — compared whole, not by target id: a
      tombstone for another target, or one whose pair moved past the
      resurface delta, is overwritten on purpose (#1504), but one committed
      after the re-read is not, nor an accept's clear or a newer run's write.
      "No candidate" also matches the JSON ``null`` rows the #2022 walk
      skipped (the ORM reads both as None);
    - the text is the one this run embedded (an edit's own run owns the
      column, #2059);
    - no ``supersedes`` edge to the target exists (an accept that did not
      touch the column), and the memory is not deleted.

    ``updated_at`` stays: detection is a system annotation, not a user edit
    (#1317). Commit is safe: the caller already committed the
    embedding_status update, so nothing else is pending here.

    Returns:
        True when the row was written; ``memory`` then holds ``candidate``
        as committed, so the seed-edge commits that follow do not flush it
        again.
    """
    from sqlalchemy import Text, update
    from sqlalchemy import cast as sql_cast
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm.attributes import set_committed_value

    from models.memory import EDGE_TYPE_SUPERSEDES, NeuralMemoryEdge

    def md5(value: str) -> str:
        return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()

    seen = memory.supersede_candidate
    if seen is None:
        column_unchanged = or_(
            Memory.supersede_candidate.is_(None),
            sql_cast(Memory.supersede_candidate, Text) == "null",
        )
    else:
        column_unchanged = sql_cast(Memory.supersede_candidate, JSONB) == sql_cast(seen, JSONB)
    accepted = exists().where(
        NeuralMemoryEdge.src_id == memory.id,
        NeuralMemoryEdge.dst_id == UUID(str(candidate["memory_id"])),
        NeuralMemoryEdge.edge_type == EDGE_TYPE_SUPERSEDES,
    )
    result = await db.execute(
        update(Memory)
        .where(
            Memory.id == memory.id,
            Memory.deleted_at.is_(None),
            column_unchanged,
            func.md5(Memory.summary) == md5(memory.summary),
            func.md5(Memory.content) == md5(memory.content),
            ~accepted,
        )
        .values(supersede_candidate=candidate)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if int(getattr(result, "rowcount", 0) or 0) == 0:
        logger.info(
            "supersede_candidate_skipped_row_changed",
            memory_id=str(memory.id),
            candidate_memory_id=str(candidate["memory_id"]),
        )
        return False
    set_committed_value(memory, "supersede_candidate", candidate)
    return True


async def _detect_supersede_candidate(
    db: AsyncSession,
    *,
    memory: Memory,
    raw_top: dict[str, Any],
    edge_repo: Any,
    config: Any,
    workspace_id_str: str,
    context_id_str: str,
    clear_allowed: bool = False,
) -> None:
    """Store ``raw_top`` as ``memory``'s supersede suggestion (#1403, #2016).

    ``clear_allowed`` (#2123): the search returned this memory's own point,
    so a missing near-duplicate is evidence (#2059) — only then may the
    judge-mode skip below 0.85 clear a suggestion stored at 0.85 or above.

    ``raw_top`` is the RAW top-1 neighbour (excluding self) at or above
    ``_SUPERSEDE_SUGGEST_THRESHOLD`` (#2123: or the lower
    ``supersede_suggest_threshold_llm`` with the judge on, in which case a
    pair below 0.85 is stored only with a ``supersedes`` / ``parallel``
    verdict) — INDEPENDENT of the calibrated/operator
    seed-edge threshold. Coupling it to the seed neighbours would let an
    operator seed threshold (knn_seed_min_similarity) > the suggest threshold
    silently suppress a valid suggestion (e.g. a 0.87 near-duplicate filtered
    out before the check). It also runs before the seed-edge idempotency guard:
    a memory created with linked_memory_ids / linked_source_uris / supersedes
    gets a declared edge synchronously (via _create_declared_links) before the
    seeding task runs, so gating detection behind that guard silently skipped it
    for exactly the explicit-link workflow — the case most likely to BE a
    supersede (#1403 review F2). Detection only READS candidates and writes the
    server-only ``supersede_candidate`` column; it creates no edge, so it is
    safe independent of edge state. Seeding runs async (decoupled from the
    remember response), so rather than block remember() the candidate is
    PERSISTED onto the new memory's dedicated server-only column (never the
    client-writable details blob) and surfaced — liveness-guarded — on the next
    recall()/reference(). Suggestion only: the edge is never auto-created (the
    #1208 over-supersede prevention keeps edge authorship with the user/agent,
    who confirms via create_edge).

    #2016: with ``config.supersede_verify_mode == "llm"`` the pair is put to
    the Sleep judge first (``services.supersede_verify``) and its verdict is
    stored beside the candidate; an earlier verdict for the same, unmoved pair
    is reused instead of re-judged. A judge that is off, capped or failing
    leaves the candidate exactly as before. #2059 / #2069: right before the
    write the row is read again, on every path; when this memory's text was
    edited since the task loaded it, nothing is written — the edit's own
    embedding run owns the column — and a dismissal or an accept committed
    meanwhile wins. The write itself only applies while the row still holds
    what that re-read saw.
    """
    from models.memory import EDGE_TYPE_SUPERSEDES
    from services.supersede_dismissal import is_dismissed
    from services.supersede_verify import (
        VERDICT_PARALLEL,
        VERDICT_SUPERSEDES,
        VERIFY_MODE_LLM,
        judge_pair,
        pair_fingerprint,
        reuse_verification,
    )

    memory_id_str = str(memory.id)
    target_id_str = str(raw_top["id"])
    raw_top_score = float(raw_top["score"])

    # Don't resurrect an ACCEPTED suggestion. If a 'supersedes' edge from this
    # memory to the candidate already exists, the user already actioned the
    # suggestion and _accept_supersede_candidate_if_matching cleared the
    # column. Since detection runs independent of the seed guard (F2), a
    # re-embed (update_memory edit / sleep reindex / backfill) would otherwise
    # re-populate the candidate and re-surface it on recall — breaking the
    # accept-and-clear self-heal contract (#1403 review F2 guard).
    existing_super = await edge_repo.get_edge(
        memory.user_id,
        memory.id,
        UUID(target_id_str),
        workspace_id=workspace_id_str,
        context_id=context_id_str,
    )
    already_superseded = (
        existing_super is not None and existing_super.edge_type == EDGE_TYPE_SUPERSEDES
    )
    # #1504: don't resurrect a REJECTED suggestion either. The accept-side
    # guard above keys on the edge; rejection leaves no edge by design (the
    # memories stay independent), so it keys on the tombstone instead. Keeping
    # both guards here means a re-embed cannot undo either decision
    # (``tests/services/test_supersede_dismissal.py`` pins that this ``if``
    # gates the write on both).
    already_dismissed = is_dismissed(
        memory.supersede_candidate, target_id=target_id_str, similarity=raw_top_score
    )
    if already_dismissed:
        logger.info(
            "supersede_candidate_suppressed_by_dismissal",
            memory_id=memory_id_str,
            candidate_memory_id=target_id_str,
            similarity=round(raw_top_score, 4),
        )
    if not already_superseded and not already_dismissed:
        candidate: dict[str, Any] = {
            "memory_id": target_id_str,
            "similarity": round(raw_top_score, 4),
            "detected_at": to_utc_iso(utcnow()),
        }
        if getattr(config, "supersede_verify_mode", None) == VERIFY_MODE_LLM:
            old = (
                await db.execute(
                    select(Memory).where(
                        Memory.id == UUID(target_id_str), Memory.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            verification = None
            newer_target = (
                old is not None
                and old.created_at is not None
                and memory.created_at is not None
                and old.created_at > memory.created_at
            )
            if newer_target:
                # The neighbour was written AFTER this memory (its embedding
                # just finished first). Asking whether the earlier text
                # "supersedes" the later one would invert the question — the
                # candidate stays unverified, exactly as before #2016.
                logger.info(
                    "supersede_verify_skipped_newer_target",
                    memory_id=memory_id_str,
                    candidate_memory_id=target_id_str,
                )
            elif old is not None:
                verification = reuse_verification(
                    memory.supersede_candidate,
                    target_id=target_id_str,
                    similarity=raw_top_score,
                    fingerprint=pair_fingerprint(old, memory),
                )
                if verification is None:
                    verification = await judge_pair(
                        db,
                        config=config,
                        user_id=memory.user_id,
                        workspace_id=workspace_id_str,
                        context_id=context_id_str,
                        new=memory,
                        old=old,
                    )
            if verification is not None:
                candidate["verification"] = verification

        # Compared as stored (4 decimals): the read path tests the stored value.
        if round(raw_top_score, 4) < _SUPERSEDE_SUGGEST_THRESHOLD:
            # #2123: the pair is a candidate only because the judge mode
            # lowered the threshold. Without the judge's answer it would be an
            # unverified suggestion below the precision 0.85 was chosen for;
            # a parallel verdict is kept (the pair is not re-judged, never
            # surfaced), a supersedes verdict is the point.
            stored = candidate.get("verification")
            verdict = stored.get("verdict") if isinstance(stored, dict) else None
            if verdict not in (VERDICT_SUPERSEDES, VERDICT_PARALLEL):
                logger.info(
                    "supersede_candidate_skipped_unjudged_low_similarity",
                    memory_id=memory_id_str,
                    candidate_memory_id=target_id_str,
                    similarity=round(raw_top_score, 4),
                    verdict=verdict,
                )
                # No near-duplicate at 0.85 or above any more: a suggestion
                # stored there (an earlier text, or before the judge was on)
                # is cleared as #2059 clears it in the off mode — a clear
                # margin below, so a pair hovering at 0.85 is not flapped.
                if (
                    clear_allowed
                    and raw_top_score < _SUPERSEDE_SUGGEST_THRESHOLD - RESURFACE_SIMILARITY_DELTA
                ):
                    stored_similarity = (
                        memory.supersede_candidate.get("similarity")
                        if isinstance(memory.supersede_candidate, dict)
                        else None
                    )
                    if (
                        isinstance(stored_similarity, (int, float))
                        and stored_similarity >= _SUPERSEDE_SUGGEST_THRESHOLD
                    ):
                        await _clear_supersede_candidate_without_neighbour(db, memory=memory)
                return

        # #2069: every path — not only the judge's round trip — ran on the row
        # as this task loaded it, before the embed, the upsert and the search.
        # An edit, a dismissal or an accept committed since must win.
        if await _supersede_write_preempted(
            db,
            memory=memory,
            edge_repo=edge_repo,
            target_id_str=target_id_str,
            raw_top_score=raw_top_score,
            workspace_id_str=workspace_id_str,
            context_id_str=context_id_str,
        ):
            return
        if not await _store_supersede_candidate(db, memory=memory, candidate=candidate):
            return
        verification = candidate.get("verification")
        logger.info(
            "supersede_candidate_detected",
            memory_id=memory_id_str,
            candidate_memory_id=candidate["memory_id"],
            similarity=candidate["similarity"],
            verdict=verification.get("verdict") if isinstance(verification, dict) else None,
        )


class _SeedEdgesDropped(Exception):
    """Raised inside the seed-edge SAVEPOINT to roll it back (#2085)."""


def _memory_log_id(memory: Memory) -> str:
    """``memory``'s id for a log line, read without a lazy load.

    A root rollback expires every instance in the session; touching
    ``memory.id`` on an expired instance lazy-loads it, which raises
    ``MissingGreenlet`` under ``AsyncSession`` — inside an ``except`` block
    that replaces the error being handled. The identity key survives expiry
    (the #2088 ``_row_id`` pattern). A transient instance (or a test double)
    has no identity key and is not expired, so its attribute is safe.
    """
    try:
        identity = sa_inspect(memory).identity
    except NoInspectionAvailable:
        identity = None
    if identity:
        return str(identity[0])
    return str(memory.id)


async def _reload_after_rollback(db: AsyncSession, memory: Memory) -> None:
    """Load ``memory`` again after a rollback expired it (#2085).

    The embedding run hands the same instance to the tag co-occurrence seeding
    after the k-NN seeding; on an expired instance its first attribute read
    would be a lazy load (``MissingGreenlet``). Best-effort: when the reload
    fails (the row is gone, the session is broken) the next step's own
    best-effort handler takes over.
    """
    try:
        await db.refresh(memory)
    except Exception as exc:
        logger.debug(
            "knn_seed_reload_after_rollback_failed",
            memory_id=_memory_log_id(memory),
            error=str(exc),
        )


async def _create_knn_seed_edges(
    db: AsyncSession,
    memory: Memory,
    vector: list[float],
    collection_name: str,
    model_name: str,
    *,
    mechanical_reembed: bool = False,
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
    calibration row yet), seed-edge creation is skipped for this call — the
    bootstrap trigger in the remember() path will populate the row once the
    context crosses D3, and subsequent calls will use the calibrated threshold.

    Issue #2006: the #1403 supersede-suggestion detection shares the same
    neighbour query but does not depend on the seed threshold, so it runs
    whether or not the threshold resolved.

    Issue #2010: ``knn_seed_enabled`` gates only the seed edges and
    ``supersede_suggest_enabled`` only the detection; the neighbour query runs
    when either is on.

    Issue #2092: a re-embed of unchanged text (``mechanical_reembed``: a
    content-chunk backfill, an embedding-model switch, a repair re-queue, an
    admin retry of a once-embedded row) runs no supersede detection and no
    judge call — the column already says what the neighbourhood said, and
    verdicts are keyed on text, not vectors. The accepted trade-off: #2059's
    "no near-duplicate any more clears the suggestion" no longer fires on an
    unchanged-text re-embed (most visibly a model switch); a stored candidate then
    stays until the next edit, accept or dismissal (the read path's liveness
    guards still apply). Seed edges are unaffected.

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
        mechanical_reembed: True when the row was already embedded with this
            exact text (#2092); detection is skipped for this run.
    """
    from db.qdrant import search_memories_qdrant
    from neural.calibration import resolve_knn_threshold
    from neural.config import NeuralMemoryConfig
    from repositories.neural_edge import NeuralEdgeRepository
    from services.supersede_verify import text_fingerprint

    global _SUPERSEDE_SUGGEST_LAST_SEEN

    try:
        # #2085: the text this run embedded. The supersede detection below can
        # refresh ``memory`` from the row, so the seed fence compares against
        # this value rather than a fingerprint taken after that refresh.
        embedded_fingerprint = text_fingerprint(memory.summary, memory.content)
        config = await NeuralMemoryConfig.from_db(db)

        # #2010: two consumers share the neighbour query below — the seed edges
        # (knn_seed_enabled) and the #1403 supersede suggestions
        # (supersede_suggest_enabled). Each flag gates only its own consumer;
        # with both off there is nothing to read the query for.
        seed_enabled = bool(config.knn_seed_enabled)
        suggest_enabled = bool(config.supersede_suggest_enabled)
        if not suggest_enabled and _SUPERSEDE_SUGGEST_LAST_SEEN is not False:
            logger.info(
                "supersede_suggest_disabled",
                hint=(
                    "SUPERSEDE_SUGGEST_ENABLED is off; the post-embed detection stores "
                    "no new supersede_candidate (existing ones stay until dismissed or "
                    "accepted; remember(dedupe='check') is unaffected)"
                ),
            )
        _SUPERSEDE_SUGGEST_LAST_SEEN = suggest_enabled
        # #2092: after the once-per-process log above, which reports the
        # configured flag, not this run.
        detection_skipped = mechanical_reembed and suggest_enabled
        if detection_skipped:
            logger.debug("supersede_detect_skipped_mechanical_reembed", memory_id=str(memory.id))
            suggest_enabled = False
        if not seed_enabled and not suggest_enabled:
            return

        if not memory.context_id:
            # Defensive: 3-level isolation requires both
            return

        # Resolve the threshold via the D4 fallback chain (#406) — only when
        # seeding is on (#2010). None means "no seed edges for this call":
        # bootstrap will catch up later, and the supersede detection below
        # still runs (#2006).
        threshold: float | None = None
        if seed_enabled:
            threshold = await resolve_knn_threshold(
                db=db,
                config=config,
                model_name=model_name,
                dimensions=len(vector),
            )
        if seed_enabled and threshold is None:
            # Bootstrap trigger (#406 C2 trigger 1): if this (model, dims)
            # has crossed D3, enqueue a deduped calibration job so
            # subsequent remember() calls find a populated row and exit
            # step 3. Dedup (Redis SET NX, 1h TTL) ensures at most one
            # enqueue per hour regardless of concurrent ingestion.
            gate_crossed = False
            try:
                from tasks.neural_calibration import maybe_trigger_bootstrap

                # #2006: the supersede detection below keeps using this
                # session, so a DB error swallowed here must not leave it in
                # an aborted transaction ("current transaction is aborted").
                # A SAVEPOINT clears it without a root rollback, which would
                # expire ``memory`` and make the detection's edit check
                # compare the re-read text with itself (#2085).
                async with db.begin_nested():
                    gate_crossed = await maybe_trigger_bootstrap(
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
                    memory_id=_memory_log_id(memory),
                    model=model_name,
                    error=str(exc),
                )
            # #2006 visibility rule: below D3 (a fresh deployment or a small
            # context) a missing calibration row is the expected state, and
            # resolve_knn_threshold already logs it at DEBUG to avoid alert
            # fatigue. Only once the D3 gate is crossed — maybe_trigger_bootstrap
            # returned True and enqueued a calibration job — does a missing row
            # mean seed edges are really off, so WARN then, once per process per
            # (model, dimensions).
            warn_key = (model_name, len(vector))
            if gate_crossed and warn_key not in _KNN_SEED_UNCALIBRATED_WARNED:
                _KNN_SEED_UNCALIBRATED_WARNED.add(warn_key)
                logger.warning(
                    "knn_seed_uncalibrated_embedder",
                    model=model_name,
                    dimensions=len(vector),
                    hint=(
                        "seed edges are off until a knn_seed calibration row "
                        "exists; supersede detection still runs"
                    ),
                )

        # #2010: seeding reads the query only with a threshold in hand; if the
        # detection is off as well, nothing consumes it, so skip the round trip.
        needs_seed = seed_enabled and threshold is not None
        if not needs_seed and not suggest_enabled:
            skip_key = (model_name, len(vector))
            if detection_skipped:
                # #2092: the flag is on; only this run skips the detection.
                # Neither the INFO hint nor its once-per-embedder slot fits.
                logger.debug(
                    "knn_seed_skip_no_consumer",
                    memory_id=str(memory.id),
                    model=model_name,
                    dimensions=len(vector),
                    reason="mechanical_reembed",
                )
            elif skip_key not in _KNN_SEED_NO_CONSUMER_REPORTED:
                _KNN_SEED_NO_CONSUMER_REPORTED.add(skip_key)
                logger.info(
                    "knn_seed_skip_no_consumer",
                    memory_id=str(memory.id),
                    model=model_name,
                    dimensions=len(vector),
                    hint=(
                        "no seed edges (no knn_seed calibration row yet) and "
                        "SUPERSEDE_SUGGEST_ENABLED is off; logged once per process "
                        "per embedder, later ingests at DEBUG"
                    ),
                )
            else:
                logger.debug(
                    "knn_seed_skip_no_consumer",
                    memory_id=str(memory.id),
                    model=model_name,
                    dimensions=len(vector),
                )
            return

        workspace_id_str = str(memory.workspace_id)
        context_id_str = str(memory.context_id)
        memory_id_str = str(memory.id)

        # Search for k+1 candidates because the new memory itself will appear
        # as the top hit (cosine similarity with itself = 1.0). When seeding
        # will not run (no threshold, or switched off — #2006 / #2010) only the
        # supersede detection consumes the result, and it reads just the top
        # non-self neighbour, so 2 hits suffice. This runs BEFORE the seed-edge
        # idempotency guard below because the #1403 supersede detection (which
        # reads these candidates) must not inherit that guard — see its comment.
        candidates = await search_memories_qdrant(
            user_id=memory.user_id,
            query_vector=vector,
            workspace_id=workspace_id_str,
            context_id=context_id_str,
            limit=config.knn_seed_k + 1 if needs_seed else 2,
            collection_name=collection_name,
        )

        # #1403 option B: the supersede-suggestion detection reads the RAW
        # top-1 neighbour, independent of the seed threshold, and runs before
        # the seed idempotency guard — see _detect_supersede_candidate.
        # edge_repo is shared by the detection and the seed guard below.
        edge_repo = NeuralEdgeRepository(db)

        non_self = [c for c in candidates if str(c["id"]) != memory_id_str]
        if suggest_enabled:
            # #2123: lower while the judge is on; 0.85 otherwise.
            detect_threshold = _supersede_detect_threshold(config)
            raw_top = max(non_self, key=lambda c: float(c["score"])) if non_self else None
            if raw_top is not None and float(raw_top["score"]) >= detect_threshold:
                await _detect_supersede_candidate(
                    db,
                    memory=memory,
                    raw_top=raw_top,
                    edge_repo=edge_repo,
                    config=config,
                    workspace_id_str=workspace_id_str,
                    context_id_str=context_id_str,
                    clear_allowed=len(non_self) < len(candidates),
                )
            elif len(non_self) < len(candidates) and (
                raw_top is None
                or float(raw_top["score"])
                < _supersede_clear_floor(memory, detect_threshold) - RESURFACE_SIMILARITY_DELTA
            ):
                # #2059: no near-duplicate any more. Only when the search
                # returned this memory's own point (just upserted): a result
                # without it is no evidence that the neighbour is gone. And
                # only a clear margin below the threshold, so a pair hovering
                # at it is not cleared and re-judged on every re-embed.
                await _clear_supersede_candidate_without_neighbour(db, memory=memory)

        # #2006: everything below creates seed edges, which needs a calibrated
        # (or operator) threshold. Without one — or with seeding switched off
        # (#2010) — only the detection above runs.
        if not needs_seed:
            logger.debug(
                "knn_seed_skip_no_threshold",
                memory_id=memory_id_str,
                model=model_name,
                dimensions=len(vector),
            )
            return

        # Idempotency guard (SEEDING ONLY — the supersede detection above
        # deliberately runs first): process_pending_embedding is called on both new
        # memory creation AND update_memory re-embed (memory_service.py:431).
        # Without this check, re-embeds would overwrite existing Hebbian edges
        # via ON CONFLICT DO UPDATE on the (user_id, src_id, dst_id) unique
        # constraint — downgrading `neural_association` weight=1.5 to
        # `semantic_similarity` weight=0.3. Skip seeding if the memory already
        # has any outgoing edges.
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

        # #2085: the inserts run inside one SAVEPOINT so the edit fence below
        # can drop them without a root rollback, which would expire
        # ``memory`` for the caller's tag seeding (a lazy load under
        # AsyncSession raises MissingGreenlet).
        try:
            async with db.begin_nested():
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

                # #2085: an edit committed after this run's success write
                # starts its own run, which finds no edges and seeds from the
                # new text — unless this run commits first: the guard above
                # then keeps the stale edges for good. The inserts are still
                # uncommitted, so re-read the text right before the commit
                # and, when it moved, drop them and leave the seeding to the
                # newer run.
                if edges_created > 0 and await _source_text_moved(db, memory, embedded_fingerprint):
                    raise _SeedEdgesDropped
        except _SeedEdgesDropped:
            logger.info("knn_seed_skipped_source_edited", memory_id=memory_id_str)
            return

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
                memory_id=_memory_log_id(memory),
                error=str(rollback_err),
            )
        else:
            await _reload_after_rollback(db, memory)
        logger.warning(
            "knn_seeding_failed",
            memory_id=_memory_log_id(memory),
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

        if not memory.context_id:
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
                memory_id=_memory_log_id(memory),
                error=str(rollback_err),
            )
        else:
            await _reload_after_rollback(db, memory)
        logger.warning(
            "tag_cooccurrence_seeding_failed",
            memory_id=_memory_log_id(memory),
            error=str(e),
        )


def is_configuration_failure(exc: BaseException) -> bool:
    """Is this failure a fixable STATE of the workspace, not a poison row? (#1496)

    `MAX_EMBEDDING_RETRIES` is terminal by design, for the class its own comment
    names: rows that can never succeed however often they are retried (bad
    input, oversized text). Burning the budget on those is exactly right.

    "No embedding credential is configured" is not that class. It is a state,
    and it is fixable — but under the old behaviour a workspace with no key
    burned all three attempts in about three minutes and then stayed broken
    permanently, because the sweep only claims rows with `retry_count < MAX`.
    Adding the key afterwards changed nothing. That is how #1496's production
    deployment accumulated 467 memories that were saved, counted, billed
    against quota, and invisible to recall in both semantic and keyword mode.

    Branching on these types is safe because `EmbeddingService` re-raises them
    UNWRAPPED, above its blanket `except Exception -> OpenAIError`, so they
    arrive at the failure handler intact. They are also raised BEFORE the
    provider call — the spend-cap gate and the credential lookup both precede
    `client.embeddings.create` — so re-probing one costs no tokens and no HTTP
    request, which is what makes an unbounded retry affordable here.

    `FeatureNotAvailableError` is on the list for the same reason the other two
    are (#1644 S12). The managed-embeddings refusal — "this plan has no managed
    embeddings, add a BYOK key or upgrade" — used to be a `ConfigurationError`
    and qualified by that type; S12 moved it to `FEAT-001` so it stops
    answering 5xx. It is the same fixable workspace STATE it always was, on
    exactly the Free-tier workspaces #1496 was written for, so it must keep
    qualifying. Dropping it here would re-introduce #1496 for them: three
    retries in about three minutes, then permanently invisible to recall even
    after the key is added.

    KNOWN GAP: a *revoked or wrong* key surfaces as a plain `OpenAIError`,
    because the blanket wrapper erases the provider SDK's type. That is also a
    configuration state and it still goes terminal. Reading the SDK's status
    codes to catch it would couple us to `openai` internals that this project
    does not pin an upper bound on, so it is deliberately left for its own
    issue rather than guessed at here.
    """
    return isinstance(
        exc, ConfigurationError | EmbeddingSpendCapExceeded | FeatureNotAvailableError
    )


def is_unrebuildable_resource_failure(exc: BaseException) -> bool:
    """Is this a resource-ingested row whose point cannot be rebuilt? (#1897)

    ``ResourceIndexer.rebuild_point`` raises ``ResourceRebuildError`` when the
    row's resource has no schema any more, or the row no longer holds a JSON
    document. Both are permanent until an operator acts — publishes the schema
    again, or ingests the document as a newer version — so a retry changes
    nothing: the row is final on the first attempt instead of waiting out the
    backoff ``MAX_EMBEDDING_RETRIES`` times.

    The opposite of ``is_configuration_failure`` in what it does to the
    budget. A missing credential is re-probed without bound because adding the
    key is all it takes and the probe is free. Here the sweep cannot tell that
    the schema is back, and every probe reads the context, the resource and
    its schema, so the row stops and the admin retry
    (``POST /admin/embedding/retry-failed``) starts it again.

    The type arrives intact: ``rebuild_point`` raises it before it calls the
    embedding service, and nothing between it and the failure handler wraps it.
    """
    from services.resource_indexer import ResourceRebuildError

    return isinstance(exc, ResourceRebuildError)


def embedding_failure_values(exc: BaseException, now: datetime) -> dict[str, Any]:
    """Column values to stamp on a memory whose embedding failed (#1496).

    Extracted from `process_pending_embedding` so the decision that matters —
    whether this failure spends the retry budget — is testable without a live
    session, a Qdrant client and a `get_db()` loop. The handler applies whatever
    this returns; a composition test pins that it still does.

    Omitting `embedding_retry_count` leaves the claim's increment standing, so
    the row counts down to terminal as before. Setting it to 0 (rather than
    refunding the increment) is exact: the claim adds 1 via a CASE on the
    pre-UPDATE status, so `-1` would over-decrement a re-claimed stale
    `processing` row. It also matches the doctrine the success path already
    states — the budget is per failure-episode, not a lifetime tally.

    A row that cannot be rebuilt (#1897, ``is_unrebuildable_resource_failure``)
    is put at the ceiling instead: ``embedding_retry_eligible_clause`` claims a
    ``failed`` row only below it, so no retry is scheduled, and the counts of
    rows nothing will retry (``embedding_retry_count >= MAX_EMBEDDING_RETRIES``)
    include it. The admin retry resets the counter along with the status.

    The project's own errors are stored as ``"<error_code>: <message>"``
    (#2015 D6), so the unsearchable-backlog metric can tell a capped or
    unconfigured workspace (``QUOTA-002``, ``CFG-*``, ``FEAT-*``) from a
    failing provider.
    """
    from config.constants import MAX_EMBEDDING_RETRIES

    error = f"{exc.error_code}: {exc}" if isinstance(exc, MemoryCloudException) else str(exc)
    values: dict[str, Any] = {
        "embedding_status": "failed",
        "embedding_error": error[:500],
        # The #979 retry backoff (embedding_retry_eligible_clause) anchors on
        # the pipeline's own clock (#1852; it was updated_at until then, which
        # made every embedded memory look edited).
        "embedding_attempted_at": now,
    }
    if is_configuration_failure(exc):
        values["embedding_retry_count"] = 0
    elif is_unrebuildable_resource_failure(exc):
        values["embedding_retry_count"] = MAX_EMBEDDING_RETRIES
    return values


def embedding_retry_eligible_clause(now: datetime):
    """SQLAlchemy clause: a ``failed`` embedding eligible for #979 auto-requeue.

    Eligible when it still has retry budget (``embedding_retry_count <
    MAX_EMBEDDING_RETRIES``) and its backoff has elapsed. The backoff is
    measured from ``embedding_attempted_at``, the pipeline's own clock (#1852;
    the failure ``UPDATE`` stamps it — until #1852 it stamped ``updated_at``);
    a NULL clock is treated as immediately eligible so a row can never get
    permanently stuck ``failed`` — the exact state #979 exists to prevent.

    Shared by the sweep prefilter (``tasks/embedding_tasks.py``) and the atomic
    claim in ``process_pending_embedding`` so the two gates cannot drift.
    """
    from datetime import timedelta

    from sqlalchemy import and_

    from config.constants import EMBEDDING_RETRY_BACKOFF_SECONDS, MAX_EMBEDDING_RETRIES
    from models.memory import Memory

    retry_cutoff = now - timedelta(seconds=EMBEDDING_RETRY_BACKOFF_SECONDS)
    return and_(
        Memory.embedding_status == "failed",
        Memory.embedding_retry_count < MAX_EMBEDDING_RETRIES,
        embedding_clock_before(retry_cutoff),
    )


def embedding_reclaim_eligible_clause(stale_cutoff: datetime):
    """SQLAlchemy clause: a stale ``processing`` row a new run may reclaim (#2095).

    Stale means the claim stamp (``embedding_attempted_at``) is older than
    ``stale_cutoff``: the run crashed, or it hung past its renewal limit. A
    reclaim spends one unit of retry budget (the claim's CASE counts it), so
    the clause also requires budget left. Without that bound, a run that
    always outlives its claim would be reclaimed forever and never fail.

    ``embedding_reclaim_exhausted_clause`` is the complement on the budget
    side: the sweep moves those rows to ``failed``.

    Shared by the sweep prefilter (``tasks/embedding_tasks.py``) and the atomic
    claim in ``process_pending_embedding`` so the two gates cannot drift.
    """
    from sqlalchemy import and_

    from config.constants import MAX_EMBEDDING_RETRIES
    from models.memory import Memory

    return and_(
        _stale_processing(stale_cutoff), Memory.embedding_retry_count < MAX_EMBEDDING_RETRIES
    )


def embedding_reclaim_exhausted_clause(stale_cutoff: datetime):
    """SQLAlchemy clause: a stale ``processing`` row with no retry budget left (#2095).

    Nothing can claim it (``embedding_reclaim_eligible_clause`` needs budget),
    so the sweep fails it. It must use the same ``stale_cutoff`` as the claim,
    or a live run would be failed while it still holds its claim.
    """
    from sqlalchemy import and_

    from config.constants import MAX_EMBEDDING_RETRIES
    from models.memory import Memory

    return and_(
        _stale_processing(stale_cutoff), Memory.embedding_retry_count >= MAX_EMBEDDING_RETRIES
    )


def _stale_processing(stale_cutoff: datetime):
    """``processing`` with a claim stamp older than ``stale_cutoff`` (NULL-safe).

    The one definition both reclaim clauses split on the retry budget.
    """
    from sqlalchemy import and_

    from models.memory import Memory

    return and_(Memory.embedding_status == "processing", embedding_clock_before(stale_cutoff))


def embedding_clock_before(cutoff: datetime):
    """``embedding_attempted_at`` older than ``cutoff`` — NULL counts as older.

    NULL is a row the pipeline has not stamped: one written before #1852 added
    the column, or one whose claim never happened. Both must be picked up, not
    skipped, so the clause is NULL-safe in the permissive direction.
    """
    from sqlalchemy import or_

    from models.memory import Memory

    return or_(Memory.embedding_attempted_at.is_(None), Memory.embedding_attempted_at < cutoff)


def build_memory_point(memory: Memory) -> tuple[dict[str, Any], list[int], list[float]]:
    """Build the Qdrant payload and BM25 sparse vector for a memory row.

    The single definition of "what a memory looks like as a point". Used by
    ``process_pending_embedding`` (the normal write path) and by the
    embedding-model migration (#1525), which re-embeds a context into another
    collection and must produce identical payloads so the new collection is a
    drop-in for the old one. Only the dense vector differs between the two
    callers; it is not built here.

    Returns:
        ``(payload, sparse_indices, sparse_values)``. The payload does NOT yet
        carry the isolation fields — ``add_memory_to_qdrant`` adds
        ``workspace_id`` / ``context_id`` / ``user_id`` itself.
    """
    from utils.sparse_vector import build_document_sparse_vector
    from utils.text import normalize_for_search
    from utils.tokenizer import tokenize_and_reading, tokenize_for_search

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

    payload: dict[str, Any] = {
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
    # WHERE axis (#1332): geo payload for filters.near. The e69
    # generated columns are the single validated source — populated
    # only for a complete numeric pair that passed the regex guard
    # and range CHECK, so the payload mirrors PG exactly.
    if memory.location_lat is not None and memory.location_lon is not None:
        payload["location"] = {
            "lat": memory.location_lat,
            "lon": memory.location_lon,
        }
    return payload, sparse_indices, sparse_values


async def write_memory_point(
    db: AsyncSession,
    memory: Memory,
    *,
    collection_name: str,
    embedding_service: EmbeddingService,
    user_id: str,
    workspace_id: str | None,
    context_id: str | None,
    usage: dict[str, int] | None = None,
    on_write: Callable[[], None] | None = None,
    resource_indexer: ResourceIndexer | None = None,
    embed_scope: tuple[Any, Any] | None = None,
) -> tuple[list[float], dict[str, Any]] | None:
    """Write ``memory``'s own vector point: the one place that picks how (#2065).

    A row the resource indexer wrote owns the indexer's point
    (``Memory.point_id``, the uuid5 of the document, embedded from the
    document text with the resource payload). Its summary is only the label
    ``[resource] doc vN``: the summary path would store a label vector under
    the row id and leave ``point_id`` naming nothing (#1870, #2053). The
    indexer rebuilds its own point from the row instead. Every other row gets
    the summary point ``build_memory_point`` describes, under its row id.

    Shared by the embedding pipeline (``process_pending_embedding``), the Sleep
    reindex and the Sleep restores, which used to repeat this branch. Content
    chunk points are the caller's: only a summary point has them.

    Args:
        user_id, workspace_id, context_id: The point's isolation fields and
            the embedding call's key scope.
        usage: Optional out-parameter for a caller that accounts embedding
            cost: ``usage["tokens"]`` is set once the embedding answered (0 on
            a cache hit), also when the write then fails.
        on_write: Called once the write is under way: right before the
            summary point's upsert, and — the indexer embedding and writing in
            one call — right before ``rebuild_point`` for a resource row.
        resource_indexer: Reused for a resource row; one is built otherwise.
        embed_scope: ``(workspace_id, context_id)`` for the embedding call's key
            lookup when it is not the point's (the pipeline passes the row's
            own, ``None`` for a context-less row).

    Returns:
        ``(vector, payload)`` of the summary point written, or ``None`` for a
        resource row (it has no summary point, chunks or seed edges).

    Raises:
        ResourceRebuildError: A resource row whose point cannot be rebuilt.
        Exception: The embedding or the vector-store write failed.
    """
    from services.resource_indexer import ResourceIndexer, owns_resource_point

    if owns_resource_point(memory):
        if on_write is not None:
            on_write()
        indexer = resource_indexer if resource_indexer is not None else ResourceIndexer(db)
        await indexer.rebuild_point(
            memory,
            collection_name=collection_name,
            embedding_service=embedding_service,
            usage=usage,
        )
        return None

    embed_workspace_id, embed_context_id = (
        embed_scope if embed_scope is not None else (workspace_id, context_id)
    )
    if usage is None:
        vector = await embedding_service.embed(
            memory.summary or "",
            user_id,
            context_id=embed_context_id,
            workspace_id=embed_workspace_id,
        )
    else:
        vector, usage["tokens"] = await embedding_service.embed_with_usage(
            memory.summary or "",
            user_id=user_id,
            context_id=embed_context_id,
            workspace_id=embed_workspace_id,
        )
    # Payload + BM25 sparse vector (#1525: the same point every writer builds;
    # the upsert replaces the whole point, #2053).
    payload, sparse_indices, sparse_values = build_memory_point(memory)
    if on_write is not None:
        on_write()
    await add_memory_to_qdrant(
        user_id=user_id,
        memory_id=memory.id,
        vector=vector,
        payload=payload,
        workspace_id=cast(str, workspace_id),
        context_id=cast(str, context_id),
        sparse_indices=sparse_indices,
        sparse_values=sparse_values,
        collection_name=collection_name,
    )
    return vector, payload


async def _requeue_after_lost_claim(db: Any, memory_id: UUID) -> bool:
    """Re-queue a row whose embedding run found its claim lost after writing (#2061).

    The run's points went out before it knew: a newer run may have written
    the memory's summary and chunk points first, and this run's writes then
    replaced them. When that newer run is done (``success``), the row goes
    back to ``pending`` so the sweep writes them once more; a row re-queued
    by an edit or a migration switch, or a newer run still in flight, is left
    as it is (``requeue_embedding`` moves only a ``success`` row). Never
    raises.
    """
    try:
        requeued = await requeue_embedding(db, memory_id, reason="claim_lost_after_write")
        await db.commit()
    except Exception as e:
        logger.warning(
            "embedding_requeue_failed", memory_id=str(memory_id), error_class=type(e).__name__
        )
        return False
    return requeued


async def process_pending_embedding(memory_id: UUID) -> None:
    """Process embedding generation + Qdrant upsert for a pending memory.

    Issue #76: Called via asyncio.create_task (fire-and-forget) or by the
    periodic sweep task for crash recovery. Reads memory from DB to get
    all needed fields — no parameter sprawl.
    """

    from datetime import timedelta

    from sqlalchemy import case, or_, select, update

    from db.base import get_db
    from models.memory import Memory
    from services.context_routing import resolve_context_routing
    from services.embedding_claim import STALE_CLAIM_SECONDS, EmbeddingClaim
    from services.embedding_service import EmbeddingService
    from services.supersede_verify import embedded_text_digest
    from utils.datetime import utcnow as _utcnow

    async for db in get_db():
        now = _utcnow()
        # #2041: the claim's stamp is this run's token. The sweep reclaims a
        # `processing` row whose stamp is older than the stale cutoff (status
        # stays `processing`, the stamp moves), so the chunk writer's checks
        # and the outcome writes below match on the stamp: a reclaimed run
        # must not end the newer run's claim. A live run renews its stamp so
        # it is not reclaimed for being slow (``services.embedding_claim``).
        claim = EmbeddingClaim(db, memory_id, now)
        # #2061: set once this run sends its first vector-store write, so a
        # run that lost its claim re-queues the row only if it may have
        # replaced the newer run's points.
        wrote_points = False
        try:
            # Claim: atomically set pending/stale-processing → processing
            # Stale processing: embedding_attempted_at older than
            # STALE_CLAIM_SECONDS (crash recovery)
            stale_cutoff = now - timedelta(seconds=STALE_CLAIM_SECONDS)
            # #979: a `failed` row is also eligible for auto-retry once its
            # backoff has elapsed and it still has retry budget. The shared
            # clause keeps this gate byte-identical to the sweep prefilter. The
            # counter is incremented when we claim a `failed` row or reclaim a
            # stale `processing` one (#2095, CASE below), so the initial
            # pending->processing claim spends no budget.
            result = await db.execute(
                update(Memory)
                .where(
                    Memory.id == memory_id,
                    Memory.deleted_at.is_(None),
                    or_(
                        Memory.embedding_status == "pending",
                        # #2095: bounded by the retry budget, shared with the
                        # sweep prefilter.
                        embedding_reclaim_eligible_clause(stale_cutoff),
                        embedding_retry_eligible_clause(now),
                    ),
                )
                .values(
                    embedding_status="processing",
                    # #1852: the pipeline's own clock; updated_at is an edit's.
                    embedding_attempted_at=now,
                    # Count retries of a previously-failed row and reclaims of
                    # a stale processing row (#2095: a run that outlives its
                    # claim every time must end). The CASE reads the
                    # pre-UPDATE status, so a pending claim leaves the counter
                    # untouched. Success resets it, so the budget is per
                    # failure episode.
                    embedding_retry_count=case(
                        (
                            Memory.embedding_status.in_(("failed", "processing")),
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
            claim.start_renewal()

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

            # #2092: the text this run embeds, and whether the row was already
            # embedded with exactly that text (a content-chunk backfill, an
            # embedding-model switch, a repair re-queue). Decided here, before
            # the success write: that ORM UPDATE may sync the new digest onto
            # ``memory``, which would make every run look mechanical.
            embedded_digest = embedded_text_digest(memory.summary or "", memory.content or "")
            mechanical_reembed = memory.embedded_text_digest == embedded_digest

            # Resolve per-context routing (#341: shared helper)
            collection, embed_svc = await resolve_context_routing(
                db, memory.context_id, default_service=EmbeddingService(db)
            )

            # #1870 / #2065: a resource-indexer row gets the indexer's point
            # back (a restored context's rows come here, #1804); every other
            # row its summary point. ``write_memory_point`` picks.
            def _mark_written() -> None:
                nonlocal wrote_points
                wrote_points = True

            written = await write_memory_point(
                db,
                memory,
                collection_name=collection,
                embedding_service=embed_svc,
                user_id=memory.user_id,
                workspace_id=str(memory.workspace_id),
                context_id=str(memory.context_id),
                on_write=_mark_written,
                embed_scope=(memory.workspace_id, memory.context_id),
            )
            vector: list[float] | None = None
            if written is not None:
                vector, payload = written
                # #2015: on an opted-in context, a long content is also
                # embedded as chunk points; a no-op (no query) for content
                # too short to split. #2049: a chunk failure is raised, not
                # swallowed, so the row is marked failed and the sweep retries
                # it instead of stamping `success` with the chunks missing.
                await write_content_chunks(
                    db,
                    memory,
                    base_payload=payload,
                    embed_svc=embed_svc,
                    collection_name=collection,
                    fail_open=False,
                    claim=claim,
                )

            # Mark success. Clear the prior embedding_error AND reset
            # embedding_retry_count (#979): the retry budget is per
            # failure-episode, not a lifetime tally — a later, unrelated
            # failure must get the full MAX_EMBEDDING_RETRIES budget again.
            #
            # #1525: only while this worker still owns the claim. An embedding
            # migration's switch re-queues rows to `pending` after flipping
            # the context's routing; this worker resolved the OLD routing and
            # wrote to the old collection. Stamping `success` here would hide
            # that row from the sweep for good. If the claim is gone, leave
            # the row as the re-queue left it and let the sweep redo it under
            # the new routing.
            #
            # #2041: the claim is also gone when the sweep reclaimed the row
            # from this run (its stamp went stale: the renewal failed, or the
            # run outlived MAX_RENEW_SECONDS). Stamping `success` on that
            # newer claim would hide the newer run's failed chunk write from
            # every retry — and with this run's chunk write skipped as
            # superseded, leave the memory with no chunk points for good.
            # Stopping the renewal first makes ``claim.stamp`` final.
            await claim.stop_renewal()
            done = await db.execute(
                update(Memory)
                .where(
                    Memory.id == memory_id,
                    Memory.embedding_status == "processing",
                    Memory.embedding_attempted_at == claim.stamp,
                )
                .values(
                    embedding_status="success",
                    embedding_error=None,
                    embedding_retry_count=0,
                    # #2092: the only writer of the column.
                    embedded_text_digest=embedded_digest,
                )
            )
            await db.commit()
            if int(getattr(done, "rowcount", 0) or 0) == 0:
                logger.info(
                    "embedding_claim_lost",
                    memory_id=str(memory_id),
                    collection=collection,
                    reason=claim.loss_reason(),
                    requeued=await _requeue_after_lost_claim(db, memory_id),
                )
                return

            logger.info(
                "embedding_completed",
                memory_id=str(memory_id),
                mechanical_reembed=mechanical_reembed,
            )

            if vector is None:
                # #1870: a resource point, as the indexer writes it — the
                # indexer seeds no edges for its rows, and neither does this.
                return

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
                mechanical_reembed=mechanical_reembed,
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
            await claim.stop_renewal()

            # #1496: a CONFIGURATION failure must not spend the retry budget.
            #
            # The decision lives in `embedding_failure_values` so it can be
            # tested without a live session and a Qdrant client; see its
            # docstring for why a configuration failure must not spend the
            # budget, and `is_configuration_failure` for which failures those
            # are (and the one known gap). #1897: it also makes a resource row
            # that cannot be rebuilt final on this attempt
            # (`is_unrebuildable_resource_failure`).
            from config.constants import MAX_EMBEDDING_RETRIES

            values = embedding_failure_values(e, utcnow())

            final_count: int | None = None
            claim_lost = False
            try:
                # #1525: same fence as the success write — a row re-queued
                # (`pending`) by a migration switch, or reclaimed by a newer
                # run (#2041), must not be marked failed by the worker that
                # lost it.
                result = await db.execute(
                    update(Memory)
                    .where(
                        Memory.id == memory_id,
                        Memory.embedding_status == "processing",
                        Memory.embedding_attempted_at == claim.stamp,
                    )
                    .values(**values)
                    .returning(Memory.embedding_retry_count)
                )
                final_count = result.scalar_one_or_none()
                claim_lost = final_count is None
                await db.commit()
            except Exception:
                logger.warning("embedding_status_update_failed", memory_id=str(memory_id))

            if claim_lost:
                logger.info(
                    "embedding_claim_lost",
                    memory_id=str(memory_id),
                    error=str(e),
                    reason=claim.loss_reason(),
                    # A run that failed before writing replaced nothing.
                    requeued=wrote_points and await _requeue_after_lost_claim(db, memory_id),
                )
                return

            # #1496: the transition that matters to an operator is the one into
            # a state nothing will ever retry. Give it its OWN event name so it
            # is greppable and alertable, and so its absence means something.
            # `embedding_failed` fires on every attempt and drowns it.
            common = {
                "memory_id": str(memory_id),
                "error": str(e),
                # The class is what turns "read 467 messages" into one grep.
                "error_class": type(e).__name__,
                "error_code": getattr(e, "error_code", None),
                "retry_count": final_count,
            }
            if is_unrebuildable_resource_failure(e):
                # #1897: final too, but no budget was spent and no provider
                # failed. Expected after restoring a context whose resource
                # lost its schema, so a warning under its own name; the row
                # carries the reason and waits for the admin retry.
                logger.warning("embedding_resource_unrebuildable", **common)
            elif final_count is not None and final_count >= MAX_EMBEDDING_RETRIES:
                logger.error("embedding_budget_exhausted", **common)
            else:
                logger.warning("embedding_failed", **common)
        finally:
            await claim.stop_renewal()
