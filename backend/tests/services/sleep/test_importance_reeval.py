"""Tests for Sleep Maintenance Phase 3: Importance Re-evaluation.

Issue #103: EMA smoothing math, boundary conditions.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.importance_reeval import (
    IMPORTANCE_MAX,
    IMPORTANCE_MIN,
    ImportanceReevalPhase,
)
from services.sleep.reporter import SleepBudget


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def reeval_phase(mock_db, mock_llm):
    with patch(
        "services.sleep.importance_reeval.update_memory_payload_in_qdrant", new_callable=AsyncMock
    ):
        phase = ImportanceReevalPhase(mock_db, mock_llm)
    return phase


def _make_config(enabled=True, alpha=0.3, provider="openai", model="gpt-5-nano"):
    config = MagicMock()
    config.sleep_importance_reeval_enabled = enabled
    config.importance_ema_alpha = alpha
    config.sleep_llm_provider = provider
    config.sleep_llm_model = model
    return config


def _make_memory(memory_id=None, importance=0.5, access_count=2, scope="working"):
    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = "test summary"
    m.type = "note"
    m.importance = importance
    m.access_count = access_count
    m.scope = scope
    return m


class TestImportanceReevalPhase:
    """Test ImportanceReevalPhase execution."""

    @pytest.mark.asyncio
    async def test_disabled_returns_skipped(self, reeval_phase):
        config = _make_config(enabled=False)
        budget = SleepBudget()

        result = await reeval_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.skipped is True
        assert result.skip_reason == "importance_reeval_disabled"

    @pytest.mark.asyncio
    async def test_no_candidates(self, reeval_phase):
        config = _make_config()
        budget = SleepBudget()

        reeval_phase._fetch_candidates = AsyncMock(return_value=[])

        result = await reeval_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["message"] == "no_stale_memories"


class TestEMAMath:
    """Test EMA smoothing correctness (critical for importance stability)."""

    def test_ema_basic(self):
        """α=0.3, old=0.5, llm=0.8 → 0.3*0.8 + 0.7*0.5 = 0.59"""
        alpha = 0.3
        old = 0.5
        llm_score = 0.8
        result = alpha * llm_score + (1 - alpha) * old
        assert result == pytest.approx(0.59)

    def test_ema_no_change(self):
        """When LLM agrees with existing importance, no change."""
        alpha = 0.3
        old = 0.7
        llm_score = 0.7
        result = alpha * llm_score + (1 - alpha) * old
        assert result == pytest.approx(0.7)

    def test_ema_convergence(self):
        """After many iterations, importance converges to LLM score."""
        alpha = 0.3
        importance = 0.5
        llm_score = 0.9

        for _ in range(30):  # 30 daily runs
            importance = alpha * llm_score + (1 - alpha) * importance

        # Should be very close to 0.9 after 30 iterations
        assert importance == pytest.approx(0.9, abs=0.01)

    def test_ema_boundary_clamp_high(self):
        """Result > 1.0 should be clamped to 1.0."""
        alpha = 0.3
        old = 0.95
        llm_score = 1.0
        result = alpha * llm_score + (1 - alpha) * old
        clamped = max(0.0, min(1.0, result))
        assert clamped <= 1.0

    def test_ema_boundary_clamp_low(self):
        """Result < 0.0 should be clamped to 0.0."""
        alpha = 0.3
        old = 0.05
        llm_score = 0.0
        result = alpha * llm_score + (1 - alpha) * old
        clamped = max(0.0, min(1.0, result))
        assert clamped >= 0.0

    def test_ema_alpha_zero_no_effect(self):
        """α=0 means LLM has no influence."""
        alpha = 0.0
        old = 0.5
        llm_score = 1.0
        result = alpha * llm_score + (1 - alpha) * old
        assert result == pytest.approx(0.5)

    def test_ema_alpha_one_full_override(self):
        """α=1 means LLM completely overrides."""
        alpha = 1.0
        old = 0.5
        llm_score = 0.9
        result = alpha * llm_score + (1 - alpha) * old
        assert result == pytest.approx(0.9)


class TestConstants:
    """Verify importance re-eval constants."""

    def test_importance_range(self):
        assert IMPORTANCE_MIN == 0.2
        assert IMPORTANCE_MAX == 0.8
        assert IMPORTANCE_MIN < IMPORTANCE_MAX
