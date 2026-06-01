"""Cost aggregation service (Issue #472).

Computes LLM + embedding usage and cost (`$`) breakdowns from the
cost-grade ``sleep_reports`` schema introduced in #471 and extended in
#523. Powers two REST endpoints (admin cross-workspace,
workspace-scoped) that share this service so the SQL pipeline lives in
one place.

Aggregation key: ``(period, workspace_id, user_id)`` with two breakdown
arrays per row — by model and by source.

Cost split:
- ``cost_usd``    — sum over rows where ``paid_by='platform'``
- ``cost_usd_byok`` — sum over rows where ``paid_by='byok'``

The split is enforced at the GROUP BY level (``paid_by`` is a grouping
column) so it cannot accidentally re-merge in application code.

Pricing lookup: each LLM unit (input / output / cache_read) and the
embedding unit are joined to the active ``llm_pricing`` row via a
LATERAL subquery that picks the latest ``effective_from <= started_at``.
A miss leaves the corresponding cost as ``NULL`` / ``None`` ("cost
unknown" — same semantics as ``LLMPricingService.lookup()`` returning
``None`` on a miss). NULL is sticky: if any contributing usage row
in a (period, workspace, user, model, source, paid_by) bucket is
unpriced, the bucket's cost is ``None`` so that partial totals don't
look like real money. Usage figures (calls, tokens) are still surfaced
even when cost is unknown.

v1 limitation: pricing-tier lookup uses ``context_min_tokens=0`` because
``sleep_report_llm_usage`` does not store the per-call context window
size. Multi-tier models (Gemini 2.5 Pro doubles past 200k) are
under-estimated for the high-tier portion. The dashboard child (#473)
will surface this caveat; a future schema iteration can add a context
size column.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.sleep import SLEEP_REPORT_PAID_BY_VALUES, SLEEP_REPORT_SOURCES
from utils.logger import get_logger

logger = get_logger(__name__)


# Allowed period values map to PostgreSQL ``date_trunc`` precision tokens.
# Bound to a literal allowlist before reaching the SQL — even though the
# value is bound (not interpolated), passing arbitrary strings would 500
# the query when ``date_trunc`` rejects them.
VALID_PERIODS: tuple[str, ...] = ("day", "week", "month")

# Maximum span of a single aggregation request, in days. Mirrors the
# frontend's UI soft cap (``CostDashboard.tsx`` ``MAX_LOOKBACK_DAYS = 365``)
# — kept intentionally identical so a server-side rejection never contradicts
# a selection the UI permits. This is the defense-in-depth layer (#528): a
# non-UI caller (curl / SDK / MCP) that bypasses the UI cap must not be able
# to scan years of ``sleep_reports``. The window is half-open ``[start, end)``,
# so ``end - start`` equals the UI's inclusive day count (``end = to + 1 day``).
MAX_LOOKBACK_DAYS = 365


def window_exceeds_cap(start: datetime, end: datetime) -> bool:
    """True if the half-open window ``[start, end)`` is wider than the cap.

    Single source of truth for the off-by-one boundary so the route
    (``_parse_window`` → 400) and the service (``aggregate`` → ValueError)
    enforce the *same* threshold from two layers without the literal
    drifting between them. ``> timedelta`` (not ``.days >``) rejects a
    sub-day overage too, which a direct service caller could pass.
    """
    return end - start > timedelta(days=MAX_LOOKBACK_DAYS)


class CostBreakdownByModel:
    """Per-model cost breakdown row inside a CostAggregationRow.

    Plain ``__slots__`` container — rounding to display precision and
    JSON serialization happen at the route boundary, not here.
    """

    __slots__ = ("model", "calls", "cost_usd", "cost_usd_byok")

    def __init__(
        self,
        model: str | None,
        calls: int,
        cost_usd: float | None,
        cost_usd_byok: float | None,
    ) -> None:
        self.model = model
        self.calls = calls
        self.cost_usd = cost_usd
        self.cost_usd_byok = cost_usd_byok


class CostBreakdownBySource:
    """Per-source cost breakdown row inside a CostAggregationRow."""

    __slots__ = ("source", "calls", "cost_usd", "cost_usd_byok")

    def __init__(
        self,
        source: str,
        calls: int,
        cost_usd: float | None,
        cost_usd_byok: float | None,
    ) -> None:
        self.source = source
        self.calls = calls
        self.cost_usd = cost_usd
        self.cost_usd_byok = cost_usd_byok


class CostAggregationRow:
    """One (period × workspace × user) aggregation row.

    Plain attribute container so the route handler can convert to the
    Pydantic response model without an extra serialization step.
    """

    __slots__ = (
        "period_start",
        "workspace_id",
        "user_id",
        "calls",
        "tokens_in",
        "tokens_out",
        "tokens_cached_in",
        "tokens_cache_write",
        "embedding_tokens",
        "cost_usd",
        "cost_usd_byok",
        "cost_breakdown_by_model",
        "cost_breakdown_by_source",
    )

    def __init__(
        self,
        period_start: date,
        workspace_id: UUID | None,
        user_id: str,
    ) -> None:
        self.period_start = period_start
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_cached_in = 0
        self.tokens_cache_write = 0
        self.embedding_tokens = 0
        # cost_usd / cost_usd_byok are NULL ("cost unknown") if any
        # contributing SQL row had unresolved pricing. They start at 0.0
        # and become None on the first NULL contribution; once None, they
        # stay None (NULL is sticky — partial cost would be misleading).
        self.cost_usd: float | None = 0.0
        self.cost_usd_byok: float | None = 0.0
        self.cost_breakdown_by_model: list[CostBreakdownByModel] = []
        self.cost_breakdown_by_source: list[CostBreakdownBySource] = []


# ----------------------------------------------------------------------
# SQL fragments
# ----------------------------------------------------------------------

# LATERAL pricing lookup template. The ``context_min_tokens=0`` clause
# pins to the lowest tier (see module docstring for the v1 limitation).
# ``{unit_type}`` and ``{alias}`` are filled via Python ``.format()`` at
# query build time — both arguments are HARDCODED constants from the
# call sites below (``"input_tokens"``, ``"output_tokens"``,
# ``"cache_read_tokens"`` and the matching ``p_in`` / ``p_out`` /
# ``p_cache`` aliases), never user input. SQL injection is impossible
# even though these are interpolated rather than bound. Filter VALUES
# (``:start``, ``:end``, ``:workspace_id`` etc.) ARE bound parameters
# in the outer query, per the project's ``security.md`` rule.
_LLM_PRICING_LATERAL = """
LEFT JOIN LATERAL (
    SELECT (price_per_unit / unit_denominator)::float8 AS rate
    FROM llm_pricing
    WHERE provider = u.provider
      AND model = u.model
      AND unit_type = '{unit_type}'
      AND effective_from <= sr.started_at
      AND context_min_tokens = 0
      AND pricing_model != 'subscription'
    ORDER BY effective_from DESC
    LIMIT 1
) {alias} ON TRUE
"""

# Embedding pricing lateral — joined against sleep_reports directly,
# guarded by NOT NULL so old rows (embedding_provider IS NULL) skip the
# lookup entirely.
_EMBEDDING_PRICING_LATERAL = """
LEFT JOIN LATERAL (
    SELECT (price_per_unit / unit_denominator)::float8 AS rate
    FROM llm_pricing
    WHERE provider = sr.embedding_provider
      AND model = sr.embedding_model
      AND unit_type = 'embedding_tokens'
      AND effective_from <= sr.started_at
      AND context_min_tokens = 0
    ORDER BY effective_from DESC
    LIMIT 1
) p_emb ON sr.embedding_provider IS NOT NULL
       AND sr.embedding_model IS NOT NULL
"""


def _build_filter_clauses(
    *,
    workspace_id: UUID | None,
    user_id: str | None,
    source: str | None,
    paid_by: str | None,
) -> tuple[list[str], dict[str, Any]]:
    """Return (where-fragment list, bound-param dict) for caller filters.

    Fragments are concatenated with ``AND`` into the WHERE clause; only
    bound parameter names appear in the strings, never user input.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if workspace_id is not None:
        clauses.append("sr.workspace_id = :workspace_id")
        params["workspace_id"] = workspace_id
    if user_id is not None:
        clauses.append("sr.user_id = :user_id")
        params["user_id"] = user_id
    if source is not None:
        clauses.append("sr.source = :source")
        params["source"] = source
    if paid_by is not None:
        clauses.append("sr.paid_by = :paid_by")
        params["paid_by"] = paid_by
    return clauses, params


class CostAggregationService:
    """Aggregate cost-grade telemetry across the cost-grade schema.

    Single ``aggregate()`` entrypoint shared by both the admin and
    workspace-scoped routes — the only difference is the caller passes
    a fixed ``workspace_id`` for the workspace-scoped route.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def aggregate(
        self,
        *,
        period: str,
        start: datetime,
        end: datetime,
        workspace_id: UUID | None = None,
        user_id: str | None = None,
        source: str | None = None,
        paid_by: str | None = None,
    ) -> list[CostAggregationRow]:
        """Aggregate cost rows across the requested filters.

        Args:
            period: One of ``'day'`` / ``'week'`` / ``'month'``. Maps to
                PostgreSQL ``date_trunc`` precision.
            start: Inclusive lower bound on ``sleep_reports.started_at``
                (naive UTC, matching the column).
            end: Exclusive upper bound on ``sleep_reports.started_at``.
            workspace_id: Restrict to a single workspace (the
                workspace-scoped route always sets this; admin route
                leaves None to see all).
            user_id: Restrict to a single user.
            source: Restrict to ``'sleep'`` or ``'analysis'``.
            paid_by: Restrict to ``'platform'`` or ``'byok'``.

        Returns:
            List of ``CostAggregationRow``, one per
            ``(period_start, workspace_id, user_id)`` triple. Sorted by
            the same triple.

        Raises:
            ValueError: ``period``, ``source``, or ``paid_by`` outside
                the allowed set; ``start >= end``; window wider than
                ``MAX_LOOKBACK_DAYS``.
        """
        # ---- Input validation (defense in depth) --------------------------
        # period feeds date_trunc directly; passing junk would 500 the
        # request. The string is bound (so SQLi is impossible) but
        # validating against an allowlist preserves clear ValueError UX.
        if period not in VALID_PERIODS:
            raise ValueError(f"Invalid period {period!r}; must be one of {VALID_PERIODS}")
        if source is not None and source not in SLEEP_REPORT_SOURCES:
            raise ValueError(f"Invalid source {source!r}; must be one of {SLEEP_REPORT_SOURCES}")
        if paid_by is not None and paid_by not in SLEEP_REPORT_PAID_BY_VALUES:
            raise ValueError(
                f"Invalid paid_by {paid_by!r}; must be one of {SLEEP_REPORT_PAID_BY_VALUES}"
            )
        if start >= end:
            raise ValueError(f"start must be < end (got start={start}, end={end})")
        # Cap the window to bound the ``sleep_reports`` scan (#528). The
        # threshold lives in ``window_exceeds_cap`` so the route enforces the
        # same boundary without a second copy of the literal.
        if window_exceeds_cap(start, end):
            raise ValueError(
                f"date range exceeds {MAX_LOOKBACK_DAYS}-day cap (got {(end - start).days} days)"
            )

        # ---- Build filter fragments ---------------------------------------
        filter_clauses, filter_params = _build_filter_clauses(
            workspace_id=workspace_id,
            user_id=user_id,
            source=source,
            paid_by=paid_by,
        )
        base_where = " AND ".join(
            ["sr.started_at >= :start", "sr.started_at < :end", *filter_clauses]
        )

        params: dict[str, Any] = {
            "period": period,
            "start": start,
            "end": end,
            **filter_params,
        }

        # ---- Single union query: per-model LLM rows + per-source emb rows -
        # The two CTEs keep their natural grouping shapes — LLM cost rolls
        # up by model (because the by_model breakdown needs it), embedding
        # cost rolls up by source only (no per-model split needed for
        # embedding in v1; one provider/model per process). UNION ALL
        # tags rows with ``kind`` so the Python merge step knows which
        # array to populate.
        sql = f"""
        -- Two CTEs, both two-stage (per-row → per-group):
        --
        -- Stage 1 computes per-(report × usage) row cost as **NULLABLE** —
        -- if any required price (e.g. input_tokens > 0 but no
        -- llm_pricing row for that (provider, model, unit_type) at the
        -- run's started_at) is missing, row_cost is NULL. Otherwise
        -- it's the float8 sum across the priced unit_types.
        --
        -- Stage 2 collapses to (period, workspace, user, model, source,
        -- paid_by) and propagates NULL via BOOL_AND: if EVERY row in the
        -- group is fully priced, cost = SUM(row_cost); if ANY row is
        -- unpriced, cost = NULL ("cost unknown" — same semantics as
        -- LLMPricingService.lookup() returning None on a miss).
        --
        -- Without this, COALESCE(... * rate, 0) made unpriced usage
        -- silently look free, breaking the existing service contract
        -- and Issue #472 design intent (NULL-model rows surface as
        -- "—" in the dashboard, not as "$0.00").
        WITH llm_per_row AS (
            SELECT
                date_trunc(:period, sr.started_at)::date AS period_start,
                sr.workspace_id,
                sr.user_id,
                u.model,
                sr.source,
                sr.paid_by,
                u.calls,
                u.input_tokens,
                u.output_tokens,
                u.cached_input_tokens,
                u.cache_write_tokens,
                CASE
                    WHEN (u.input_tokens        > 0 AND p_in.rate          IS NULL)
                      OR (u.output_tokens       > 0 AND p_out.rate         IS NULL)
                      OR (u.cached_input_tokens > 0 AND p_cache.rate       IS NULL)
                      OR (u.cache_write_tokens  > 0 AND p_cache_write.rate IS NULL)
                    THEN NULL::float8
                    ELSE (
                        COALESCE(u.input_tokens        * p_in.rate,          0) +
                        COALESCE(u.output_tokens       * p_out.rate,         0) +
                        COALESCE(u.cached_input_tokens * p_cache.rate,       0) +
                        COALESCE(u.cache_write_tokens  * p_cache_write.rate, 0)
                    )::float8
                END AS row_cost
            FROM sleep_reports sr
            JOIN sleep_report_llm_usage u ON u.report_id = sr.id
            {_LLM_PRICING_LATERAL.format(unit_type="input_tokens", alias="p_in")}
            {_LLM_PRICING_LATERAL.format(unit_type="output_tokens", alias="p_out")}
            {_LLM_PRICING_LATERAL.format(unit_type="cache_read_tokens", alias="p_cache")}
            {_LLM_PRICING_LATERAL.format(unit_type="cache_write_tokens", alias="p_cache_write")}
            WHERE {base_where}
        ),
        llm_per_model AS (
            SELECT
                period_start,
                workspace_id,
                user_id,
                model,
                source,
                paid_by,
                SUM(calls)::bigint               AS calls,
                SUM(input_tokens)::bigint        AS tokens_in,
                SUM(output_tokens)::bigint       AS tokens_out,
                SUM(cached_input_tokens)::bigint AS tokens_cached_in,
                SUM(cache_write_tokens)::bigint  AS tokens_cache_write,
                CASE
                    WHEN BOOL_AND(row_cost IS NOT NULL) THEN SUM(row_cost)::float8
                    ELSE NULL::float8
                END AS cost
            FROM llm_per_row
            GROUP BY 1, 2, 3, 4, 5, 6
        ),
        emb_per_row AS (
            SELECT
                date_trunc(:period, sr.started_at)::date AS period_start,
                sr.workspace_id,
                sr.user_id,
                sr.source,
                sr.paid_by,
                sr.embedding_tokens,
                CASE
                    WHEN sr.embedding_tokens > 0 AND p_emb.rate IS NULL THEN NULL::float8
                    ELSE (sr.embedding_tokens * p_emb.rate)::float8
                END AS row_cost
            FROM sleep_reports sr
            {_EMBEDDING_PRICING_LATERAL}
            WHERE {base_where}
              -- Filter to rows with actual embedding usage so a "completed
              -- run with no LLM child rows AND no embedding tokens" does
              -- not surface as an all-zero aggregation row. This also
              -- excludes status='running'/'failed' reports that have not
              -- yet recorded embedding usage.
              AND sr.embedding_tokens > 0
        ),
        emb_per_source AS (
            SELECT
                period_start,
                workspace_id,
                user_id,
                source,
                paid_by,
                SUM(embedding_tokens)::bigint AS embedding_tokens,
                CASE
                    WHEN BOOL_AND(row_cost IS NOT NULL) THEN SUM(row_cost)::float8
                    ELSE NULL::float8
                END AS cost
            FROM emb_per_row
            GROUP BY 1, 2, 3, 4, 5
        )
        SELECT
            'llm'::text AS kind,
            period_start, workspace_id, user_id,
            model, source, paid_by,
            calls, tokens_in, tokens_out, tokens_cached_in, tokens_cache_write,
            0::bigint AS embedding_tokens,
            cost
        FROM llm_per_model
        UNION ALL
        SELECT
            'emb'::text AS kind,
            period_start, workspace_id, user_id,
            NULL::text AS model, source, paid_by,
            0::bigint AS calls,
            0::bigint AS tokens_in,
            0::bigint AS tokens_out,
            0::bigint AS tokens_cached_in,
            0::bigint AS tokens_cache_write,
            embedding_tokens,
            cost
        FROM emb_per_source
        ORDER BY period_start, workspace_id, user_id, kind, model, source, paid_by
        """

        result = await self.db.execute(text(sql), params)
        rows = result.mappings().all()
        assembled = _assemble_rows(rows)

        logger.info(
            "cost_aggregation_executed",
            period=period,
            start=start.isoformat(),
            end=end.isoformat(),
            workspace_id=str(workspace_id) if workspace_id else None,
            user_id=user_id,
            source=source,
            paid_by=paid_by,
            raw_row_count=len(rows),
            aggregated_row_count=len(assembled),
        )
        return assembled


def _assemble_rows(raw_rows: Sequence[RowMapping]) -> list[CostAggregationRow]:
    """Group raw SQL rows into CostAggregationRow objects.

    Input rows are pre-grouped by SQL — assembly here is bounded by the
    per-(period, workspace, user) cardinality, not by raw event volume.
    """
    # Outer key: (period_start, workspace_id, user_id) → CostAggregationRow
    # Inner trackers: per-model cost split by paid_by; per-source cost split.
    # ``defaultdict`` keeps the per-(model | source) sub-totals tidy
    # without per-row branching.
    # Bucket value shape: {"calls": int, "platform": float | None,
    # "byok": float | None}. None means "cost unknown" (any contributing
    # SQL row had unresolved pricing). Sticky — once None, stays None.
    aggregates: dict[tuple[date, UUID | None, str], CostAggregationRow] = {}
    by_model_acc: dict[
        tuple[date, UUID | None, str],
        dict[str | None, dict[str, float | int | None]],
    ] = defaultdict(lambda: defaultdict(lambda: {"calls": 0, "platform": 0.0, "byok": 0.0}))
    by_source_acc: dict[
        tuple[date, UUID | None, str],
        dict[str, dict[str, float | int | None]],
    ] = defaultdict(lambda: defaultdict(lambda: {"calls": 0, "platform": 0.0, "byok": 0.0}))

    def _add_or_null(current: float | None, increment: float | None) -> float | None:
        """Sum two cost values with sticky-NULL semantics.

        If either side is None ("cost unknown"), the result is None —
        a partial total over a partly-priced group would be misleading.
        """
        if current is None or increment is None:
            return None
        return current + increment

    for r in raw_rows:
        key = (r["period_start"], r["workspace_id"], r["user_id"])
        agg = aggregates.get(key)
        if agg is None:
            agg = CostAggregationRow(
                period_start=r["period_start"],
                workspace_id=r["workspace_id"],
                user_id=r["user_id"],
            )
            aggregates[key] = agg

        kind = r["kind"]
        # Defensive validation: even with the DB CHECK constraint and the
        # service-level allowlist guard, an unexpected paid_by value
        # reaching this point indicates a bug (legacy NULL row, schema
        # drift, future enum expansion not handled here). Fail loud
        # rather than silently miscategorising billing totals.
        paid_by_value = r["paid_by"]
        if paid_by_value not in SLEEP_REPORT_PAID_BY_VALUES:
            logger.error(
                "cost_aggregation_unexpected_paid_by",
                paid_by=paid_by_value,
                period_start=r["period_start"].isoformat()
                if r["period_start"] is not None
                else None,
                workspace_id=str(r["workspace_id"]) if r["workspace_id"] else None,
                user_id=r["user_id"],
                kind=kind,
            )
            raise ValueError(f"Unexpected paid_by value in raw row: {paid_by_value!r}")
        paid_by_col = paid_by_value
        cost_field = "cost_usd" if paid_by_col == "platform" else "cost_usd_byok"

        # cost from SQL is float | None (None when CTE detected an
        # unpriced contribution via BOOL_AND). Pass through as-is — the
        # _add_or_null helper handles the sticky-NULL aggregation.
        sql_cost: float | None = float(r["cost"]) if r["cost"] is not None else None

        if kind == "llm":
            calls = int(r["calls"] or 0)
            agg.calls += calls
            agg.tokens_in += int(r["tokens_in"] or 0)
            agg.tokens_out += int(r["tokens_out"] or 0)
            agg.tokens_cached_in += int(r["tokens_cached_in"] or 0)
            agg.tokens_cache_write += int(r["tokens_cache_write"] or 0)
            setattr(agg, cost_field, _add_or_null(getattr(agg, cost_field), sql_cost))

            model_bucket = by_model_acc[key][r["model"]]
            model_bucket["calls"] = int(model_bucket["calls"] or 0) + calls
            model_bucket[paid_by_col] = _add_or_null(model_bucket[paid_by_col], sql_cost)

            source_bucket = by_source_acc[key][r["source"]]
            source_bucket["calls"] = int(source_bucket["calls"] or 0) + calls
            source_bucket[paid_by_col] = _add_or_null(source_bucket[paid_by_col], sql_cost)
        else:
            # kind == 'emb' — embedding rows contribute tokens + cost but
            # no LLM call count (embedding is metered separately and the
            # response shape exposes embedding_tokens at the top level
            # only). They DO contribute to source-level cost so the
            # by_source breakdown matches the row-level cost_usd totals.
            agg.embedding_tokens += int(r["embedding_tokens"] or 0)
            setattr(agg, cost_field, _add_or_null(getattr(agg, cost_field), sql_cost))

            source_bucket = by_source_acc[key][r["source"]]
            source_bucket[paid_by_col] = _add_or_null(source_bucket[paid_by_col], sql_cost)

    # Materialize breakdowns onto each row. cost_usd / cost_usd_byok are
    # ``float | None`` per the sticky-NULL rule; the route layer's
    # response model declares the same nullable shape.
    for key, agg in aggregates.items():
        agg.cost_breakdown_by_model = [
            CostBreakdownByModel(
                model=model,
                calls=int(b["calls"] or 0),
                cost_usd=b["platform"],  # float | None
                cost_usd_byok=b["byok"],  # float | None
            )
            for model, b in sorted(
                by_model_acc[key].items(),
                key=lambda kv: (kv[0] is None, kv[0] or ""),
            )
        ]
        agg.cost_breakdown_by_source = [
            CostBreakdownBySource(
                source=source,
                calls=int(b["calls"] or 0),
                cost_usd=b["platform"],
                cost_usd_byok=b["byok"],
            )
            for source, b in sorted(by_source_acc[key].items())
        ]

    # Explicit sort by (period_start, workspace_id, user_id) — the
    # docstring promises this ordering. Dict insertion order would
    # preserve the SQL ORDER BY in CPython 3.7+, but relying on that is
    # brittle: a future query rewrite could drop the ORDER BY (it has
    # no effect on the SQL semantics, only on the assembly side) and
    # silently break the response contract. ``workspace_id is None``
    # is the primary key for None-vs-UUID ordering so admin queries
    # with NULL workspace rows (legacy data) sort deterministically.
    return sorted(
        aggregates.values(),
        key=lambda row: (
            row.period_start,
            row.workspace_id is None,
            str(row.workspace_id) if row.workspace_id is not None else "",
            row.user_id,
        ),
    )
