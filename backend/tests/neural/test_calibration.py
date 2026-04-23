"""Unit tests for the D4 fallback chain and calibration model (#406 Phase B).

Covers ``neural.calibration.resolve_knn_threshold`` and the
``EmbeddingCalibration`` model's ``percentile()`` / ``is_expired()``.
Integration tests for the runtime wiring in ``_create_knn_seed_edges``
live alongside ``tests/services/test_knn_seeding.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.neural import EmbeddingCalibration
from neural.calibration import resolve_knn_threshold
from neural.config import NeuralMemoryConfig


def _make_config(
    min_similarity: float | None = None,
    min_percentile: float = 90.0,
    floor: float = 0.3,
) -> NeuralMemoryConfig:
    """Build a NeuralMemoryConfig with the fields this module reads.

    Uses ``from_env()`` to pick up the full default surface, then
    overrides the three knn-seed-calibration fields that the D4 chain
    cares about. Other fields (hebbian, scoring, etc.) keep their
    defaults.
    """
    cfg = NeuralMemoryConfig.from_env()
    cfg.knn_seed_min_similarity = min_similarity
    cfg.knn_seed_min_percentile = min_percentile
    cfg.knn_seed_min_similarity_floor = floor
    return cfg


def _make_calibration(
    p90: float = 0.6322,
    expired: bool = False,
) -> EmbeddingCalibration:
    """Build a calibration row with the given p90 and expiry state.

    Other percentiles are placeholder values that keep the row valid
    (ascending p25→p99) but are not read by the p90-only resolve path.
    """
    now = datetime.now(UTC)
    valid_until = now - timedelta(minutes=1) if expired else now + timedelta(days=30)
    return EmbeddingCalibration(
        model_name="text-embedding-3-small",
        dimensions=512,
        context_id=None,
        p25=0.40,
        p50=0.50,
        p75=0.55,
        p90=p90,
        p95=min(p90 + 0.05, 1.0),
        p99=min(p90 + 0.12, 1.0),
        sample_size=10000,
        sampled_at=now,
        valid_until=valid_until,
    )


def _mock_db(calibration_result: EmbeddingCalibration | None) -> AsyncMock:
    """AsyncSession mock whose execute() returns the given calibration row.

    ``calibration_result`` is what ``scalar_one_or_none()`` yields on
    the row returned by ``db.execute(...)`` — pass ``None`` to simulate
    "no calibration row exists".
    """
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=calibration_result)
    db.execute = AsyncMock(return_value=result)
    return db


class TestResolveKnnThresholdOperatorOverride:
    """D4 Step 1: operator-set ``knn_seed_min_similarity`` wins over calibration."""

    @pytest.mark.asyncio
    async def test_operator_value_returned_verbatim(self):
        cfg = _make_config(min_similarity=0.35)
        db = _mock_db(calibration_result=None)
        result = await resolve_knn_threshold(db, cfg, "text-embedding-3-small", 512)
        assert result == 0.35
        # Step 1 should short-circuit — no calibration lookup hit the DB.
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_operator_value_ignores_calibration(self):
        """Even with a fresh calibration row, operator override wins (D6)."""
        cfg = _make_config(min_similarity=0.50)
        db = _mock_db(calibration_result=_make_calibration(p90=0.63))
        result = await resolve_knn_threshold(db, cfg, "text-embedding-3-small", 512)
        assert result == 0.50
        db.execute.assert_not_called()


class TestResolveKnnThresholdCalibrationPath:
    """D4 Step 2: calibration row present → ``max(percentile(p), floor)``."""

    @pytest.mark.asyncio
    async def test_p90_above_floor_returns_p90(self):
        cfg = _make_config(min_similarity=None, min_percentile=90.0, floor=0.3)
        db = _mock_db(calibration_result=_make_calibration(p90=0.6322))
        result = await resolve_knn_threshold(db, cfg, "text-embedding-3-small", 512)
        assert result == pytest.approx(0.6322)

    @pytest.mark.asyncio
    async def test_p90_below_floor_returns_floor(self):
        """Floor protects sparse corpora where the distribution collapses."""
        cfg = _make_config(min_similarity=None, min_percentile=90.0, floor=0.4)
        db = _mock_db(calibration_result=_make_calibration(p90=0.25))
        result = await resolve_knn_threshold(db, cfg, "text-embedding-3-small", 512)
        assert result == 0.4

    @pytest.mark.asyncio
    async def test_different_percentile_setting(self):
        """``knn_seed_min_percentile`` picks p50 → interpolates from stored grid."""
        cfg = _make_config(min_similarity=None, min_percentile=50.0, floor=0.2)
        db = _mock_db(calibration_result=_make_calibration(p90=0.6322))
        result = await resolve_knn_threshold(db, cfg, "text-embedding-3-small", 512)
        # _make_calibration fixes p50 = 0.50 > floor 0.2
        assert result == pytest.approx(0.50)

    @pytest.mark.asyncio
    async def test_expired_row_still_serves_value(self):
        """Lazy-TTL: expired row is served (fail-open) while refresh enqueues."""
        cfg = _make_config(min_similarity=None)
        db = _mock_db(calibration_result=_make_calibration(p90=0.55, expired=True))
        result = await resolve_knn_threshold(db, cfg, "text-embedding-3-small", 512)
        # Expired rows are still returned; enqueue happens as a side effect
        # (swallowed by ImportError guard when tasks module is absent).
        assert result == pytest.approx(0.55)


class TestResolveKnnThresholdDisabled:
    """D4 Step 3: no calibration row → ``None`` (disable seeding)."""

    @pytest.mark.asyncio
    async def test_missing_calibration_returns_none(self):
        cfg = _make_config(min_similarity=None)
        db = _mock_db(calibration_result=None)
        result = await resolve_knn_threshold(db, cfg, "qwen3-embedding:8b", 4096)
        assert result is None


class TestEmbeddingCalibrationPercentile:
    """``EmbeddingCalibration.percentile()`` linear-interp helper."""

    def test_direct_stored_percentiles(self):
        row = _make_calibration(p90=0.60)
        # Stored grid values are returned directly (no interpolation).
        assert row.percentile(25.0) == pytest.approx(row.p25)
        assert row.percentile(90.0) == pytest.approx(0.60)
        assert row.percentile(99.0) == pytest.approx(row.p99)

    def test_interpolate_between_p75_and_p90(self):
        row = _make_calibration(p90=0.60)
        # p75=0.55, p90=0.60 → midpoint p=82.5 → 0.575
        mid = row.percentile(82.5)
        assert mid == pytest.approx((row.p75 + row.p90) / 2)

    def test_clamp_below_p25(self):
        row = _make_calibration(p90=0.60)
        assert row.percentile(10.0) == pytest.approx(row.p25)

    def test_clamp_above_p99(self):
        row = _make_calibration(p90=0.60)
        assert row.percentile(99.9) == pytest.approx(row.p99)


class TestEmbeddingCalibrationIsExpired:
    """``EmbeddingCalibration.is_expired()`` drives the lazy-TTL trigger."""

    def test_fresh_row_not_expired(self):
        row = _make_calibration(expired=False)
        assert row.is_expired() is False

    def test_expired_row_flagged(self):
        row = _make_calibration(expired=True)
        assert row.is_expired() is True

    def test_naive_valid_until_coerced_to_utc(self):
        """Some DB drivers return naive datetimes; the helper must still work."""
        row = _make_calibration(expired=False)
        # Strip tzinfo to simulate a naive datetime coming back from the DB.
        row.valid_until = row.valid_until.replace(tzinfo=None)
        assert row.is_expired() is False
