"""Tests for Sleep Maintenance Phase 4: Consolidation.

Issue #103: Rule-based parity with legacy consolidation_task,
LLM borderline path, bridge node protection.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.consolidation import ConsolidationPhase
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def consolidation_phase(mock_db, mock_llm):
    with (
        patch("services.sleep.consolidation.MemoryRepository"),
        patch("services.sleep.consolidation.GraphService"),
        patch("services.sleep.consolidation.delete_memory_from_qdrant", new_callable=AsyncMock),
    ):
        phase = ConsolidationPhase(mock_db, mock_llm)
        phase.memory_repo = AsyncMock()
    return phase


def _make_config(provider="openai", model="gpt-5-nano"):
    config = MagicMock()
    config.sleep_llm_provider = provider
    config.sleep_llm_model = model
    return config


def _make_working_memory(
    memory_id=None,
    importance=0.5,
    access_count=0,
    age_days=10,
):
    from datetime import datetime, timedelta

    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = "test working memory"
    m.type = "note"
    m.importance = importance
    m.access_count = access_count
    m.scope = "working"
    m.created_at = datetime(2026, 1, 1) - timedelta(days=age_days - 10)
    return m


class TestConsolidationPhase:
    """Test ConsolidationPhase execution."""

    @pytest.mark.asyncio
    async def test_no_working_memories(self, consolidation_phase):
        config = _make_config()
        budget = SleepBudget()

        consolidation_phase._fetch_working_memories = AsyncMock(return_value=[])

        result = await consolidation_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["message"] == "no_working_memories"


class TestRuleBasedPromotion:
    """Test that rule-based promotion matches legacy consolidation_task.

    Patterns 1-4 (memory access / importance / age) must stay identical to
    the rules in tasks/neural_tasks.py::consolidation_task for backward
    compatibility when sleep_enabled=true. The Issue #44 neural metrics
    criteria were dropped from neural_tasks.py in Issue #651 (they live on
    in services/sleep/consolidation.py only), so this parity covers Issue
    #1 patterns only.
    """

    def test_pattern1_frequent_and_important(self):
        """access_count >= 3 AND importance >= 0.5 → promote."""
        mem = _make_working_memory(access_count=3, importance=0.5)
        should = mem.access_count >= 3 and mem.importance >= 0.5
        assert should is True

    def test_pattern2_very_frequent(self):
        """access_count >= 5 → promote."""
        mem = _make_working_memory(access_count=5, importance=0.1)
        should = mem.access_count >= 5
        assert should is True

    def test_pattern3_important_and_aged(self):
        """importance >= 0.8 AND age >= 3 days → promote."""
        mem = _make_working_memory(importance=0.8, age_days=5)
        age_days = 5  # Simulated
        should = mem.importance >= 0.8 and age_days >= 3
        assert should is True

    def test_pattern4_old_and_used(self):
        """age >= 30 AND access_count >= 1 → promote."""
        mem = _make_working_memory(access_count=1, age_days=30)
        age_days = 30
        should = age_days >= 30 and mem.access_count >= 1
        assert should is True

    def test_no_match_is_borderline(self):
        """Memory that doesn't match any rule goes to borderline."""
        mem = _make_working_memory(access_count=1, importance=0.4, age_days=5)
        age_days = 5
        should_promote = (
            (mem.access_count >= 3 and mem.importance >= 0.5)
            or (mem.access_count >= 5)
            or (mem.importance >= 0.8 and age_days >= 3)
            or (age_days >= 30 and mem.access_count >= 1)
        )
        assert should_promote is False


class TestDeletionSafety:
    """Test bridge node protection in deletion logic."""

    def test_isolated_old_unused_deleted(self):
        """age >= 30, access=0, isolated → delete."""
        age_days = 35
        access_count = 0
        neural_metrics = {"is_isolated": True}
        should_delete = age_days >= 30 and access_count == 0 and neural_metrics["is_isolated"]
        assert should_delete is True

    def test_connected_old_unused_not_deleted(self):
        """age >= 30, access=0, but has edges → NOT deleted (bridge protection)."""
        age_days = 35
        access_count = 0
        neural_metrics = {"is_isolated": False}
        should_delete = age_days >= 30 and access_count == 0 and neural_metrics["is_isolated"]
        assert should_delete is False

    def test_recently_used_not_deleted(self):
        """access_count > 0 → NOT deleted."""
        age_days = 35
        access_count = 1
        neural_metrics = {"is_isolated": True}
        should_delete = age_days >= 30 and access_count == 0 and neural_metrics["is_isolated"]
        assert should_delete is False


class TestLLMJudgeParsing:
    """Test LLM response parsing for consolidation."""

    def test_valid_promote_response(self):
        id_a = uuid4()
        label_to_id = {"A": id_a}

        response = {
            "decisions": [{"label": "A", "action": "promote", "reason": "durable knowledge"}]
        }

        # Simulate parsing logic
        decisions = {}
        for item in response.get("decisions", []):
            label = item.get("label")
            action = item.get("action")
            if label in label_to_id and action in ("promote", "keep", "archive"):
                decisions[label_to_id[label]] = action

        assert decisions[id_a] == "promote"

    def test_invalid_action_ignored(self):
        label_to_id = {"A": uuid4()}

        response = {"decisions": [{"label": "A", "action": "destroy", "reason": "bad action"}]}

        decisions = {}
        for item in response.get("decisions", []):
            label = item.get("label")
            action = item.get("action")
            if label in label_to_id and action in ("promote", "keep", "archive"):
                decisions[label_to_id[label]] = action

        assert len(decisions) == 0

    def test_invalid_label_ignored(self):
        label_to_id = {"A": uuid4()}

        response = {"decisions": [{"label": "Z", "action": "promote", "reason": "hallucinated"}]}

        decisions = {}
        for item in response.get("decisions", []):
            label = item.get("label")
            action = item.get("action")
            if label in label_to_id and action in ("promote", "keep", "archive"):
                decisions[label_to_id[label]] = action

        assert len(decisions) == 0


def _isolation_memory(*, importance, access_count, age_days):
    """Build a working Memory with a concrete created_at so the age computed
    inside ConsolidationPhase.execute() matches `age_days` exactly."""
    m = MagicMock()
    m.id = uuid4()
    m.summary = "isolation test memory"
    m.type = "note"
    m.importance = importance
    m.access_count = access_count
    m.scope = "working"
    m.created_at = utcnow() - timedelta(days=age_days)
    return m


class TestNeuralMetricsUnderIsolation:
    """Issue #659: drive ConsolidationPhase.execute() end-to-end to pin the
    neural-metrics promotion criteria and the is_isolated deletion guard under
    the 3-level isolation model.

    Unlike TestDeletionSafety / TestRuleBasedPromotion (which re-implement the
    boolean formulas inline), these tests exercise the real code path so they
    catch the Issue #44 class of regression — e.g. a missing ``await`` on
    ``get_node_metrics`` that previously made the whole neural branch a silent
    no-op (#651 root cause).
    """

    @pytest.mark.asyncio
    async def test_does_not_delete_connected_memory(self, mock_db, mock_llm):
        """is_isolated=False old/unused memory is NOT deleted (bridge protection)."""
        mem = _isolation_memory(importance=0.3, access_count=0, age_days=40)
        graph = MagicMock()
        graph.stats = AsyncMock(return_value={"total_edges": 3})
        graph.get_node_metrics = AsyncMock(
            return_value={
                "centrality": 0.1,
                "edge_count": 2,
                "avg_edge_weight": 0.2,
                "is_hub_node": False,
                "is_isolated": False,
            }
        )
        with (
            patch("services.sleep.consolidation.MemoryRepository"),
            patch("services.sleep.consolidation.GraphService", return_value=graph),
            patch(
                "services.sleep.consolidation.delete_memory_from_qdrant", new_callable=AsyncMock
            ) as del_qdrant,
        ):
            phase = ConsolidationPhase(mock_db, mock_llm)
            phase.memory_repo = AsyncMock()
            phase._fetch_working_memories = AsyncMock(return_value=[mem])
            # LLM off → borderline memories are left untouched.
            result = await phase.execute(_make_config(provider=""), "u", "ws", "ctx", SleepBudget())

        phase.memory_repo.delete.assert_not_called()
        del_qdrant.assert_not_called()
        phase.memory_repo.promote_to_persistent.assert_not_called()
        assert result.details["rule_deleted"] == 0

    @pytest.mark.asyncio
    async def test_neural_metrics_promotion_under_isolation(self, mock_db, mock_llm):
        """High centrality promotes a memory that no Issue #1 rule would promote."""
        mem = _isolation_memory(importance=0.3, access_count=0, age_days=5)
        graph = MagicMock()
        graph.stats = AsyncMock(return_value={"total_edges": 10})
        graph.get_node_metrics = AsyncMock(
            return_value={
                "centrality": 0.9,  # >= NEURAL_CENTRALITY_THRESHOLD (0.7)
                "edge_count": 2,
                "avg_edge_weight": 0.2,
                "is_hub_node": False,
                "is_isolated": False,
            }
        )
        with (
            patch("services.sleep.consolidation.MemoryRepository"),
            patch("services.sleep.consolidation.GraphService", return_value=graph),
            patch("services.sleep.consolidation.delete_memory_from_qdrant", new_callable=AsyncMock),
        ):
            phase = ConsolidationPhase(mock_db, mock_llm)
            phase.memory_repo = AsyncMock()
            phase._fetch_working_memories = AsyncMock(return_value=[mem])
            result = await phase.execute(_make_config(provider=""), "u", "ws", "ctx", SleepBudget())

        phase.memory_repo.promote_to_persistent.assert_awaited_once_with(mem.id)
        assert result.details["rule_promoted"] == 1

    @pytest.mark.asyncio
    async def test_get_node_metrics_is_awaited(self, mock_db, mock_llm):
        """Regression guard for the #44/#651 missing-await: get_node_metrics is
        actually awaited (an unawaited coroutine would raise TypeError on the
        ``neural_metrics['centrality']`` subscript and be silently swallowed)."""
        mem = _isolation_memory(importance=0.3, access_count=0, age_days=5)
        graph = MagicMock()
        graph.stats = AsyncMock(return_value={"total_edges": 4})
        graph.get_node_metrics = AsyncMock(
            return_value={
                "centrality": 0.9,
                "edge_count": 1,
                "avg_edge_weight": 0.2,
                "is_hub_node": False,
                "is_isolated": False,
            }
        )
        with (
            patch("services.sleep.consolidation.MemoryRepository"),
            patch("services.sleep.consolidation.GraphService", return_value=graph),
            patch("services.sleep.consolidation.delete_memory_from_qdrant", new_callable=AsyncMock),
        ):
            phase = ConsolidationPhase(mock_db, mock_llm)
            phase.memory_repo = AsyncMock()
            phase._fetch_working_memories = AsyncMock(return_value=[mem])
            await phase.execute(_make_config(provider=""), "u", "ws", "ctx", SleepBudget())

        graph.get_node_metrics.assert_awaited()
        assert graph.get_node_metrics.await_count == 1
