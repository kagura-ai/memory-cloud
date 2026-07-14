"""Stage [J]: atomic persist for one analysis run.

The all-or-nothing requirement of issue #495 means **every write
this stage emits must commit or roll back together**:

- ``memory_analyses`` row updated to ``status='succeeded'`` (or
  ``'failed'``) with ``finished_at``, ``quality`` JSONB,
  ``cost_actual_cents``.
- ``memory_analysis_clusters`` rows — one per cluster_index.
- ``memory_analysis_assignments`` rows — one per memory.
- ``sleep_reports`` cost row created at ``source='analysis'``,
  ``paid_by='byok'``, finalized via ``SleepReporter.complete_report``.
- ``sleep_report_llm_usage`` rows with ``phase='cluster_labeling'``
  (the d07_495 migration extends the CHECK to allow this).

The orchestrator owns the transaction boundary (``async with
db.begin()``); this module never opens its own. That keeps the
reporter testable with a session already inside a SAVEPOINT and
makes the rollback semantics observable from the orchestrator.

Cost accounting: ``cost_actual_cents`` on ``memory_analyses`` is the
**single source of truth for the analysis run's cost**. The
``sleep_reports`` row + ``sleep_report_llm_usage`` rows feed the
#472 aggregation API for the workspace-wide BYOK ledger view, but
``memory_analyses.cost_actual_cents`` is what the per-run UI shows.
We compute it from the labeler breakdown using the run's frozen
``model_snapshot`` (set at run start) so a price change after the
run does not mutate historical costs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from models.analysis import (
    MEMORY_ANALYSIS_STATUSES,
    MemoryAnalysis,
    MemoryAnalysisAssignment,
    MemoryAnalysisCluster,
)
from models.sleep import SLEEP_REPORT_PAID_BY_VALUES, SLEEP_REPORT_SOURCES
from services.analysis.labeler import (
    MAX_CLUSTER_FAILURE_RATIO,
    ClusterLabel,
    ClusterLabelingThresholdExceeded,
)
from services.analysis.property_stats import (
    MemoryFacets,
    aggregate_cluster_stats,
)
from services.analysis.vector_pull import MemoryRecord
from services.sleep.reporter import (
    LLMCallBreakdown,
    PhaseResult,
    SleepReporter,
)
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


# Status / dimension constants. Use explicit string literals (NOT
# tuple indices) so a future tuple reordering does not silently flip
# the values. The asserts at module-load pin the contract that these
# literals must remain members of the canonical tuples; if a tuple
# value is removed, the assertion fires loud at startup rather than
# at the first INSERT (where it would surface as IntegrityError from
# the DB CHECK constraint).
_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED = "failed"
_STATUS_CANCELLED = "cancelled"
_SOURCE_ANALYSIS = "analysis"
_SLEEP_PAID_BY_BYOK = "byok"
assert _STATUS_SUCCEEDED in MEMORY_ANALYSIS_STATUSES
assert _STATUS_FAILED in MEMORY_ANALYSIS_STATUSES
assert _STATUS_CANCELLED in MEMORY_ANALYSIS_STATUSES
assert _SOURCE_ANALYSIS in SLEEP_REPORT_SOURCES
assert _SLEEP_PAID_BY_BYOK in SLEEP_REPORT_PAID_BY_VALUES

# sleep_report_llm_usage.phase value for analysis runs. The DB CHECK
# constraint added by the d07_495 migration must list this exact
# string; if the migration is updated, change here too.
_PHASE_CLUSTER_LABELING = "cluster_labeling"

# Defensive cap on the persisted error column. ``Text`` has no DB-side
# limit but a runaway exception chain could fill the column with
# megabytes of stack trace.
_ERROR_FIELD_MAX_CHARS = 8000


@dataclass(frozen=True)
class PersistInputs:
    """Bundle of compute-stage outputs the reporter consumes."""

    analysis: MemoryAnalysis
    memories: list[MemoryRecord]
    embeddings: np.ndarray
    cluster_labels: np.ndarray
    coords_2d: np.ndarray
    cluster_results: list[ClusterLabel]
    silhouette: float
    size_variance: float
    outlier_ratio: float
    window_from: datetime | None
    window_to: datetime | None


@dataclass(frozen=True)
class _CostTotals:
    """Aggregated token totals from a list of ``LLMCallBreakdown``s."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    calls: int


def _aggregate_breakdown_totals(
    cluster_labels: list[ClusterLabel],
) -> tuple[list[LLMCallBreakdown], _CostTotals]:
    """Walk cluster_labels once to extract breakdowns + summed totals.

    Replaces the prior pattern of walking the list twice (once for
    cost computation, once for ``PhaseResult`` totals) — same
    information, single pass.
    """
    breakdowns: list[LLMCallBreakdown] = []
    total_input = 0
    total_output = 0
    total_cached = 0
    total_calls = 0
    for cl in cluster_labels:
        b = cl.breakdown
        if b is None:
            continue
        breakdowns.append(b)
        total_input += b.input_tokens
        total_output += b.output_tokens
        total_cached += b.cached_input_tokens
        total_calls += b.calls
    return breakdowns, _CostTotals(
        input_tokens=total_input,
        output_tokens=total_output,
        cached_input_tokens=total_cached,
        calls=total_calls,
    )


def _compute_actual_cost_cents(
    totals: _CostTotals,
    model_snapshot: dict[str, Any],
) -> int:
    """Compute the run's actual cost from pre-aggregated totals.

    The snapshot must contain unit-price rows for input/output
    tokens (and optionally cache_read). We prefer rates from the
    snapshot — even if pricing changed after the run started, the
    historical cost stays stable. Returns rounded-up integer cents.
    """
    rates = model_snapshot.get("rates", {})
    input_per_m = float(rates.get("input_tokens", 0.0))
    output_per_m = float(rates.get("output_tokens", 0.0))
    # If cache_read isn't priced in the snapshot, treat cached input
    # as 10% of standard input (matches the Anthropic / OpenAI
    # discount range; gpt-5-nano's seeded rate confirms 10% is right).
    cached_per_m = float(rates.get("cache_read_tokens", input_per_m / 10.0))

    cost_usd = (
        totals.input_tokens * input_per_m
        + totals.output_tokens * output_per_m
        + totals.cached_input_tokens * cached_per_m
    ) / 1_000_000.0
    return max(1, math.ceil(cost_usd * 100)) if cost_usd > 0 else 0


def _build_cluster_row(
    *,
    cluster_id: UUID,
    analysis_id: UUID,
    cluster_label: ClusterLabel,
    member_pos: np.ndarray,
    coords_2d: np.ndarray,
    member_memories: list[MemoryRecord],
    window_from: datetime | None,
    window_to: datetime | None,
) -> MemoryAnalysisCluster:
    """Construct one ``MemoryAnalysisCluster`` ORM row.

    Centralized so the loop in ``persist_results`` stays readable
    and the per-cluster transformation is unit-testable.
    """
    facets = [
        MemoryFacets(
            type=m.type,
            tags=m.tags,
            importance=m.importance,
            created_at=m.created_at,
        )
        for m in member_memories
    ]
    property_stats = aggregate_cluster_stats(
        facets,
        window_from=window_from,
        window_to=window_to,
    )

    if member_pos.size > 0:
        cent_2d = coords_2d[member_pos].mean(axis=0).astype(float)
        cent_xy = [float(cent_2d[0]), float(cent_2d[1])]
    else:
        cent_xy = [0.0, 0.0]

    rep_uuids: list[UUID] = []
    for rid in cluster_label.representative_memory_ids:
        try:
            rep_uuids.append(UUID(rid))
        except (TypeError, ValueError):
            continue

    return MemoryAnalysisCluster(
        id=cluster_id,
        analysis_id=analysis_id,
        parent_id=None,  # v2 hierarchy
        cluster_index=cluster_label.cluster_index,
        label=cluster_label.label,
        description=cluster_label.description or None,
        count=int(member_pos.size),
        centroid_2d=cent_xy,
        representative_memory_ids=rep_uuids,
        property_stats=property_stats,
        label_confidence=float(cluster_label.label_confidence),
    )


def _index_members_by_cluster(
    cluster_labels: np.ndarray,
    n_clusters: int,
) -> dict[int, np.ndarray]:
    """Pre-compute member indices per cluster in one O(n) pass.

    Replaces ``np.where(cluster_labels == idx)`` per-cluster scans
    that would walk the labels array N_clusters times.
    """
    by_idx: dict[int, list[int]] = {i: [] for i in range(n_clusters)}
    for i, lbl in enumerate(cluster_labels):
        by_idx.setdefault(int(lbl), []).append(i)
    return {i: np.asarray(positions, dtype=np.int64) for i, positions in by_idx.items()}


async def persist_results(
    db: AsyncSession,
    *,
    inputs: PersistInputs,
    user_id: str,
    workspace_id: str,
    context_id: str,
) -> None:
    """Stage [J] all-or-nothing persist.

    Caller MUST run this inside a transaction (``async with
    db.begin()``). On any exception the surrounding transaction
    rolls back all writes — no half-persisted analysis run is
    possible.

    Args:
        db: Session inside the orchestrator's transaction.
        inputs: Compute-stage results (Stages [C] through [H]).
        user_id, workspace_id, context_id: Auth/scoping for the
            sibling ``sleep_reports`` cost row.
    """
    analysis = inputs.analysis
    memories = inputs.memories
    cluster_labels_arr = inputs.cluster_labels
    coords_2d = inputs.coords_2d
    cluster_results = inputs.cluster_results

    # 0. Cancellation guard FIRST, under a row lock (#1241). The guard
    #    used to run after the cluster/assignment flush — its early
    #    ``return`` was then followed by the orchestrator's commit, so a
    #    cancelled run permanently kept its cluster rows (served forever
    #    by /clusters, /positions and MCP get_cluster, none of which
    #    filter by status). ``with_for_update`` serializes against the
    #    DELETE handler's own locked re-check: whichever side takes the
    #    lock first decides the terminal state, and the loser observes
    #    it after commit instead of overwriting it.
    await db.refresh(analysis, with_for_update=True)
    if analysis.status == _STATUS_CANCELLED:
        logger.info(
            "analysis_persist_skipped_due_to_cancel",
            analysis_id=str(analysis.id),
            cancellation_reason=analysis.cancellation_reason,
        )
        return

    n_clusters = len(cluster_results)

    # #1246: enforce the labeler's documented failure contract BEFORE any
    # row is written. Runs AFTER the #1241 locked cancel guard by design:
    # a concurrent user cancel wins over the threshold failure, and the
    # raise reaches the orchestrator's failure path whose persist_failure
    # re-checks cancellation under the same lock. Without this check a
    # run whose every cluster failed labeling persisted as
    # ``status='succeeded'`` with ``label_confidence=0.0`` — an
    # unqualified success built entirely of "(unlabeled)" rows, with the
    # daily-quota slot consumed and no error surfaced.
    #
    # The DENOMINATOR is the LABELABLE cluster count — "(empty)"
    # sentinels never attempt labeling and cannot fail, so counting
    # them would dilute the ratio on degenerate embeddings (KMeans can
    # leave most clusters empty) until a run whose every real cluster
    # failed still slipped through as a success.
    labeling_failures = sum(1 for cl in cluster_results if cl.failed)
    labelable = sum(1 for cl in cluster_results if not cl.empty)
    if labelable > 0 and (labeling_failures / labelable) > MAX_CLUSTER_FAILURE_RATIO:
        raise ClusterLabelingThresholdExceeded(
            f"Cluster labeling failed for {labeling_failures}/{labelable} labelable "
            f"clusters (more than MAX_CLUSTER_FAILURE_RATIO={MAX_CLUSTER_FAILURE_RATIO}). "
            "Check the workspace BYOK key and the provider status; the run is "
            "marked failed instead of persisting mostly-unlabeled clusters."
        )

    members_by_idx = _index_members_by_cluster(cluster_labels_arr, n_clusters)

    # 1. Cluster rows. We generate UUIDs client-side so the assignment
    #    rows can reference them without a per-cluster flush round-trip
    #    (90 round-trips at n=8000 → 1 round-trip).
    cluster_id_by_index: dict[int, UUID] = {}
    cluster_rows: list[MemoryAnalysisCluster] = []
    for cluster_label in cluster_results:
        idx = cluster_label.cluster_index
        member_pos = members_by_idx.get(idx, np.empty(0, dtype=np.int64))
        cluster_id = uuid4()
        cluster_id_by_index[idx] = cluster_id
        cluster_rows.append(
            _build_cluster_row(
                cluster_id=cluster_id,
                analysis_id=analysis.id,
                cluster_label=cluster_label,
                member_pos=member_pos,
                coords_2d=coords_2d,
                member_memories=[memories[i] for i in member_pos.tolist()],
                window_from=inputs.window_from,
                window_to=inputs.window_to,
            )
        )

    db.add_all(cluster_rows)
    await db.flush()

    # 2. Assignment rows — one per memory, batched bulk-insert.
    assignment_payloads: list[dict[str, Any]] = []
    for i, memory in enumerate(memories):
        idx = int(cluster_labels_arr[i])
        cluster_id = cluster_id_by_index.get(idx)
        if cluster_id is None:
            continue
        assignment_payloads.append(
            {
                "analysis_id": analysis.id,
                "memory_id": memory.id,
                "cluster_id": cluster_id,
                "x": float(coords_2d[i, 0]),
                "y": float(coords_2d[i, 1]),
            }
        )
    if assignment_payloads:
        await db.execute(
            MemoryAnalysisAssignment.__table__.insert(),  # type: ignore[attr-defined]
            assignment_payloads,
        )

    # 3. Quality JSONB. Aggregate breakdowns once and reuse for both
    #    cost computation and the PhaseResult below.
    breakdowns, totals = _aggregate_breakdown_totals(cluster_results)

    label_confidences = [cl.label_confidence for cl in cluster_results if not cl.failed]
    avg_label_confidence = (
        float(sum(label_confidences) / len(label_confidences)) if label_confidences else 0.0
    )
    quality = {
        "silhouette": inputs.silhouette,
        "size_variance": inputs.size_variance,
        "outlier_ratio": inputs.outlier_ratio,
        "label_confidence": avg_label_confidence,
        "n_clusters": n_clusters,
        "n_memories": len(memories),
        # Below the #1246 threshold by construction — the guard at the
        # top of this function raised otherwise.
        "labeling_failures": labeling_failures,
    }

    # 4. Update the memory_analyses row. embedding_model was set by
    #    the orchestrator before this transaction opened — we only
    #    finalize the run-level fields here. A concurrent soft-cancel
    #    cannot slip in at this point: the row has been locked since
    #    the step-0 ``with_for_update`` refresh, so the DELETE
    #    handler's own locked re-check blocks until this transaction
    #    commits (#1241; supersedes the #496 refresh-then-write guard
    #    that raced in the window between refresh and commit).
    cost_actual_cents = _compute_actual_cost_cents(totals, dict(analysis.model_snapshot or {}))
    analysis.status = _STATUS_SUCCEEDED
    analysis.finished_at = utcnow()
    analysis.quality = quality
    analysis.cost_actual_cents = cost_actual_cents

    # 5. Sibling sleep_reports row for cost-grade observability.
    #    create_report flushes the row at status='running'; we
    #    immediately complete it with the labeler's PhaseResult so
    #    the per-(provider, model) breakdown lands in
    #    sleep_report_llm_usage with phase='cluster_labeling'.
    sleep_reporter = SleepReporter(db)
    sleep_report = await sleep_reporter.create_report(
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=context_id,
        source=_SOURCE_ANALYSIS,
        paid_by=_SLEEP_PAID_BY_BYOK,
    )

    phase_result = PhaseResult(
        phase_name=_PHASE_CLUSTER_LABELING,
        success=True,
        llm_calls_used=totals.calls,
        tokens_used=totals.input_tokens + totals.output_tokens + totals.cached_input_tokens,
        memories_processed=len(memories),
        details={
            "n_clusters": n_clusters,
            "labeling_failures": quality["labeling_failures"],
        },
        llm_breakdown=breakdowns,
        # #1183: a cluster whose every fallback model raised (ClusterLabel.failed)
        # is a judge failure — without this the sibling row always grades
        # 'completed' even when labeling failed for ALL clusters, reproducing
        # the exact masking bug #1183 closes for the Sleep phases.
        llm_call_failures=quality["labeling_failures"],
    )
    await sleep_reporter.complete_report(sleep_report, [phase_result])

    logger.info(
        "analysis_persist_complete",
        analysis_id=str(analysis.id),
        n_clusters=n_clusters,
        n_memories=len(memories),
        cost_actual_cents=cost_actual_cents,
        labeling_failures=quality["labeling_failures"],
    )


async def persist_failure(
    db: AsyncSession,
    *,
    analysis: MemoryAnalysis,
    error_message: str,
) -> None:
    """Mark the run as failed without writing cluster/assignment rows.

    Called from the orchestrator's outer ``except`` block when a
    pre-Stage[J] stage raises. Runs in its own transaction
    (the compute-stage transaction has already rolled back). The
    orchestrator commits this status update separately so
    ``status='failed'`` is observable to the API caller polling
    the run row.
    """
    truncated_error = error_message[:_ERROR_FIELD_MAX_CHARS]
    # Same cancellation-overwrite guard as ``persist_results`` — refresh
    # from DB so a concurrent DELETE soft-cancel is not silently flipped
    # to ``failed``. The compute exception is still recorded in the log
    # below; we just avoid corrupting the status + cancellation_reason
    # audit trail. Issue #496 Copilot review; #1241 upgraded the refresh
    # to a row lock so a cancel committing between refresh and commit
    # can no longer be overwritten (same protocol as ``persist_results``
    # and the DELETE handler).
    await db.refresh(analysis, with_for_update=True)
    if analysis.status == _STATUS_CANCELLED:
        logger.info(
            "analysis_persist_failure_skipped_due_to_cancel",
            analysis_id=str(analysis.id),
            cancellation_reason=analysis.cancellation_reason,
            compute_error=truncated_error,
        )
        return
    analysis.status = _STATUS_FAILED
    analysis.finished_at = utcnow()
    analysis.error = truncated_error
    # Explicit flush makes the failure status observable BEFORE the
    # surrounding ``async with db.begin()`` block commits — useful for
    # tests that introspect the row inside the same transaction.
    await db.flush()
    # Log the SAME truncated value, not the unbounded original — an
    # upstream exception with a large embedded payload (e.g. dumped
    # response body, stack trace) would otherwise produce log lines
    # bigger than the DB column allows AND leak more bytes than
    # what's actually persisted.
    logger.error(
        "analysis_persist_failure",
        analysis_id=str(analysis.id),
        error=truncated_error,
    )
