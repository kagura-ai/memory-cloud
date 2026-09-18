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
  resolves the run's *lane* (#1569: strict BYOK when the workspace has
  an enabled key, else the platform-managed LLM when the plan carries
  ``managed_llm``) before compute starts, and ``start()`` records it on
  the row (``paid_by`` / ``llm_provider`` / ``llm_model``) so ``run()``
  — on a fresh session — labels on the same lane.

Idempotency surface: the API layer (#496) catches ``ConflictError``
and returns 409 with the existing ``run_id`` so the client can poll
the prior run instead of triggering a duplicate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

# Pre-warm umap + sklearn at module level. Both project_to_2d and
# cluster_high_dim use lazy imports; if two asyncio.to_thread workers
# race to import the same modules concurrently, CPython's per-module
# importlib lock deadlocks (umap imports sklearn internally,
# cluster_high_dim also imports sklearn — circular wait).
import umap  # noqa: F401
from sklearn.cluster import KMeans  # noqa: F401
from sklearn.metrics import silhouette_score  # noqa: F401
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.constraint_names import integrity_error_constraint_name
from models.analysis import MEMORY_ANALYSIS_STATUSES, MemoryAnalysis
from models.auth import User
from models.llm_pricing import LLMPricing
from services.analysis import labeler as analysis_labeler
from services.analysis.byok_resolver import assert_openai_byok_key_available
from services.analysis.clusterer import cluster_high_dim
from services.analysis.llm_lane import AnalysisLane, lane_for_run
from services.analysis.preview import DEFAULT_MODEL_ID, DEFAULT_PROVIDER, estimate_cost
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
from utils.exceptions import ConfigurationError, ConflictError, ValidationError
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


# Default provider / model when params.model_id is None come from
# ``services/analysis/preview.py`` (DEFAULT_PROVIDER / DEFAULT_MODEL_ID)
# directly — used at the only call site below (_resolve_pricing_row).
# v1.5 will replace the constants with a per-workspace
# ``Workspace.analysis_default_model_id`` lookup.

# Status / dimension constants. Use explicit string literals (NOT
# tuple indices) so a future tuple reordering does not silently flip
# the values. The assert pins the contract that the literal must
# remain a member of the canonical tuple — if someone removes
# "running" from MEMORY_ANALYSIS_STATUSES the import-time assertion
# fires loud at startup rather than at the first INSERT. ``paid_by``
# comes from the lane since #1569 (``services/analysis/llm_lane.py``).
_STATUS_RUNNING = "running"
assert _STATUS_RUNNING in MEMORY_ANALYSIS_STATUSES, (
    f"_STATUS_RUNNING out of sync with MEMORY_ANALYSIS_STATUSES: {MEMORY_ANALYSIS_STATUSES}"
)


def _params_iso_to_naive_utc(s: str | None, *, end_of_day: bool = False) -> datetime | None:
    """Parse an ISO-8601 string into a naive UTC datetime.

    The API layer (#496) accepts both naive and tz-aware datetimes in
    the params payload. Here we normalize to naive UTC so the filter
    binds cleanly against ``Memory.created_at`` (TIMESTAMP WITHOUT
    TIME ZONE — naive UTC by repo convention #489). Without this
    normalization, asyncpg raises a binding error when a tz-aware
    bound parameter is compared against a naive column.

    When ``end_of_day=True`` AND the input is a **date-only** string
    (e.g. ``"2026-05-28"``, no ``T`` separator), the result is shifted
    to the *start of the next day* so that downstream filters using
    ``Memory.created_at < to_dt`` are effectively day-inclusive (#820).
    The UI date picker surfaces ``to`` as the last *included* date;
    without the shift, ``to=2026-05-28`` would silently exclude every
    memory created on 2026-05-28. Strings carrying a time component
    are passed through unchanged so callers that need precise-time
    bounds keep the original semantics.
    """
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    # Date-only detection: ISO-8601 date is exactly 10 chars ("YYYY-MM-DD")
    # and contains no time separator. Anything richer (datetime, tz suffix)
    # is treated as caller-controlled precision.
    if end_of_day and len(s) == 10 and "T" not in s:
        dt = dt + timedelta(days=1)
    return dt


def _validate_date_params(params: AnalysisParams) -> None:
    """Reject malformed ``from``/``to`` BEFORE any row is created (#1240).

    Runs the exact parser ``run()`` will use (``_params_iso_to_naive_utc``)
    on the serialized params, so start-time acceptance and run-time parsing
    cannot diverge. REST already rejects at the Pydantic boundary
    (``AnalysisPreviewRequest._validate_iso8601``); this guard covers MCP —
    which delivers raw strings via ``AnalysisParams.extra`` — and any future
    entrypoint. Without it a malformed date raises in ``run()`` AFTER the
    ``memory_analyses`` row is INSERTed at status='running', stranding the
    run: the quota slot stays consumed and the idempotency guard 409-blocks
    every future run on the context.
    """
    jsonb = params.to_jsonb()
    for key, end_of_day in (("from", False), ("to", True)):
        value = jsonb.get(key)
        try:
            _params_iso_to_naive_utc(value, end_of_day=end_of_day)
        except (ValueError, TypeError) as e:
            raise ValidationError(
                f"Invalid ISO-8601 datetime for {key!r}: {value!r}. "
                "Expected forms: date-only '2026-05-02', '2026-05-02T00:00:00Z', "
                "'2026-05-02T09:00:00+09:00', or naive 'YYYY-MM-DDTHH:MM:SS'.",
                field=key,
            ) from e


async def _resolve_pricing_row(
    db: AsyncSession,
    model_id: int | None,
    *,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL_ID,
) -> tuple[LLMPricing, dict]:
    """Resolve the ``llm_pricing`` row + frozen snapshot for the run.

    Single SELECT pulls every (provider, model, effective_from) sibling
    so the snapshot's per-unit-type rate map is built without a
    second round-trip. ``primary`` is the row with the latest
    ``effective_from`` (or the row matching ``model_id`` when given);
    sibling rows for that same effective_from contribute the rate
    map ('input_tokens', 'output_tokens', 'cache_read_tokens', ...).

    ``provider`` / ``model`` name the default-path lookup target: the
    BYOK lane's OpenAI default, or the managed lane's model (#1569).
    """
    if model_id is not None:
        # When the caller pins a specific row, look up its (provider,
        # model, effective_from) triple and pull all sibling rates in
        # one shot via a self-join CTE expressed inline.
        target_stmt = select(LLMPricing).where(LLMPricing.id == model_id)
        target = (await db.execute(target_stmt)).scalar_one_or_none()
        if target is None:
            # Caller-supplied model_id refers to a non-existent row →
            # client input error (422), NOT a 409 conflict.
            raise ValidationError(
                f"No LLM pricing row found for model_id={model_id}.",
                field="model_id",
                model_id=model_id,
            )
        rate_stmt = select(LLMPricing).where(
            LLMPricing.provider == target.provider,
            LLMPricing.model == target.model,
            LLMPricing.effective_from == target.effective_from,
        )
        all_rows = list((await db.execute(rate_stmt)).scalars().all())
        primary = target
    else:
        # Default path: pull every row for (provider, model) ordered by
        # effective_from desc with a stable tie-breaker on ``unit_type``
        # then ``id``. Without the tie-breaker the ``primary`` row chosen
        # for ``MemoryAnalysis.model_id`` (the FK we persist) varies
        # between equally-recent rows (input_tokens / output_tokens /
        # cache_read_tokens all share the same ``effective_from``). The
        # deterministic order pins the FK to one specific row across
        # DBs / re-runs.
        stmt = (
            select(LLMPricing)
            .where(
                LLMPricing.provider == provider,
                LLMPricing.model == model,
            )
            .order_by(
                LLMPricing.effective_from.desc(),
                LLMPricing.unit_type,
                LLMPricing.id,
            )
        )
        all_rows = list((await db.execute(stmt)).scalars().all())
        if not all_rows:
            # Default-model fallback path: missing rows mean the seed
            # migration didn't run or was rolled back (or, on the managed
            # lane, no LLM_PRICING_OVERRIDES entry) → server-side
            # configuration error (500), NOT a 409 conflict.
            raise ConfigurationError(
                f"LLM pricing row not found for {provider}/{model}. Run alembic "
                "migrations to seed `llm_pricing`, or price the model with "
                "LLM_PRICING_OVERRIDES."
            )
        primary = all_rows[0]

    # Filter to the same effective_from group as primary so a stale
    # historical row doesn't pollute the snapshot's rate map.
    rate_rows = [r for r in all_rows if r.effective_from == primary.effective_from]
    snapshot = {
        "provider": primary.provider,
        "model": primary.model,
        "effective_from": (primary.effective_from.isoformat() if primary.effective_from else None),
        # Rates are USD per MILLION units, whatever the row's
        # ``unit_denominator`` (#1570: operator overrides may use another
        # denominator; the seed rows are all per-1M so this is a no-op for
        # them). ``preview.estimate_cost`` and
        # ``reporter._compute_actual_cost_cents`` both divide by 1e6.
        "rates": {
            r.unit_type: float(r.price_per_unit) * 1_000_000 / float(r.unit_denominator)
            for r in rate_rows
        },
    }
    return primary, snapshot


async def try_resolve_pricing_row(
    db: AsyncSession,
    model_id: int | None,
    *,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL_ID,
) -> tuple[LLMPricing, dict] | None:
    """``_resolve_pricing_row`` for the preview + managed-lane paths (#1570).

    Returns ``None`` instead of raising ``ConfigurationError`` when the
    model has no ``llm_pricing`` rows, so a deployment that has not
    seeded / priced its analysis model gets ``estimated_cost_cents=null``
    from ``/preview`` rather than a 500. A caller-pinned ``model_id`` that
    does not exist still raises ``ValidationError`` (client input error).
    The BYOK run path keeps ``_resolve_pricing_row``; the managed lane
    (#1569) uses this and runs with ``model_id=NULL`` when unpriced.
    """
    try:
        return await _resolve_pricing_row(db, model_id, provider=provider, model=model)
    except ConfigurationError:
        return None


def _unpriced_snapshot(lane: AnalysisLane) -> dict:
    """Snapshot for a managed-lane model with no ``llm_pricing`` row (#1569).

    Same shape as ``_resolve_pricing_row``'s but with an empty rate map, so
    ``estimate_cost`` and ``reporter._compute_actual_cost_cents`` both yield
    ``None`` ("cost unknown", #1570) instead of guessing.
    """
    return {
        "provider": lane.provider,
        "model": lane.primary_model,
        "effective_from": None,
        "rates": {},
    }


async def resolve_lane_pricing(
    db: AsyncSession, lane: AnalysisLane, model_id: int | None
) -> tuple[LLMPricing | None, dict]:
    """Pricing row + snapshot for the lane a run will label on (#1569).

    The BYOK lane keeps the strict pre-#1569 contract (its OpenAI default
    is seeded by Alembic, so a missing row is a deployment fault → 500).
    The managed lane must run even when the operator has not priced its
    model: ``None`` row, empty-rate snapshot, ``cost_*_cents`` stay NULL.
    A caller-pinned ``model_id`` is honoured on both lanes.
    """
    if lane.kind == "byok":
        return await _resolve_pricing_row(db, model_id)
    resolved = await try_resolve_pricing_row(
        db, model_id, provider=lane.provider, model=lane.primary_model
    )
    if resolved is not None:
        return resolved
    logger.info(
        "analysis_managed_model_unpriced",
        provider=lane.provider,
        model=lane.primary_model,
        hint="cost recorded as unknown; set LLM_PRICING_OVERRIDES to track it",
    )
    return None, _unpriced_snapshot(lane)


class AnalysisOrchestrator:
    """Coordinator for one Memory Analysis run.

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

    async def start(
        self,
        *,
        workspace_id: UUID,
        context_id: UUID,
        user_id: str,
        params: AnalysisParams,
    ) -> MemoryAnalysis:
        """Phase 1 (synchronous).

        - Resolves the run's lane (#1569): strict BYOK when the workspace
          has an enabled OpenAI key, else the platform-managed LLM when
          the plan carries ``managed_llm`` (raises ValidationError → 422
          at API layer when neither applies).
        - Idempotency check: a prior run at status='running' for the
          same (workspace, context) raises ConflictError (409). The
          API layer surfaces this with the prior run's ``run_id``.
        - Pre-flight cost estimate.
        - Resolves the pricing row + builds the model_snapshot for the
          lane's model (NULL row / empty rates when a managed model is
          unpriced — the run still proceeds, cost unknown).
        - Creates the ``memory_analyses`` row at status='running'
          and flushes — caller commits.

        Returns the freshly-flushed (uncommitted) analysis row so the
        caller can ``await db.commit()`` and return ``analysis.id``
        in the 202 response.
        """
        _validate_date_params(params)

        lane = await assert_openai_byok_key_available(
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
            # ``run_id`` lands in ConflictError.details so #496's API/MCP
            # handlers can surface it as a structured field (not just
            # parsed out of the message text).
            raise ConflictError(
                f"An analysis run is already in progress "
                f"(run_id={prior.id}). Wait for it to finish or cancel.",
                run_id=str(prior.id),
            )

        pricing, snapshot = await resolve_lane_pricing(self.db, lane, params.model_id)

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
            model_id=pricing.id if pricing is not None else None,
            model_snapshot=snapshot,
            # #1569: the lane record ``run()`` rebuilds the lane from.
            llm_provider=lane.provider,
            llm_model=lane.primary_model,
            embedding_model="(pending)",
            params=params.to_jsonb(),
            input_count=0,  # filled in by run() once vector_pull resolves
            cost_estimated_cents=None,
            paid_by=lane.paid_by,
            status=_STATUS_RUNNING,
        )
        self.db.add(analysis)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            # Only the partial unique index means "lost the race with a
            # concurrent start" — other IntegrityErrors (e.g. the contexts
            # CASCADE FK after a mid-request context delete, or the
            # llm_pricing RESTRICT FK) are NOT a conflicting run and must
            # not be translated to a 409 telling the user to wait for a
            # run that does not exist. Re-raise those for the generic
            # 500 path. Structured diagnostics first (asyncpg/psycopg via
            # the shared helper); message substring only as the fallback
            # for wrapper shapes without them — so an unusual driver
            # chain degrades to the old behavior, never to a wrong 409.
            constraint = integrity_error_constraint_name(exc)
            if constraint is not None:
                is_running_race = constraint == "uq_memory_analyses_one_running"
            else:
                is_running_race = "uq_memory_analyses_one_running" in str(exc.orig)
            if not is_running_race:
                raise
            # Lost the race with a concurrent start: the partial unique
            # index ``uq_memory_analyses_one_running`` rejected a second
            # 'running' row for this (workspace, context) — the SELECT
            # guard above is check-then-insert and cannot see a row the
            # racing transaction has not committed yet (#1240). Roll back
            # so the session is usable, then surface the same 409 the
            # guard raises, with the winner's run_id when visible.
            await self.db.rollback()
            prior = (await self.db.execute(running_stmt)).scalar_one_or_none()
            raise ConflictError(
                "An analysis run is already in progress"
                + (f" (run_id={prior.id})" if prior is not None else "")
                + ". Wait for it to finish or cancel.",
                run_id=str(prior.id) if prior is not None else None,
            ) from exc
        logger.info(
            "analysis_run_started",
            analysis_id=str(analysis.id),
            workspace_id=str(workspace_id),
            context_id=str(context_id),
            user_id=user_id,
            model=snapshot["model"],
            provider=lane.provider,
            lane=lane.kind,
            paid_by=lane.paid_by,
            priced=pricing is not None,
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

        try:
            # Normalize tz-aware ISO strings to NAIVE UTC so the filter
            # binds cleanly against ``Memory.created_at`` (TIMESTAMP WITHOUT
            # TIME ZONE — naive UTC by repo convention #489). API callers
            # may submit either tz-aware (e.g. "...+09:00") or naive ISO
            # strings; both routes converge here. ``start()`` validates
            # these pre-INSERT (#1240); parsing INSIDE the try keeps a
            # malformed value on a pre-#1240 row from stranding the run
            # at 'running' — it lands in ``_mark_failed`` like any other
            # stage failure.
            from_dt = _params_iso_to_naive_utc(params_jsonb.get("from"))
            # ``to`` is inclusive at the day level for date-only inputs — see
            # _params_iso_to_naive_utc / #820. Datetimes with time components
            # stay exclusive so callers needing precision are not surprised.
            to_dt = _params_iso_to_naive_utc(params_jsonb.get("to"), end_of_day=True)

            # Stage [C] — pull memories + their existing Qdrant vectors.
            # This is the LAST DB-using step before compute. After it
            # we commit the read-side state and release the connection
            # back to the pool — KMeans / UMAP / labeler work without
            # holding an orchestrator-level DB connection.
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
                rates=snapshot_dict.get("rates"),
                model_id=str(snapshot_dict.get("model", DEFAULT_MODEL_ID)),
            )
            analysis.cost_estimated_cents = estimate.estimated_cost_cents

            # Issue #542: resolve user's locale for prompt language.
            # Fetch while we still have the read transaction open;
            # committing immediately after releases the connection
            # before the long-running compute stages below.
            user_result = await self.db.execute(
                select(User.locale).where(User.user_id == analysis.triggered_by)
            )
            db_locale = user_result.scalar_one_or_none()
            label_locale = db_locale if db_locale else "en"

            # Commit the read + vector_pull state, releasing the
            # orchestrator's connection. Compute stages below run with
            # NO open transaction; ``persist_results`` autobegins a
            # fresh transaction on its first SQL op. Without this commit
            # the connection would stay checked out for the whole
            # ~2-20s compute window. (Per Copilot review on PR #530.)
            await self.db.commit()

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
            # ``label_clusters`` no longer takes ``llm_service`` — each
            # cluster task opens its own ``AsyncSession`` to avoid the
            # SQLAlchemy concurrent-ops violation on the orchestrator's
            # shared session. See labeler.py for the per-task pattern.
            # The lane comes from the row ``start()`` wrote (#1569), so a
            # managed-lane run never drifts back onto the workspace key.
            lane = lane_for_run(
                paid_by=analysis.paid_by,
                provider=analysis.llm_provider,
                model=analysis.llm_model,
            )
            cluster_label_results = await analysis_labeler.label_clusters(
                cluster_labels=cluster_result.labels,
                centroids=cluster_result.centroids,
                embeddings=pull.embeddings,
                memories=pull.memories,
                user_id=analysis.triggered_by,
                workspace_id=str(analysis.workspace_id),
                context_id=str(analysis.context_id),
                locale=label_locale,
                lane=lane,
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
            # The previous ``await self.db.commit()`` after vector_pull
            # released the connection. ``persist_results`` autobegins a
            # FRESH transaction on its first SQL op (it adds rows and
            # flushes), wrapping the cluster + assignment + sleep_reports
            # writes in a single all-or-nothing tx. The caller of run()
            # owns the session lifecycle (closes it on task exit).
            #
            # The ``analysis`` ORM instance was loaded BEFORE the
            # earlier commit; SQLAlchemy with ``expire_on_commit=False``
            # keeps the in-memory state intact and the next op
            # re-attaches it to the new transaction.
            try:
                await persist_results(
                    self.db,
                    inputs=inputs,
                    user_id=analysis.triggered_by,
                    workspace_id=str(analysis.workspace_id),
                    context_id=str(analysis.context_id),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

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
        """Persist the failed status in its own commit boundary.

        The compute-stage transaction has already rolled back via
        ``persist_results``' caller-managed try/except. We commit the
        status update separately so ``status='failed'`` is observable
        to the API caller polling the run row.

        Failures raised OUTSIDE that try (vector_pull's SELECT, the
        mid-run commit, the locale fetch) can leave the session's
        transaction in a failed state; roll back FIRST so
        ``persist_failure``'s refresh/UPDATE runs on a clean
        transaction. Without this the refresh raises
        ``PendingRollbackError`` and the row is stranded at
        status='running' — permanently 409-blocking the context via
        the idempotency guard (#1240).
        """
        try:
            await self.db.rollback()
        except Exception:
            logger.warning(
                "analysis_mark_failed_rollback_error",
                analysis_id=str(analysis.id),
                exc_info=True,
            )
        try:
            await persist_failure(self.db, analysis=analysis, error_message=error_message)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
