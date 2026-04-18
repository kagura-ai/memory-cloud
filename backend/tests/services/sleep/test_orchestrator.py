"""Tests for Sleep Maintenance Orchestrator.

Issue #101/#103: Phase execution order, budget enforcement, error isolation.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.orchestrator import SleepOrchestrator
from services.sleep.reporter import PhaseResult


@pytest.fixture
def mock_db():
    return AsyncMock()


def _make_config():
    config = MagicMock()
    config.sleep_max_llm_calls_per_run = 50
    config.sleep_max_memories_per_run = 200
    config.sleep_dedup_enabled = True
    config.sleep_edge_discovery_enabled = True
    config.sleep_importance_reeval_enabled = True
    config.sleep_llm_provider = "openai"
    config.sleep_llm_model = "gpt-5-nano"
    config.sleep_dedup_similarity_threshold = 0.92
    config.sleep_edge_discovery_sample_size = 30
    config.importance_ema_alpha = 0.3
    return config


class TestSleepOrchestrator:
    """Test orchestrator phase coordination."""

    @pytest.mark.asyncio
    async def test_runs_all_phases_in_order(self, mock_db):
        """All 6 phases should execute in order."""
        config = _make_config()
        phase_names_executed = []

        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase") as MockED,
            patch("services.sleep.orchestrator.DedupMergePhase") as MockDM,
            patch("services.sleep.orchestrator.ImportanceReevalPhase") as MockIR,
            patch("services.sleep.orchestrator.ConsolidationPhase") as MockCP,
            patch("services.sleep.orchestrator.ReindexPhase") as MockRI,
        ):
            # Set up reporter
            reporter = AsyncMock()
            report = MagicMock()
            report.id = uuid4()
            reporter.create_report = AsyncMock(return_value=report)
            reporter.complete_report = AsyncMock()
            MockReporter.return_value = reporter

            # Set up phases to track execution order
            for i, MockPhase in enumerate([MockED, MockDM, MockIR, MockCP]):
                instance = AsyncMock()

                async def make_result(cfg, uid, ws, ctx, budget, idx=i, **kwargs):
                    names = ["edge_discovery", "dedup_merge", "importance_reeval", "consolidation"]
                    phase_names_executed.append(names[idx])
                    return PhaseResult(phase_name=names[idx])

                instance.execute = make_result
                MockPhase.return_value = instance

            # Reindex
            reindex_instance = AsyncMock()

            async def reindex_exec(changed, uid, ws=None, ctx=None):
                phase_names_executed.append("reindex")
                return PhaseResult(phase_name="reindex")

            reindex_instance.execute = reindex_exec
            MockRI.return_value = reindex_instance

            orchestrator = SleepOrchestrator(mock_db)
            await orchestrator.run("user-1", "ws-1", "ctx-1", config=config)

        # Verify order
        assert phase_names_executed == [
            "edge_discovery",
            "dedup_merge",
            "importance_reeval",
            "consolidation",
            "reindex",
        ]
        reporter.create_report.assert_awaited_once()
        reporter.complete_report.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_phase_failure_doesnt_stop_others(self, mock_db):
        """If one phase fails, subsequent phases still execute."""
        config = _make_config()
        phases_completed = []

        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase") as MockED,
            patch("services.sleep.orchestrator.DedupMergePhase") as MockDM,
            patch("services.sleep.orchestrator.ImportanceReevalPhase") as MockIR,
            patch("services.sleep.orchestrator.ConsolidationPhase") as MockCP,
            patch("services.sleep.orchestrator.ReindexPhase") as MockRI,
        ):
            reporter = AsyncMock()
            report = MagicMock()
            report.id = uuid4()
            reporter.create_report = AsyncMock(return_value=report)
            reporter.complete_report = AsyncMock()
            MockReporter.return_value = reporter

            # Edge discovery raises
            ed_instance = AsyncMock()
            ed_instance.execute = AsyncMock(side_effect=RuntimeError("phase 1 boom"))
            MockED.return_value = ed_instance

            # Dedup works fine
            dm_instance = AsyncMock()

            async def dm_exec(cfg, uid, ws, ctx, budget, **kwargs):
                phases_completed.append("dedup_merge")
                return PhaseResult(phase_name="dedup_merge")

            dm_instance.execute = dm_exec
            MockDM.return_value = dm_instance

            # Others work fine
            for MockPhase, name in [(MockIR, "importance_reeval"), (MockCP, "consolidation")]:
                inst = AsyncMock()

                async def phase_exec(cfg, uid, ws, ctx, budget, n=name, **kwargs):
                    phases_completed.append(n)
                    return PhaseResult(phase_name=n)

                inst.execute = phase_exec
                MockPhase.return_value = inst

            ri_inst = AsyncMock()

            async def ri_exec(changed, uid, ws=None, ctx=None):
                phases_completed.append("reindex")
                return PhaseResult(phase_name="reindex")

            ri_inst.execute = ri_exec
            MockRI.return_value = ri_inst

            orchestrator = SleepOrchestrator(mock_db)
            await orchestrator.run("user-1", "ws-1", "ctx-1", config=config)

        # Phase 1 failed but 2-5 still ran
        assert "dedup_merge" in phases_completed
        assert "importance_reeval" in phases_completed
        assert "consolidation" in phases_completed
        assert "reindex" in phases_completed
        # Report was completed (not failed) since orchestrator caught phase error
        reporter.complete_report.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_changed_memory_ids_flow_to_reindex(self, mock_db):
        """Changed memory IDs from earlier phases should be passed to reindex."""
        config = _make_config()
        mem_id_1 = uuid4()
        mem_id_2 = uuid4()
        reindex_received = []

        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase") as MockED,
            patch("services.sleep.orchestrator.DedupMergePhase") as MockDM,
            patch("services.sleep.orchestrator.ImportanceReevalPhase") as MockIR,
            patch("services.sleep.orchestrator.ConsolidationPhase") as MockCP,
            patch("services.sleep.orchestrator.ReindexPhase") as MockRI,
        ):
            reporter = AsyncMock()
            report = MagicMock()
            report.id = uuid4()
            reporter.create_report = AsyncMock(return_value=report)
            reporter.complete_report = AsyncMock()
            MockReporter.return_value = reporter

            # Phase 1: returns mem_id_1
            ed = AsyncMock()
            ed.execute = AsyncMock(
                return_value=PhaseResult(
                    phase_name="edge_discovery",
                    changed_memory_ids={mem_id_1},
                )
            )
            MockED.return_value = ed

            # Phase 2: returns mem_id_2
            dm = AsyncMock()
            dm.execute = AsyncMock(
                return_value=PhaseResult(
                    phase_name="dedup_merge",
                    changed_memory_ids={mem_id_2},
                )
            )
            MockDM.return_value = dm

            # Phases 3-4: no changes
            for MockPhase, name in [(MockIR, "importance_reeval"), (MockCP, "consolidation")]:
                inst = AsyncMock()
                inst.execute = AsyncMock(return_value=PhaseResult(phase_name=name))
                MockPhase.return_value = inst

            # Reindex: capture what it receives
            ri_inst = AsyncMock()

            async def capture_reindex(changed, uid, ws=None, ctx=None):
                reindex_received.extend(changed)
                return PhaseResult(phase_name="reindex")

            ri_inst.execute = capture_reindex
            MockRI.return_value = ri_inst

            orchestrator = SleepOrchestrator(mock_db)
            await orchestrator.run("user-1", "ws-1", "ctx-1", config=config)

        assert mem_id_1 in reindex_received
        assert mem_id_2 in reindex_received


class TestSleepMode:
    """Test context-level sleep_mode dispatch."""

    @pytest.mark.asyncio
    async def test_skip_mode_does_nothing(self, mock_db):
        """sleep_mode='skip' should return immediately without running any phases."""
        config = _make_config()

        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
        ):
            reporter = AsyncMock()
            MockReporter.return_value = reporter

            orchestrator = SleepOrchestrator(mock_db)
            orchestrator._get_sleep_mode = AsyncMock(return_value="skip")

            await orchestrator.run("user-1", "ws-1", "ctx-1", config=config)

        # No report should be created for skip mode
        reporter.create_report.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_edges_only_mode_skips_dedup_importance_consolidation(self, mock_db):
        """sleep_mode='edges_only' should only run edge_discovery + reindex."""
        config = _make_config()
        phases_run = []

        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase") as MockED,
            patch("services.sleep.orchestrator.DedupMergePhase") as MockDM,
            patch("services.sleep.orchestrator.ImportanceReevalPhase") as MockIR,
            patch("services.sleep.orchestrator.ConsolidationPhase") as MockCP,
            patch("services.sleep.orchestrator.ReindexPhase") as MockRI,
        ):
            reporter = AsyncMock()
            report = MagicMock()
            report.id = uuid4()
            reporter.create_report = AsyncMock(return_value=report)
            reporter.complete_report = AsyncMock()
            MockReporter.return_value = reporter

            # Track which phases actually execute
            for MockPhase, name in [
                (MockED, "edge_discovery"),
                (MockDM, "dedup_merge"),
                (MockIR, "importance_reeval"),
                (MockCP, "consolidation"),
            ]:
                inst = AsyncMock()

                async def make_result(cfg, uid, ws, ctx, budget, n=name, **kwargs):
                    phases_run.append(n)
                    return PhaseResult(phase_name=n)

                inst.execute = make_result
                MockPhase.return_value = inst

            ri_inst = AsyncMock()
            ri_inst.execute = AsyncMock(return_value=PhaseResult(phase_name="reindex"))
            MockRI.return_value = ri_inst

            orchestrator = SleepOrchestrator(mock_db)
            orchestrator._get_sleep_mode = AsyncMock(return_value="edges_only")

            await orchestrator.run("user-1", "ws-1", "ctx-1", config=config)

        # Only edge_discovery should have executed
        assert "edge_discovery" in phases_run
        assert "dedup_merge" not in phases_run
        assert "importance_reeval" not in phases_run
        assert "consolidation" not in phases_run

        # Report should include skipped phases
        reporter.complete_report.assert_awaited_once()
        results = reporter.complete_report.call_args[0][1]
        skipped_names = [r.phase_name for r in results if r.skipped]
        assert "dedup_merge" in skipped_names
        assert "importance_reeval" in skipped_names
        assert "consolidation" in skipped_names
