"""Tests for Sleep Maintenance Reporter and shared data structures.

Issue #101: PhaseResult, SleepBudget, SleepReporter.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.sleep.reporter import PhaseResult, SleepBudget, SleepBudgetPlan, SleepReporter


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


class TestSleepBudgetPlan:
    """#3019: per-phase budgets of one run."""

    def test_every_phase_gets_the_full_memory_allowance(self):
        plan = SleepBudgetPlan(max_llm_calls=50, max_memories=200, llm_phases=["dedup_merge"])
        for name in ("edge_discovery", "dedup_merge", "merge_retention", "consolidation"):
            budget = plan.for_phase(name)
            assert (budget.max_memories, budget.memories_used) == (200, 0)
            plan.settle(name, budget)

    def test_equal_shares_with_the_remainder_to_the_earliest(self):
        plan = SleepBudgetPlan(max_llm_calls=10, max_memories=200, llm_phases=["a", "b", "c", "d"])
        shares = []
        for name in ("a", "b", "c", "d"):
            budget = plan.for_phase(name)
            shares.append(budget.max_llm_calls)
            budget.consume(llm_calls=budget.max_llm_calls)
            plan.settle(name, budget)
        assert shares == [3, 3, 2, 2]

    def test_unused_calls_roll_forward_and_the_total_holds(self):
        plan = SleepBudgetPlan(max_llm_calls=9, max_memories=200, llm_phases=["a", "b", "c"])
        a = plan.for_phase("a")
        a.consume(llm_calls=1)
        plan.settle("a", a)
        b = plan.for_phase("b")
        assert b.max_llm_calls == 3 + 2
        b.consume(llm_calls=b.max_llm_calls)
        plan.settle("b", b)
        c = plan.for_phase("c")
        assert c.max_llm_calls == 3
        assert 1 + 5 + 3 == 9

    def test_a_phase_that_calls_no_model_gets_no_calls_and_keeps_the_carry(self):
        plan = SleepBudgetPlan(max_llm_calls=4, max_memories=200, llm_phases=["a", "b"])
        a = plan.for_phase("a")
        plan.settle("a", a)  # unused: 2 roll forward
        retention = plan.for_phase("merge_retention")
        assert (retention.max_llm_calls, plan.calls_model("merge_retention")) == (0, False)
        assert not retention.can_afford(llm_calls=1)
        assert retention.can_afford(memories=200)
        plan.settle("merge_retention", retention)
        assert plan.for_phase("b").max_llm_calls == 4

    def test_a_cap_smaller_than_the_phase_count(self):
        plan = SleepBudgetPlan(max_llm_calls=1, max_memories=10, llm_phases=["a", "b", "c"])
        assert [plan.for_phase(n).max_llm_calls for n in ("a", "b", "c")] == [1, 0, 0]

    def test_no_model_phase_at_all(self):
        plan = SleepBudgetPlan(max_llm_calls=50, max_memories=200, llm_phases=[])
        assert plan.for_phase("dedup_merge").max_llm_calls == 0


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


class TestRunStatusGrading:
    """#1183: complete_report grades runs by judge-LLM health.

    A fully non-functional judge (every call raised) used to report
    status='completed' — indistinguishable from a healthy run without
    parsing per-phase JSON (week1-derisk Day-5: llm_call_failures=5/5
    under ok=true).
    """

    @pytest.fixture
    def reporter(self):
        db = AsyncMock()
        db.add = MagicMock()
        return SleepReporter(db)

    @staticmethod
    def _breakdown(calls: int):
        from services.sleep.reporter import LLMCallBreakdown

        return LLMCallBreakdown(provider="self_hosted", model="qwen3.5:9b", calls=calls)

    @pytest.mark.asyncio
    async def test_total_judge_failure_grades_failed(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        results = [
            PhaseResult(phase_name="edge_discovery", llm_calls_used=3, llm_call_failures=3),
            PhaseResult(phase_name="dedup_merge", llm_calls_used=2, llm_call_failures=2),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "failed"
        assert "llm_judge_total_failure" in report.error_message
        assert report.llm_call_failures == 5

    @pytest.mark.asyncio
    async def test_failed_calls_with_spent_tokens_still_grade_failed(self, reporter):
        """#2038: a failed call's tokens land in the breakdown with calls=0, so
        a run whose every judge call raised still grades 'failed'."""
        from services.sleep.reporter import LLMCallBreakdown

        report = MagicMock()
        report.id = uuid4()
        spent = LLMCallBreakdown(
            provider="self_hosted", model="qwen3.5:9b", input_tokens=80, output_tokens=20
        )
        results = [
            PhaseResult(
                phase_name="dedup_merge",
                tokens_used=100,
                llm_call_failures=2,
                llm_breakdown=[spent],
            ),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "failed"
        assert report.llm_tokens_used == 100

    @pytest.mark.asyncio
    async def test_partial_judge_failure_grades_degraded(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        results = [
            PhaseResult(
                phase_name="edge_discovery",
                llm_calls_used=3,
                llm_call_failures=1,
                llm_breakdown=[self._breakdown(calls=2)],
            ),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "degraded"
        assert report.llm_call_failures == 1

    @pytest.mark.asyncio
    async def test_no_failures_grades_completed(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        results = [
            PhaseResult(
                phase_name="edge_discovery",
                llm_calls_used=2,
                llm_breakdown=[self._breakdown(calls=2)],
            ),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "completed"
        assert report.llm_call_failures == 0

    @pytest.mark.asyncio
    async def test_no_llm_work_at_all_grades_completed(self, reporter):
        # Zero attempts is NOT a failure — e.g. no candidates, or LLM off.
        report = MagicMock()
        report.id = uuid4()
        results = [PhaseResult(phase_name="reindex", memories_processed=4)]

        await reporter.complete_report(report, results)

        assert report.status == "completed"
        assert report.llm_call_failures == 0

    @pytest.mark.asyncio
    async def test_phase_data_carries_llm_call_failures(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        results = [
            PhaseResult(
                phase_name="dedup_merge",
                llm_calls_used=1,
                llm_call_failures=1,
                details={"merged": 0},
            ),
        ]

        await reporter.complete_report(report, results)

        assert report.dedup_result["llm_call_failures"] == 1


class TestPhaseFailureGrading:
    """#1229: a phase that raised (success=False) must degrade the run.

    The dedup phase died with MissingGreenlet on its first merge while the
    run still graded 'completed' (0 judge failures, because the crash
    happened before any judge call) — a failed stage hiding behind a green
    run, the exact plausible-success shape #1177/#1183 exist to prevent.
    """

    @pytest.fixture
    def reporter(self):
        db = AsyncMock()
        db.add = MagicMock()
        return SleepReporter(db)

    @pytest.mark.asyncio
    async def test_phase_hard_failure_grades_degraded(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        report.error_message = None
        results = [
            PhaseResult(phase_name="edge_discovery"),
            PhaseResult(
                phase_name="dedup_merge",
                success=False,
                error="greenlet_spawn has not been called; ...",
            ),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "degraded"
        assert "dedup_merge" in report.error_message

    @pytest.mark.asyncio
    async def test_phase_failure_never_upgrades_a_failed_run(self, reporter):
        """Total judge death stays 'failed' even with phase failures."""
        report = MagicMock()
        report.id = uuid4()
        results = [
            PhaseResult(phase_name="edge_discovery", llm_calls_used=3, llm_call_failures=3),
            PhaseResult(phase_name="dedup_merge", success=False, error="boom"),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "failed"

    @pytest.mark.asyncio
    async def test_all_phases_ok_still_completed(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        results = [
            PhaseResult(phase_name="edge_discovery"),
            PhaseResult(phase_name="dedup_merge"),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "completed"


class TestPartialPhaseFailureGrading:
    """#3036: a phase that succeeded with some failed items degrades the run."""

    @pytest.fixture
    def reporter(self):
        db = AsyncMock()
        db.add = MagicMock()
        return SleepReporter(db)

    @pytest.mark.asyncio
    async def test_a_partly_failed_reindex_grades_degraded_with_the_count(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        report.error_message = None
        results = [
            PhaseResult(phase_name="importance_reeval"),
            PhaseResult(phase_name="reindex", operation_failures=2, memories_processed=5),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "degraded"
        assert report.error_message == ("reindex_partial_failure: 2 of 5 reindex operations failed")

    @pytest.mark.asyncio
    async def test_a_phase_failure_and_a_partial_one_are_both_named(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        report.error_message = None
        results = [
            PhaseResult(phase_name="dedup_merge", success=False, error="boom"),
            PhaseResult(phase_name="reindex", operation_failures=1, memories_processed=3),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "degraded"
        assert report.error_message == (
            "phase_failure: dedup_merge; reindex_partial_failure: 1 of 3 reindex operations failed"
        )

    @pytest.mark.asyncio
    async def test_an_entirely_failed_reindex_keeps_the_phase_failure(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        report.error_message = None
        results = [
            PhaseResult(
                phase_name="reindex", success=False, error="All 3 reindex operations failed"
            )
        ]

        await reporter.complete_report(report, results)

        assert report.status == "degraded"
        assert report.error_message == "phase_failure: reindex"

    @pytest.mark.asyncio
    async def test_a_partial_failure_never_upgrades_a_failed_run(self, reporter):
        report = MagicMock()
        report.id = uuid4()
        results = [
            PhaseResult(phase_name="edge_discovery", llm_calls_used=2, llm_call_failures=2),
            PhaseResult(phase_name="reindex", operation_failures=1, memories_processed=2),
        ]

        await reporter.complete_report(report, results)

        assert report.status == "failed"


class TestSupersedeApplyBlob3020:
    """#3020: the supersede-apply phase has no column of its own; its blob is
    stored inside ``dedup_result`` whatever the order of the results."""

    @pytest.fixture
    def reporter(self):
        db = AsyncMock()
        db.add = MagicMock()
        return SleepReporter(db)

    @staticmethod
    def _report():
        from types import SimpleNamespace

        return SimpleNamespace(
            id=uuid4(),
            error_message=None,
            embedding_provider=None,
            source="analysis",
            context_id=None,
            edge_discovery_result=None,
            dedup_result=None,
            merge_retention_result=None,
            importance_result=None,
            consolidation_result=None,
            reindex_result=None,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("apply_first", [False, True])
    async def test_the_blob_nests_in_dedup_result(self, reporter, apply_first):
        report = self._report()
        dedup = PhaseResult(phase_name="dedup_merge", details={"merged": 2})
        apply = PhaseResult(
            phase_name="supersede_apply",
            memories_processed=3,
            details={"applied": 1, "budget_cut": False},
        )
        results = [apply, dedup] if apply_first else [dedup, apply]

        await reporter.complete_report(report, results)

        assert report.dedup_result["details"] == {"merged": 2}
        assert report.dedup_result["supersede_apply"]["details"] == {
            "applied": 1,
            "budget_cut": False,
        }
        assert report.dedup_result["supersede_apply"]["memories_processed"] == 3
        assert report.memories_merged == 2

    @pytest.mark.asyncio
    async def test_without_the_phase_dedup_result_is_unchanged(self, reporter):
        report = self._report()
        await reporter.complete_report(
            report, [PhaseResult(phase_name="dedup_merge", details={"merged": 0})]
        )
        assert "supersede_apply" not in report.dedup_result

    @pytest.mark.asyncio
    async def test_a_run_without_dedup_still_stores_the_blob(self, reporter):
        report = self._report()
        await reporter.complete_report(report, [PhaseResult(phase_name="supersede_apply")])
        assert set(report.dedup_result) == {"supersede_apply"}

    @pytest.mark.asyncio
    async def test_a_cut_phase_is_budget_limited(self, reporter):
        from models.sleep import budget_limited_phases

        report = self._report()
        await reporter.complete_report(
            report,
            [
                PhaseResult(phase_name="consolidation", details={"budget_cut": False}),
                PhaseResult(phase_name="supersede_apply", details={"budget_cut": True}),
            ],
        )
        assert budget_limited_phases(report) == {"supersede_apply": "cut"}

    @pytest.mark.asyncio
    async def test_failed_writes_grade_the_run_degraded_with_the_count(self, reporter):
        report = self._report()
        await reporter.complete_report(
            report,
            [PhaseResult(phase_name="supersede_apply", operation_failures=1, memories_processed=3)],
        )
        assert report.status == "degraded"
        assert report.error_message == (
            "supersede_apply_partial_failure: 1 of 3 supersede_apply operations failed"
        )
