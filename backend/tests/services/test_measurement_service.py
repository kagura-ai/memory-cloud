"""Unit tests for MeasurementService (Issue #1333).

Pins the validation contract (metric length, finite-number values, period/agg
allowlists, window cap) and the SQL shape of ``recall_series`` (date_trunc +
GROUP BY with every dynamic piece bound, never interpolated) without a
database — the session is mocked. DB-backed behaviour (defaults, FK cascade,
index) is covered in tests/integration/test_e71_1333_measurements_migration.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.measurement_service import (
    DEFAULT_WINDOW_DAYS,
    MAX_LOOKBACK_DAYS,
    METRIC_MAX_LEN,
    VALID_AGGS,
    VALID_PERIODS,
    MeasurementService,
)
from utils.datetime import utcnow

CTX = uuid4()


def _db(rows: list | None = None) -> MagicMock:
    """Mock AsyncSession: add/commit for record, execute().all() for series."""
    result = MagicMock()
    result.all.return_value = rows or []
    return MagicMock(
        add=MagicMock(),
        commit=AsyncMock(),
        execute=AsyncMock(return_value=result),
    )


class TestRecord:
    @pytest.mark.asyncio
    async def test_rejects_bad_metric(self):
        db = _db()
        svc = MeasurementService(db)
        for bad in ("", "m" * (METRIC_MAX_LEN + 1), 42, None):
            with pytest.raises(ValueError):
                await svc.record(CTX, bad, 1.0)  # type: ignore[arg-type]
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_non_finite_and_non_number_values(self):
        db = _db()
        svc = MeasurementService(db)
        for bad in (True, False, float("inf"), float("-inf"), float("nan"), "72.5", None):
            with pytest.raises(ValueError):
                await svc.record(CTX, "weight_kg", bad)  # type: ignore[arg-type]
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_bad_unit(self):
        db = _db()
        svc = MeasurementService(db)
        for bad in ("u" * 33, "", 7):
            with pytest.raises(ValueError):
                await svc.record(CTX, "weight_kg", 1.0, unit=bad)  # type: ignore[arg-type]
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_non_dict_details(self):
        db = _db()
        svc = MeasurementService(db)
        with pytest.raises(ValueError):
            await svc.record(CTX, "weight_kg", 1.0, details=["not", "a", "dict"])  # type: ignore[arg-type]
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_defaults_measured_at_to_now(self):
        db = _db()
        row = await MeasurementService(db).record(
            CTX, "weight_kg", 72.5, unit="kg", details={"device": "scale"}
        )
        db.add.assert_called_once_with(row)
        db.commit.assert_awaited_once()
        assert row.context_id == CTX
        assert row.metric == "weight_kg"
        assert row.value == 72.5
        assert row.unit == "kg"
        assert row.details == {"device": "scale"}
        # naive UTC, defaulted to "now"
        assert row.measured_at.tzinfo is None
        assert abs((utcnow() - row.measured_at).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_aware_measured_at_is_normalized_to_naive_utc(self):
        db = _db()
        aware = datetime(2026, 7, 1, 12, 30, tzinfo=UTC)
        row = await MeasurementService(db).record(CTX, "weight_kg", 70, measured_at=aware)
        assert row.measured_at == datetime(2026, 7, 1, 12, 30)
        assert row.measured_at.tzinfo is None

    @pytest.mark.asyncio
    async def test_int_value_is_accepted(self):
        db = _db()
        row = await MeasurementService(db).record(CTX, "steps", 10000)
        assert row.value == 10000


class TestRecallSeriesValidation:
    @pytest.mark.asyncio
    async def test_rejects_unknown_period(self):
        db = _db()
        svc = MeasurementService(db)
        for bad in ("hour", "DAY", "day; DROP TABLE measurements", ""):
            with pytest.raises(ValueError):
                await svc.recall_series(CTX, "weight_kg", period=bad)
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_unknown_agg(self):
        db = _db()
        svc = MeasurementService(db)
        for bad in ("median", "AVG", "avg)--", ""):
            with pytest.raises(ValueError):
                await svc.recall_series(CTX, "weight_kg", agg=bad)
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_bad_metric(self):
        db = _db()
        with pytest.raises(ValueError):
            await MeasurementService(db).recall_series(CTX, "m" * (METRIC_MAX_LEN + 1))
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_inverted_window(self):
        db = _db()
        end = utcnow()
        with pytest.raises(ValueError):
            await MeasurementService(db).recall_series(
                CTX, "weight_kg", start=end, end=end - timedelta(days=1)
            )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_window_wider_than_cap(self):
        db = _db()
        end = utcnow()
        with pytest.raises(ValueError):
            await MeasurementService(db).recall_series(
                CTX, "weight_kg", start=end - timedelta(days=MAX_LOOKBACK_DAYS, seconds=1), end=end
            )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_window_exactly_at_cap(self):
        db = _db()
        end = utcnow()
        await MeasurementService(db).recall_series(
            CTX, "weight_kg", start=end - timedelta(days=MAX_LOOKBACK_DAYS), end=end
        )
        db.execute.assert_awaited_once()

    def test_allowlists_are_pinned(self):
        assert VALID_PERIODS == ("day", "week", "month")
        assert set(VALID_AGGS) >= {"avg", "min", "max", "sum", "count"}


class TestRecallSeriesSQL:
    @pytest.mark.asyncio
    async def test_sql_shape_and_bound_params(self):
        db = _db()
        await MeasurementService(db).recall_series(CTX, "weight_kg", period="week", agg="max")
        stmt, params = db.execute.await_args.args
        sql = str(stmt)
        # date_trunc bucketing with the period BOUND (allowlist-gated), not
        # interpolated; grouped and ordered by bucket.
        assert "date_trunc(:period" in sql
        assert "GROUP BY" in sql
        assert "ORDER BY" in sql
        # every dynamic piece is a bind param — the metric never appears in SQL
        assert "weight_kg" not in sql
        assert ":metric" in sql and ":context_id" in sql
        assert ":start" in sql and ":end" in sql
        assert params["period"] == "week"
        assert params["metric"] == "weight_kg"
        assert params["context_id"] == CTX
        assert "MAX" in sql.upper()

    @pytest.mark.asyncio
    async def test_default_window_is_30_days_ending_now(self):
        db = _db()
        await MeasurementService(db).recall_series(CTX, "weight_kg")
        _, params = db.execute.await_args.args
        assert params["end"] - params["start"] == timedelta(days=DEFAULT_WINDOW_DAYS)
        assert abs((utcnow() - params["end"]).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_rows_map_to_bucket_value_count(self):
        rows = [
            SimpleNamespace(bucket=datetime(2026, 7, 1), value=71.2, count=3),
            SimpleNamespace(bucket=datetime(2026, 7, 2), value=70.8, count=1),
        ]
        db = _db(rows)
        series = await MeasurementService(db).recall_series(CTX, "weight_kg")
        assert series == [
            {"bucket": datetime(2026, 7, 1), "value": 71.2, "count": 3},
            {"bucket": datetime(2026, 7, 2), "value": 70.8, "count": 1},
        ]

    @pytest.mark.asyncio
    async def test_aware_window_bounds_are_normalized_to_naive_utc(self):
        db = _db()
        start = datetime(2026, 6, 1, tzinfo=UTC)
        end = datetime(2026, 7, 1, tzinfo=UTC)
        await MeasurementService(db).recall_series(CTX, "weight_kg", start=start, end=end)
        _, params = db.execute.await_args.args
        assert params["start"] == datetime(2026, 6, 1)
        assert params["start"].tzinfo is None
        assert params["end"] == datetime(2026, 7, 1)
