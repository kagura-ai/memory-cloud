"""HOW-MUCH measurement lane service (Issue #1333).

Append-only writes and bucketed reads over the dedicated ``measurements``
table — a numeric time-series lane structurally excluded from ``recall()``
(it lives in its own table, not ``memories``, and is never embedded) and
untouchable by Sleep consolidation.

``record`` is a plain INSERT (no upsert — a series is history, not state).
``recall_series`` buckets with PostgreSQL ``date_trunc`` and aggregates with
an allowlist-gated aggregate, capped to a bounded lookback window.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.measurement import Measurement
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Matches the measurements.metric column length (VARCHAR(64)).
METRIC_MAX_LEN = 64
# Matches the measurements.unit column length (VARCHAR(32)).
UNIT_MAX_LEN = 32

# Allowed period values map to PostgreSQL ``date_trunc`` precision tokens.
# Bound to a literal allowlist before reaching the SQL — even though the
# value is bound (not interpolated), passing arbitrary strings would 500
# the query when ``date_trunc`` rejects them. (Precedent:
# ``services.cost_aggregation_service.VALID_PERIODS``.)
VALID_PERIODS: tuple[str, ...] = ("day", "week", "month")

# Aggregate name → SQL fragment. The fragment is selected from this module
# constant by allowlisted key — user input never reaches the SQL text, only
# the key lookup. ``::float8`` so asyncpg returns floats, not Decimals.
_AGG_SQL: dict[str, str] = {
    "avg": "AVG(value)::float8",
    "min": "MIN(value)::float8",
    "max": "MAX(value)::float8",
    "sum": "SUM(value)::float8",
    "count": "COUNT(value)::float8",
    "last": "(ARRAY_AGG(value ORDER BY measured_at DESC))[1]::float8",
}
VALID_AGGS: tuple[str, ...] = tuple(_AGG_SQL)

# Maximum span of a single series request, in days. Defense-in-depth window
# cap mirroring ``cost_aggregation_service.MAX_LOOKBACK_DAYS`` (#528): a
# non-UI caller (curl / SDK / MCP) must not be able to scan years of
# ``measurements`` in one query.
MAX_LOOKBACK_DAYS = 365

# Default half-open window when the caller gives no bounds: [now - 30d, now).
DEFAULT_WINDOW_DAYS = 30


def window_exceeds_cap(start: datetime, end: datetime) -> bool:
    """True if the half-open window ``[start, end)`` is wider than the cap.

    ``> timedelta`` (not ``.days >``) rejects a sub-day overage too, which a
    direct service caller could pass (same rationale as
    ``cost_aggregation_service.window_exceeds_cap``).
    """
    return end - start > timedelta(days=MAX_LOOKBACK_DAYS)


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize an aware datetime to the project's naive-UTC convention."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _validate_metric(metric: Any) -> str:
    """Return ``metric`` if it is a non-empty string within the column cap."""
    if not isinstance(metric, str) or not metric:
        raise ValueError("'metric' must be a non-empty string")
    if len(metric) > METRIC_MAX_LEN:
        raise ValueError(f"'metric' must be at most {METRIC_MAX_LEN} characters")
    return metric


class MeasurementService:
    """Record and aggregate numeric measurement series, scoped to a context."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def record(
        self,
        context_id: UUID,
        metric: str,
        value: float,
        *,
        measured_at: datetime | None = None,
        unit: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Measurement:
        """Append one measurement observation (plain INSERT, never upsert).

        Args:
            context_id: Owning context (rows cascade with it).
            metric: Series name, e.g. ``"weight_kg"`` (max 64 chars).
            value: Finite number. Bools, NaN and infinities are rejected.
            measured_at: Observation time; defaults to now (UTC). Aware
                datetimes are normalized to naive UTC.
            unit: Optional display unit (max 32 chars).
            details: Optional free-form JSON metadata (device, source, ...).

        Returns:
            The persisted ``Measurement`` row.

        Raises:
            ValueError: On any invalid argument.
        """
        _validate_metric(metric)
        # bool is an int subclass — reject explicitly before the isinstance
        # check; NaN/inf are numbers but poison every aggregate.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("'value' must be a number")
        if not math.isfinite(value):
            raise ValueError("'value' must be finite (NaN and infinity are rejected)")
        if unit is not None:
            if not isinstance(unit, str) or not unit:
                raise ValueError("'unit' must be a non-empty string when provided")
            if len(unit) > UNIT_MAX_LEN:
                raise ValueError(f"'unit' must be at most {UNIT_MAX_LEN} characters")
        if details is not None and not isinstance(details, dict):
            raise ValueError("'details' must be an object when provided")
        if measured_at is not None and not isinstance(measured_at, datetime):
            raise ValueError("'measured_at' must be a datetime when provided")

        row = Measurement(
            context_id=context_id,
            metric=metric,
            measured_at=_to_naive_utc(measured_at) if measured_at is not None else utcnow(),
            value=value,
            unit=unit,
            details=details,
        )
        self.db.add(row)
        await self.db.commit()
        logger.info(
            "measurement_recorded",
            context_id=str(context_id),
            metric=metric,
        )
        return row

    async def recall_series(
        self,
        context_id: UUID,
        metric: str,
        *,
        period: str = "day",
        agg: str = "avg",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate one metric's series into time buckets.

        Args:
            context_id: Owning context.
            metric: Series name to aggregate.
            period: Bucket size — one of ``VALID_PERIODS``.
            agg: Aggregate — one of ``VALID_AGGS`` (``last`` = most recent
                value in the bucket by ``measured_at``).
            start: Window start (inclusive); defaults to ``end - 30 days``.
            end: Window end (exclusive); defaults to now (UTC).

        Returns:
            ``[{"bucket": datetime, "value": float, "count": int}, ...]``
            ordered by bucket ascending. Empty buckets are omitted.

        Raises:
            ValueError: On bad metric/period/agg or an inverted/oversized
                window (maps to ``validation_error`` at the MCP boundary).
        """
        _validate_metric(metric)
        if period not in VALID_PERIODS:
            raise ValueError(f"Invalid period {period!r}. Valid: {', '.join(VALID_PERIODS)}")
        if agg not in _AGG_SQL:
            raise ValueError(f"Invalid agg {agg!r}. Valid: {', '.join(VALID_AGGS)}")

        end_at = _to_naive_utc(end) if end is not None else utcnow()
        start_at = (
            _to_naive_utc(start)
            if start is not None
            else end_at - timedelta(days=DEFAULT_WINDOW_DAYS)
        )
        if start_at >= end_at:
            raise ValueError("'start' must be before 'end'")
        if window_exceeds_cap(start_at, end_at):
            raise ValueError(f"Window too wide: maximum lookback is {MAX_LOOKBACK_DAYS} days")

        # The aggregate fragment comes from the module-constant _AGG_SQL map
        # keyed by the allowlisted ``agg`` — never from user input. Everything
        # user-controlled (period, metric, window, context) is a bind param.
        sql = (
            "SELECT date_trunc(:period, measured_at) AS bucket, "
            + _AGG_SQL[agg]
            + " AS value, COUNT(*)::bigint AS count "
            "FROM measurements "
            "WHERE context_id = :context_id AND metric = :metric "
            "AND measured_at >= :start AND measured_at < :end "
            "GROUP BY bucket ORDER BY bucket ASC"
        )
        rows = (
            await self.db.execute(
                text(sql),
                {
                    "period": period,
                    "context_id": context_id,
                    "metric": metric,
                    "start": start_at,
                    "end": end_at,
                },
            )
        ).all()
        return [{"bucket": row.bucket, "value": row.value, "count": row.count} for row in rows]
