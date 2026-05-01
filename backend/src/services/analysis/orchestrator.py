"""``AnalysisOrchestrator`` — coordinates Stages [B] through [J].

Phase 4 architecture decisions (Pragmatic):

- **Linear stage calls** with named coroutines, not the sleep-style
  fail-isolated phase loop. Analysis is all-or-nothing across four
  tables; isolation between stages would create partial state.
- **Two transaction boundaries**:
  1. The pre-flight idempotency check + ``memory_analyses`` create
     happen in the request session and commit immediately so the
     202 caller (#496 API) gets back a ``run_id``.
  2. The compute-and-persist work runs inside ``async with
     db.begin()`` against a **fresh** session opened by the task
     entry point (``tasks/analysis_tasks.py``), so the all-or-nothing
     transaction wraps Stage [J] only — long-running compute work
     does not hold a DB connection.
- **Idempotency guard**: a pre-existing
  ``memory_analyses(status='running', workspace_id=W, context_id=C)``
  row raises ``ConflictError`` (409). The crashed-run cleanup is
  out of scope for this PR and matches sleep's known limitation
  (operator manually marks the run cancelled before retry).
- **BYOK key**: the orchestrator does not load or hold the key.
  ``LLMService.complete_json`` resolves it inside its own coroutine
  frame on each call. The pre-flight ``assert_openai_byok_key_available``
  ensures the workspace has an enabled key before compute starts.

Idempotency surface: the API layer (#496) catches ``ConflictError``
and returns 409 with the existing ``run_id`` so the client can poll
the prior run instead of triggering a duplicate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.analysis import (
    MEMORY_ANALYSIS_PAID_BY_VALUES,
    MEMORY_ANALYSIS_STATUSES,
    MemoryAnalysis,
)
from models.llm_pricing import LLMPricing
from services.analysis import labeler as analysis_labeler
from services.analysis.byok_resolver import assert_openai_byok_key_available
from services.analysis.clusterer import cluster_high_dim
from services.analysis.preview import estimate_cost
from services.analysis.projector import project_to_2d
from services.analysis.reporter import (
    PersistInputs,
    persist_failure,
    persist_results,
)
from services.analysis.vector_pull import (
    EmbeddingMismatchError,
    pull_memories_with_vectors,
)
from services.llm_service import LLMService
from utils.exceptions import ConflictError
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AnalysisParams:
    """Run parameters captured in ``memory_analyses.params`` JSONB.

    Fields mirror the issue spec preview/POST contract. ``query`` is
    forwarded unchanged to v1.5; v1 ignores it (the analysis pipeline
    operates on the full filtered set, not query results).
    """

    from_dt: datetime | None = None
    to_dt: datetime | None = None
    types: list[str] | None = None
    tags: list[str] | None = None
    min_importance: float | None = None
    query: str | None = None
    model_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_jsonb(self) -> dict:
        return {
            "from": self.from_dt.isoformat() if self.from_dt else None,
            "to": self.to_dt.isoformat() if self.to_dt else None,
            "types": self.types,
            "tags": self.tags,
            "min_importance": self.min_importance,
            "query": self.query,
            "model_id": self.model_id,
            **self.extra,
        }


# Default model when params.model_id is None — v1 default is gpt-5-nano
# (Phase 3 clarification). v1.5 will pull this from a workspace-level
# default like ``Workspace.analysis_default_model_id``.
_DEFAULT_PROVIDER = "openai"
_DEFAULT_MODEL = "gpt-5-nano"

# Status values pulled from the model-side tuple so a re-ordering of
# the tuple doesn't silently change call-site semantics.
_STATUS_RUNNING = MEMORY_ANALYSIS_STATUSES[0]  # "running"
_PAID_BY_BYOK = MEMORY_ANALYSIS_PAID_BY_VALUES[0]  # "byok"


async def _resolve_pricing_row(db: AsyncSession, model_id: int | None) -> tuple[LLMPricing, dict]:
    """Resolve the ``llm_pricing`` row + frozen snapshot for the run.

    Single SELECT pulls every (provider, model, effective_from) sibling
    so the snapshot's per-unit-type rate map is built without a
    second round-trip. ``primary`` is the row with the latest
    ``effective_from`` (or the row matching ``model_id`` when given);
    sibling rows for that same effective_from contribute the rate
    map ('input_tokens', 'output_tokens', 'cache_read_tokens', ...).
    """
    if model_id is not None:
        # When the caller pins a specific row, look up its (provider,
        # model, effective_from) triple and pull all sibling rates in
        # one shot via a self-join CTE expressed inline.
        target_stmt = select(LLMPricing).where(LLMPricing.id == model_id)
        target = (await db.execute(target_stmt)).scalar_one_or_none()
        if target is None:
            raise ConflictError(f"No LLM pricing row found for model_id={model_id}.")
        rate_stmt = select(LLMPricing).where(
            LLMPricing.provider == target.provider,
            LLMPricing.model == target.model,
            LLMPricing.effective_from == target.effective_from,
        )
        all_rows = list((await db.execute(rate_stmt)).scalars().all())
        primary = target
    else:
        # Default path: pull every row for (provider=openai, model=gpt-5-nano)
        # ordered by effective_from desc, then take the latest
        # effective_from group as primary + its rates. One SELECT.
        stmt = (
            select(LLMPricing)
            .where(
                LLMPricing.provider == _DEFAULT_PROVIDER,
                LLMPricing.model == _DEFAULT_MODEL,
            )
            .order_by(LLMPricing.effective_from.desc())
        )
        all_rows = list((await db.execute(stmt)).scalars().all())
        if not all_rows:
            raise ConflictError(
                f"No LLM pricing row found for model_id={model_id} (or default gpt-5-nano)."
            )
        primary = all_rows[0]

    # Filter to the same effective_from group as primary so a stale
    # historical row doesn't pollute the snapshot's rate map.
    rate_rows = [r for r in all_rows if r.effective_from == primary.effective_from]
    snapshot = {
        "provider": primary.provider,
        "model": primary.model,
        "effective_from": (primary.effective_from.isoformat() if primary.effective_from else None),
        "rates": {r.unit_type: float(r.price_per_unit) for r in rate_rows},
    }
    return primary, snapshot


class AnalysisOrchestrator:
    """Coordinator for one broadlistening run.

    Usage from ``tasks/analysis_tasks.py``:

        orchestrator = AnalysisOrchestrator(db)
        analysis = await orchestrator.start(
            workspace_id=..., context_id=..., user_id=..., params=...
        )
        # ... commit so 202 caller can return analysis.id ...
        await orchestrator.run(analysis_id=analysis.id, params=params)

    ``start()`` is the synchronous part (idempotency check + create
    row + commit). ``run()`` is the long-running part that opens its
    own ``async with db.begin()`` block for Stage [J].
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.llm_service = LLMService(db)

    async def start(
        self,
        *,
        workspace_id: UUID,
        context_id: UUID,
        user_id: str,
        params: AnalysisParams,
    ) -> MemoryAnalysis:
        """Phase 1 (synchronous).

        - Asserts BYOK key exists (raises ConfigurationError → 422
          at API layer).
        - Idempotency check: a prior run at status='running' for the
          same (workspace, context) raises ConflictError (409). The
          API layer surfaces this with the prior run's ``run_id``.
        - Pre-flight cost estimate.
        - Resolves the pricing row + builds the model_snapshot.
        - Creates the ``memory_analyses`` row at status='running'
          and flushes — caller commits.

        Returns the freshly-flushed (uncommitted) analysis row so the
        caller can ``await db.commit()`` and return ``analysis.id``
        in the 202 response.
        """
        await assert_openai_byok_key_available(
            self.db,
            workspace_id=workspace_id,
            context_id=context_id,
        )

        # Idempotency: refuse to start a second concurrent run.
        running_stmt = select(MemoryAnalysis).where(
            and_(
                MemoryAnalysis.workspace_id == workspace_id,
                MemoryAnalysis.context_id == context_id,
                MemoryAnalysis.status == _STATUS_RUNNING,
            )
        )
        prior = (await self.db.execute(running_stmt)).scalar_one_or_none()
        if prior is not None:
            raise ConflictError(
                f"An analysis run is already in progress "
                f"(run_id={prior.id}). Wait for it to finish or cancel."
            )

        pricing, snapshot = await _resolve_pricing_row(self.db, params.model_id)

        # ``cost_estimated_cents`` is left NULL at start time — the
        # filter count is not known until ``vector_pull`` runs in
        # Phase 2. The B3 #496 ``/preview`` endpoint will compute the
        # user-facing estimate via ``estimate_cost(...)`` separately.
        # Phase 2 fills this column in once the actual count is known
        # so post-run "estimated vs actual" deltas are meaningful.
        analysis = MemoryAnalysis(
            workspace_id=workspace_id,
            context_id=context_id,
            triggered_by=user_id,
            model_id=pricing.id,
            model_snapshot=snapshot,
            embedding_model="(pending)",
            params=params.to_jsonb(),
            input_count=0,  # filled in by run() once vector_pull resolves
            cost_estimated_cents=None,
            paid_by=_PAID_BY_BYOK,
            status=_STATUS_RUNNING,
        )
        self.db.add(analysis)
        await self.db.flush()
        logger.info(
            "analysis_run_started",
            analysis_id=str(analysis.id),
            workspace_id=str(workspace_id),
            context_id=str(context_id),
            user_id=user_id,
            model=pricing.model,
        )
        return analysis

    async def run(self, *, analysis_id: UUID) -> None:
        """Phase 2 (long-running).

        Loads the run row by id (must exist at status='running'),
        runs Stages [C] through [J], and finalizes status. On any
        failure before Stage [J] commits, marks status='failed'
        with the exception message in ``error`` (committed in a
        separate transaction so the failed status is observable).
        """
        analysis = await self.db.get(MemoryAnalysis, analysis_id)
        if analysis is None:
            raise ConflictError(f"Analysis run {analysis_id} not found at run() time.")
        if analysis.status != _STATUS_RUNNING:
            raise ConflictError(
                f"Analysis run {analysis_id} is in status={analysis.status!r}; "
                f"expected {_STATUS_RUNNING!r}."
            )

        params_jsonb = dict(analysis.params or {})
        from_dt = datetime.fromisoformat(params_jsonb["from"]) if params_jsonb.get("from") else None
        to_dt = datetime.fromisoformat(params_jsonb["to"]) if params_jsonb.get("to") else None

        try:
            # Stage [C] — pull memories + their existing Qdrant vectors.
            pull = await pull_memories_with_vectors(
                self.db,
                workspace_id=analysis.workspace_id,
                context_id=analysis.context_id,
                from_dt=from_dt,
                to_dt=to_dt,
                types=params_jsonb.get("types"),
                tags=params_jsonb.get("tags"),
                min_importance=params_jsonb.get("min_importance"),
            )
            analysis.input_count = len(pull.memories)
            analysis.embedding_model = pull.embedding_model
            # Now that the actual filter count is known, set
            # ``cost_estimated_cents`` so the post-run estimated-vs-actual
            # delta on ``memory_analyses`` row is meaningful.
            snapshot_dict = dict(analysis.model_snapshot or {})
            estimate = estimate_cost(
                memory_count=len(pull.memories),
                model_id=str(snapshot_dict.get("model", "gpt-5-nano")),
            )
            analysis.cost_estimated_cents = estimate.estimated_cost_cents

            # Stages [D] and [E] are CPU-bound (sklearn KMeans + UMAP).
            # Run them concurrently in worker threads so the asyncio
            # event loop is not blocked for the combined ~2-20s of
            # compute on an 8000-memory run. Both stages consume the
            # same embedding matrix and produce independent outputs.
            cluster_result, coords_2d = await asyncio.gather(
                asyncio.to_thread(cluster_high_dim, pull.embeddings),
                asyncio.to_thread(project_to_2d, pull.embeddings),
            )

            # Stage [F + G] — representative selection + LLM labeling.
            cluster_label_results = await analysis_labeler.label_clusters(
                cluster_labels=cluster_result.labels,
                centroids=cluster_result.centroids,
                embeddings=pull.embeddings,
                memories=pull.memories,
                llm_service=self.llm_service,
                user_id=analysis.triggered_by,
                workspace_id=str(analysis.workspace_id),
                context_id=str(analysis.context_id),
            )

            # Stage [J] — atomic persist. The transaction here wraps
            # all four-table writes; reporter does NOT open its own.
            inputs = PersistInputs(
                analysis=analysis,
                memories=pull.memories,
                embeddings=pull.embeddings,
                cluster_labels=cluster_result.labels,
                coords_2d=coords_2d,
                cluster_results=cluster_label_results,
                silhouette=cluster_result.silhouette,
                size_variance=cluster_result.size_variance,
                outlier_ratio=cluster_result.outlier_ratio,
                window_from=from_dt,
                window_to=to_dt,
            )
            async with self.db.begin():
                await persist_results(
                    self.db,
                    inputs=inputs,
                    user_id=analysis.triggered_by,
                    workspace_id=str(analysis.workspace_id),
                    context_id=str(analysis.context_id),
                )

            logger.info(
                "analysis_run_succeeded",
                analysis_id=str(analysis.id),
                n_memories=len(pull.memories),
                n_clusters=cluster_result.n_clusters,
            )

        except EmbeddingMismatchError as e:
            # ValidationError subclass — surface as 422 by the API layer.
            await self._mark_failed(analysis, str(e))
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(
                "analysis_run_failed",
                analysis_id=str(analysis.id),
                error=str(e),
                exc_info=True,
            )
            await self._mark_failed(analysis, str(e))
            raise

    async def _mark_failed(self, analysis: MemoryAnalysis, error_message: str) -> None:
        """Persist the failed status in its own transaction."""
        async with self.db.begin():
            await persist_failure(self.db, analysis=analysis, error_message=error_message)
