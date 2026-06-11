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
from neural.calibration import resolve_edge_threshold, resolve_knn_threshold
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
    kind: str = "knn_seed",
    p25: float = 0.40,
    p50: float = 0.50,
    p75: float = 0.55,
) -> EmbeddingCalibration:
    """Build a calibration row with the given p90 and expiry state.

    Other percentiles default to placeholder values that keep the row
    valid (ascending p25→p99). ``kind`` distinguishes the knn-seed
    (top-k neighbor) distribution from the edge-gate (random-pair)
    distribution (#982).
    """
    now = datetime.now(UTC)
    valid_until = now - timedelta(minutes=1) if expired else now + timedelta(days=30)
    return EmbeddingCalibration(
        model_name="text-embedding-3-small",
        dimensions=512,
        context_id=None,
        kind=kind,
        p25=p25,
        p50=p50,
        p75=p75,
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
    async def test_expired_row_still_serves_value(self, monkeypatch):
        """Lazy-TTL: expired row is served (fail-open) while refresh enqueues.

        Monkeypatches ``_enqueue_lazy_recalibration`` so the test doesn't
        trigger the real Redis + asyncio task path (Copilot review PR #420
        loop 2 finding). We only assert the returned threshold.
        """
        from unittest.mock import AsyncMock as _AsyncMock

        import neural.calibration as calibration_module

        enqueue_stub = _AsyncMock(return_value=None)
        monkeypatch.setattr(calibration_module, "_enqueue_lazy_recalibration", enqueue_stub)

        cfg = _make_config(min_similarity=None)
        db = _mock_db(calibration_result=_make_calibration(p90=0.55, expired=True))
        result = await resolve_knn_threshold(db, cfg, "text-embedding-3-small", 512)
        assert result == pytest.approx(0.55)
        # The lazy-TTL enqueue is fire-and-forget via ``asyncio.create_task``
        # (loop 3 fix so ``remember()`` doesn't block on the Redis round-
        # trip). The coroutine is CREATED (mock called) but may not have
        # been AWAITED by the time the test returns — scheduling a task
        # doesn't yield to the event loop. Assert the call, not the await.
        enqueue_stub.assert_called_once()


class TestResolveKnnThresholdDisabled:
    """D4 Step 3: no calibration row → ``None`` (disable seeding)."""

    @pytest.mark.asyncio
    async def test_missing_calibration_returns_none(self):
        cfg = _make_config(min_similarity=None)
        db = _mock_db(calibration_result=None)
        result = await resolve_knn_threshold(db, cfg, "qwen3-embedding:8b", 4096)
        assert result is None


class TestEmbeddingCalibrationKind:
    """``kind`` column (#982) distinguishes knn_seed vs edge_gate rows."""

    def test_kind_attribute_round_trips(self):
        row = _make_calibration(kind="edge_gate")
        assert row.kind == "edge_gate"


def _make_edge_config(
    absolute: float = 0.5,
    percentile: float = 95.0,
    floor: float = 0.3,
) -> NeuralMemoryConfig:
    """Config with the three edge-gate calibration fields set (#982)."""
    cfg = NeuralMemoryConfig.from_env()
    cfg.min_similarity_for_edge = absolute
    cfg.min_similarity_for_edge_percentile = percentile
    cfg.min_similarity_for_edge_floor = floor
    return cfg


class TestResolveEdgeThresholdCalibrationPath:
    """#982 Step 1: edge_gate calibration row → ``max(percentile(p), floor)``."""

    @pytest.mark.asyncio
    async def test_percentile_above_floor_returned(self):
        cfg = _make_edge_config(percentile=95.0, floor=0.3)
        # p90=0.40 → p95=0.45 (per _make_calibration); percentile(95)=0.45 > floor.
        db = _mock_db(_make_calibration(p90=0.40, kind="edge_gate"))
        result = await resolve_edge_threshold(db, cfg, "text-embedding-3-small", 512)
        assert result == pytest.approx(0.45)

    @pytest.mark.asyncio
    async def test_percentile_below_floor_returns_floor(self):
        cfg = _make_edge_config(percentile=95.0, floor=0.4)
        # p90=0.10 → p95=0.15 < floor 0.4 → floor wins.
        db = _mock_db(_make_calibration(p90=0.10, p25=0.05, p50=0.08, p75=0.09, kind="edge_gate"))
        result = await resolve_edge_threshold(db, cfg, "text-embedding-3-small", 512)
        assert result == 0.4


class TestResolveEdgeThresholdFallback:
    """#982 Step 2: no calibration row → absolute fallback (gate stays active)."""

    @pytest.mark.asyncio
    async def test_no_calibration_returns_absolute(self):
        cfg = _make_edge_config(absolute=0.5)
        db = _mock_db(calibration_result=None)
        result = await resolve_edge_threshold(db, cfg, "text-embedding-3-small", 512)
        # Unlike knn-seed (which returns None to disable), the edge gate MUST
        # stay active — the absolute value is the anti-noise fallback (#118).
        assert result == 0.5

    @pytest.mark.asyncio
    async def test_expired_row_still_served(self):
        """Fail-open on stale calibration: serve the stored value anyway."""
        cfg = _make_edge_config(percentile=95.0, floor=0.3)
        db = _mock_db(_make_calibration(p90=0.40, expired=True, kind="edge_gate"))
        result = await resolve_edge_threshold(db, cfg, "text-embedding-3-small", 512)
        assert result == pytest.approx(0.45)


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
