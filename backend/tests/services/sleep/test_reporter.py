"""Tests for Sleep Maintenance Reporter and shared data structures.

Issue #101: PhaseResult, SleepBudget, SleepReporter.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.sleep.reporter import PhaseResult, SleepBudget, SleepReporter


class TestPhaseResult:
    """Test PhaseResult dataclass."""

    def test_default_values(self):
        result = PhaseResult(phase_name="test")
        assert result.success is True
        assert result.skipped is False
        assert result.llm_calls_used == 0
        assert result.tokens_used == 0
        assert result.memories_processed == 0
        assert result.changed_memory_ids == set()
        assert result.error is None

    def test_with_values(self):
        ids = {uuid4(), uuid4()}
        result = PhaseResult(
            phase_name="dedup_merge",
            success=True,
            llm_calls_used=5,
            tokens_used=1200,
            memories_processed=10,
            changed_memory_ids=ids,
            details={"merged": 3},
        )
        assert result.phase_name == "dedup_merge"
        assert result.llm_calls_used == 5
        assert result.changed_memory_ids == ids

    def test_skipped_result(self):
        result = PhaseResult(
            phase_name="edge_discovery",
            skipped=True,
            skip_reason="budget_exhausted",
        )
        assert result.skipped is True
        assert result.skip_reason == "budget_exhausted"


class TestSleepBudget:
    """Test SleepBudget tracking."""

    def test_initial_state(self):
        budget = SleepBudget(max_llm_calls=50, max_memories=200)
        assert budget.can_afford(llm_calls=1)
        assert budget.can_afford(memories=1)
        assert not budget.exhausted

    def test_consume_and_check(self):
        budget = SleepBudget(max_llm_calls=5, max_memories=10)
        budget.consume(llm_calls=3, memories=5)
        assert budget.llm_calls_used == 3
        assert budget.memories_used == 5
        assert budget.can_afford(llm_calls=2)
        assert not budget.can_afford(llm_calls=3)

    def test_exhausted_by_llm_calls(self):
        budget = SleepBudget(max_llm_calls=2, max_memories=100)
        budget.consume(llm_calls=2)
        assert budget.exhausted

    def test_exhausted_by_memories(self):
        budget = SleepBudget(max_llm_calls=100, max_memories=5)
        budget.consume(memories=5)
        assert budget.exhausted

    def test_can_afford_zero(self):
        budget = SleepBudget(max_llm_calls=0, max_memories=0)
        assert budget.exhausted
        assert budget.can_afford(llm_calls=0, memories=0)


class TestSleepReporter:
    """Test SleepReporter lifecycle."""

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        return db

    @pytest.fixture
    def reporter(self, mock_db):
        return SleepReporter(mock_db)

    @pytest.mark.asyncio
    async def test_create_report(self, reporter, mock_db):
        report = await reporter.create_report(
            user_id="user-1",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )
        assert report.user_id == "user-1"
        assert report.status == "running"
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_action(self, reporter, mock_db):
        report_id = uuid4()
        memory_id = uuid4()
        action = await reporter.add_action(
            report_id=report_id,
            phase="dedup_merge",
            action_type="merge",
            memory_id=memory_id,
            details={"winner": str(memory_id)},
        )
        assert action.phase == "dedup_merge"
        assert action.action_type == "merge"
        assert action.memory_id == memory_id
        mock_db.add.assert_called()

    @pytest.mark.asyncio
    async def test_complete_report(self, reporter):
        report = MagicMock()
        report.id = uuid4()

        results = [
            PhaseResult(
                phase_name="edge_discovery",
                llm_calls_used=5,
                tokens_used=500,
                memories_processed=20,
            ),
            PhaseResult(
                phase_name="dedup_merge",
                llm_calls_used=3,
                tokens_used=300,
                memories_processed=10,
                details={"merged": 2},
            ),
            PhaseResult(
                phase_name="reindex",
                embedding_calls_used=8,
                memories_processed=8,
            ),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "completed"
        assert report.completed_at is not None
        assert report.llm_calls_made == 8
        assert report.llm_tokens_used == 800
        assert report.embedding_calls_made == 8
        assert report.memories_processed == 38
        assert report.edge_discovery_result is not None
        assert report.dedup_result is not None
        assert report.reindex_result is not None

    @pytest.mark.asyncio
    async def test_fail_report(self, reporter):
        report = MagicMock()
        report.id = uuid4()

        await reporter.fail_report(report, "Something went wrong")

        assert report.status == "failed"
        assert report.completed_at is not None
        assert report.error_message == "Something went wrong"
