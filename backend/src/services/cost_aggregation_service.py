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
A miss contributes 0 cost (same "cost unknown" semantics as the
existing ``LLMPricingService``); the usage figures are still surfaced.

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
from datetime import date, datetime
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
        cost_usd: float,
        cost_usd_byok: float,
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
        cost_usd: float,
        cost_usd_byok: float,
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
        self.embedding_tokens = 0
        self.cost_usd = 0.0
        self.cost_usd_byok = 0.0
        self.cost_breakdown_by_model: list[CostBreakdownByModel] = []
        self.cost_breakdown_by_source: list[CostBreakdownBySource] = []


# ----------------------------------------------------------------------
# SQL fragments
# ----------------------------------------------------------------------

# LATERAL pricing lookup template. The ``context_min_tokens=0`` clause
# pins to the lowest tier (see module docstring for the v1 limitation).
# Bound parameter ``:unit_type_X`` distinguishes the three LLM units.
_LLM_PRICING_LATERAL = """
LEFT JOIN LATERAL (
    SELECT (price_per_unit / unit_denominator)::float8 AS rate
    FROM llm_pricing
    WHERE provider = u.provider
      AND model = u.model
      AND unit_type = '{unit_type}'
      AND effective_from <= sr.started_at
      AND context_min_tokens = 0
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
                the allowed set; ``start >= end``.
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
        WITH llm_per_model AS (
            SELECT
                date_trunc(:period, sr.started_at)::date AS period_start,
                sr.workspace_id,
                sr.user_id,
                u.model,
                sr.source,
                sr.paid_by,
                SUM(u.calls)::bigint               AS calls,
                SUM(u.input_tokens)::bigint        AS tokens_in,
                SUM(u.output_tokens)::bigint       AS tokens_out,
                SUM(u.cached_input_tokens)::bigint AS tokens_cached_in,
                SUM(
                    COALESCE(u.input_tokens        * p_in.rate,    0) +
                    COALESCE(u.output_tokens       * p_out.rate,   0) +
                    COALESCE(u.cached_input_tokens * p_cache.rate, 0)
                )::float8 AS cost
            FROM sleep_reports sr
            JOIN sleep_report_llm_usage u ON u.report_id = sr.id
            {_LLM_PRICING_LATERAL.format(unit_type="input_tokens", alias="p_in")}
            {_LLM_PRICING_LATERAL.format(unit_type="output_tokens", alias="p_out")}
            {_LLM_PRICING_LATERAL.format(unit_type="cache_read_tokens", alias="p_cache")}
            WHERE {base_where}
            GROUP BY 1, 2, 3, 4, 5, 6
        ),
        emb_per_source AS (
            SELECT
                date_trunc(:period, sr.started_at)::date AS period_start,
                sr.workspace_id,
                sr.user_id,
                sr.source,
                sr.paid_by,
                SUM(sr.embedding_tokens)::bigint AS embedding_tokens,
                SUM(COALESCE(sr.embedding_tokens * p_emb.rate, 0))::float8 AS cost
            FROM sleep_reports sr
            {_EMBEDDING_PRICING_LATERAL}
            WHERE {base_where}
            GROUP BY 1, 2, 3, 4, 5
        )
        SELECT
            'llm'::text AS kind,
            period_start, workspace_id, user_id,
            model, source, paid_by,
            calls, tokens_in, tokens_out, tokens_cached_in,
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
    aggregates: dict[tuple[date, UUID | None, str], CostAggregationRow] = {}
    by_model_acc: dict[
        tuple[date, UUID | None, str],
        dict[str | None, dict[str, float | int]],
    ] = defaultdict(lambda: defaultdict(lambda: {"calls": 0, "platform": 0.0, "byok": 0.0}))
    by_source_acc: dict[
        tuple[date, UUID | None, str],
        dict[str, dict[str, float | int]],
    ] = defaultdict(lambda: defaultdict(lambda: {"calls": 0, "platform": 0.0, "byok": 0.0}))

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
        paid_by_col = "platform" if r["paid_by"] == "platform" else "byok"
        cost_field = "cost_usd" if paid_by_col == "platform" else "cost_usd_byok"

        if kind == "llm":
            calls = int(r["calls"] or 0)
            cost = float(r["cost"] or 0.0)
            agg.calls += calls
            agg.tokens_in += int(r["tokens_in"] or 0)
            agg.tokens_out += int(r["tokens_out"] or 0)
            agg.tokens_cached_in += int(r["tokens_cached_in"] or 0)
            setattr(agg, cost_field, getattr(agg, cost_field) + cost)

            model_bucket = by_model_acc[key][r["model"]]
            model_bucket["calls"] = int(model_bucket["calls"]) + calls
            model_bucket[paid_by_col] = float(model_bucket[paid_by_col]) + cost

            source_bucket = by_source_acc[key][r["source"]]
            source_bucket["calls"] = int(source_bucket["calls"]) + calls
            source_bucket[paid_by_col] = float(source_bucket[paid_by_col]) + cost
        else:
            # kind == 'emb' — embedding rows contribute tokens + cost but
            # no LLM call count (embedding is metered separately and the
            # response shape exposes embedding_tokens at the top level
            # only). They DO contribute to source-level cost so the
            # by_source breakdown matches the row-level cost_usd totals.
            cost = float(r["cost"] or 0.0)
            agg.embedding_tokens += int(r["embedding_tokens"] or 0)
            setattr(agg, cost_field, getattr(agg, cost_field) + cost)

            source_bucket = by_source_acc[key][r["source"]]
            source_bucket[paid_by_col] = float(source_bucket[paid_by_col]) + cost

    # Materialize breakdowns onto each row.
    for key, agg in aggregates.items():
        agg.cost_breakdown_by_model = [
            CostBreakdownByModel(
                model=model,
                calls=int(b["calls"]),
                cost_usd=float(b["platform"]),
                cost_usd_byok=float(b["byok"]),
            )
            for model, b in sorted(
                by_model_acc[key].items(),
                key=lambda kv: (kv[0] is None, kv[0] or ""),
            )
        ]
        agg.cost_breakdown_by_source = [
            CostBreakdownBySource(
                source=source,
                calls=int(b["calls"]),
                cost_usd=float(b["platform"]),
                cost_usd_byok=float(b["byok"]),
            )
            for source, b in sorted(by_source_acc[key].items())
        ]

    return list(aggregates.values())
