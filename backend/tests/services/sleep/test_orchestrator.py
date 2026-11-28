"""Tests for Sleep Maintenance Orchestrator.

Issue #101/#103: Phase execution order, budget enforcement, error isolation.
"""

import copy
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.orchestrator import SleepOrchestrator
from services.sleep.reporter import PhaseResult, SleepBudget


@pytest.fixture
def mock_db():
    db = AsyncMock()
    # #1229: begin_nested() is a sync call returning an async context
    # manager (each phase runs inside its own SAVEPOINT).
    db.begin_nested = MagicMock(return_value=AsyncMock())
    return db


@pytest.fixture(autouse=True)
def coverage_lookup():
    """#3019: the run reads its partition's last coverage positions from
    sleep_reports. These tests pass a mock session and non-UUID scope strings,
    so the lookup answers "no state" unless a test sets it."""
    with patch("services.sleep.orchestrator.current_state", AsyncMock(return_value=None)) as lookup:
        yield lookup


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
    @pytest.mark.parametrize("related_enabled", [False, True])
    async def test_runs_all_phases_in_order(self, mock_db, monkeypatch, related_enabled):
        """All 6 phases should execute in order."""
        monkeypatch.setenv("SLEEP_RELATED_INDEX_ENABLED", str(related_enabled).lower())
        config = _make_config()
        phase_names_executed = []

        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase") as MockED,
            patch("services.sleep.orchestrator.RelatedIndexPhase") as MockRelated,
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

            async def related_exec(*args, **kwargs):
                phase_names_executed.append("related_index")
                return PhaseResult(phase_name="related_index")

            MockRelated.return_value.execute = related_exec

            # Reindex
            reindex_instance = AsyncMock()

            async def reindex_exec(changed, uid, ws=None, ctx=None, **kwargs):
                phase_names_executed.append("reindex")
                return PhaseResult(phase_name="reindex")

            reindex_instance.execute = reindex_exec
            MockRI.return_value = reindex_instance

            orchestrator = SleepOrchestrator(mock_db)
            # ctx-1 is not a valid UUID; _get_sleep_mode falls back to "skip"
            # post-#558, which would early-return. Mock to keep the legacy
            # "all phases run" semantics this test depends on.
            orchestrator._get_sleep_mode = AsyncMock(return_value="full")
            await orchestrator.run("user-1", "ws-1", "ctx-1", config=config)

        # Verify order
        assert phase_names_executed == [
            "edge_discovery",
            *(["related_index"] if related_enabled else []),
            "dedup_merge",
            "importance_reeval",
            "consolidation",
            "reindex",
        ]
        reporter.create_report.assert_awaited_once()
        reporter.complete_report.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_takes_the_graph_learning_lock_before_the_first_phase(self, mock_db):
        """#1985: Sleep holds the user's graph learning lock for its whole
        transaction, taken before any phase runs."""
        config = _make_config()
        events: list[str] = []

        async def lock(db, user_id):
            events.append(f"lock:{user_id}")

        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase") as MockED,
            patch("services.sleep.orchestrator.DedupMergePhase"),
            patch("services.sleep.orchestrator.ImportanceReevalPhase"),
            patch("services.sleep.orchestrator.ConsolidationPhase"),
            patch("services.sleep.orchestrator.ReindexPhase"),
            patch("services.sleep.orchestrator.lock_graph_learning", side_effect=lock) as locked,
        ):
            reporter = AsyncMock()
            reporter.create_report = AsyncMock(return_value=MagicMock(id=uuid4()))
            MockReporter.return_value = reporter

            async def first_phase(*args, **kwargs):
                events.append("edge_discovery")
                return PhaseResult(phase_name="edge_discovery")

            MockED.return_value.execute = first_phase

            orchestrator = SleepOrchestrator(mock_db)
            orchestrator._get_sleep_mode = AsyncMock(return_value="edges_only")
            await orchestrator.run("user-1", "ws-1", "ctx-1", config=config)

        locked.assert_awaited_once_with(mock_db, "user-1")
        assert events[:2] == ["lock:user-1", "edge_discovery"]

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

            async def ri_exec(changed, uid, ws=None, ctx=None, **kwargs):
                phases_completed.append("reindex")
                return PhaseResult(phase_name="reindex")

            ri_inst.execute = ri_exec
            MockRI.return_value = ri_inst

            orchestrator = SleepOrchestrator(mock_db)
            orchestrator._get_sleep_mode = AsyncMock(return_value="full")
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
        mem_id_3 = uuid4()
        reindex_received = []
        content_received: set = set()

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
                    content_changed_memory_ids={mem_id_2},
                )
            )
            MockDM.return_value = dm

            # Phase 3: a metadata-only change (#2015 D5); phase 4: no changes
            ir = AsyncMock()
            ir.execute = AsyncMock(
                return_value=PhaseResult(
                    phase_name="importance_reeval",
                    changed_memory_ids={mem_id_3},
                    content_changed_memory_ids=set(),
                )
            )
            MockIR.return_value = ir
            cp = AsyncMock()
            cp.execute = AsyncMock(return_value=PhaseResult(phase_name="consolidation"))
            MockCP.return_value = cp

            # Reindex: capture what it receives
            ri_inst = AsyncMock()

            async def capture_reindex(changed, uid, ws=None, ctx=None, **kwargs):
                reindex_received.extend(changed)
                content_received.update(kwargs["content_changed_memory_ids"])
                return PhaseResult(phase_name="reindex")

            ri_inst.execute = capture_reindex
            MockRI.return_value = ri_inst

            orchestrator = SleepOrchestrator(mock_db)
            orchestrator._get_sleep_mode = AsyncMock(return_value="full")
            await orchestrator.run("user-1", "ws-1", "ctx-1", config=config)

        assert set(reindex_received) == {mem_id_1, mem_id_2, mem_id_3}
        # #2015 D5: an unclassified phase (edge_discovery here) counts as a
        # content change; the importance re-eval's metadata-only id does not.
        assert content_received == {mem_id_1, mem_id_2}


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


class TestGetSleepModeFallback:
    """_get_sleep_mode() returns 'skip' when context cannot be resolved (#558).

    The fallback aligns with the column default flip from 'full' → 'skip':
    if we can't determine the mode, fail safe by not running LLM phases.
    """

    @pytest.mark.asyncio
    async def test_returns_skip_when_context_id_is_none(self, mock_db):
        orchestrator = SleepOrchestrator(mock_db)
        assert await orchestrator._get_sleep_mode(None) == "skip"

    @pytest.mark.asyncio
    async def test_returns_skip_when_lookup_raises(self, mock_db):
        orchestrator = SleepOrchestrator(mock_db)
        mock_db.execute = AsyncMock(side_effect=RuntimeError("db down"))
        assert await orchestrator._get_sleep_mode(str(uuid4())) == "skip"

    @pytest.mark.asyncio
    async def test_returns_db_value_when_context_found(self, mock_db):
        orchestrator = SleepOrchestrator(mock_db)
        ctx = MagicMock()
        ctx.sleep_mode = "edges_only"
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ctx)
        mock_db.execute = AsyncMock(return_value=result)
        mock_db.get = AsyncMock(return_value=_workspace("pro"))
        assert await orchestrator._get_sleep_mode(str(uuid4())) == "edges_only"


def _workspace(plan: str):
    from models.auth import Workspace

    return Workspace(id=uuid4(), name="w", plan_name=plan, owner_user_id="u")


class TestSleepSuspendedOnFree:
    """#1939: a Free-again workspace keeps each context's sleep_mode, but Sleep
    (LLM cost) does not run — for the nightly sweep and the admin trigger
    alike, since both go through ``_get_sleep_mode``."""

    def _orchestrator(self, mock_db, plan: str, sleep_mode: str = "full"):
        ctx = MagicMock()
        ctx.sleep_mode = sleep_mode
        ctx.workspace_id = uuid4()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=ctx)
        mock_db.execute = AsyncMock(return_value=result)
        mock_db.get = AsyncMock(return_value=_workspace(plan))
        return SleepOrchestrator(mock_db), ctx

    @pytest.mark.asyncio
    async def test_free_workspace_full_mode_is_suspended(self, mock_db):
        """Suspended rather than skipped: the non-LLM retention phases keep running."""
        orchestrator, ctx = self._orchestrator(mock_db, "free")
        assert await orchestrator._get_sleep_mode(str(uuid4())) == "suspended"
        # Stored preference is untouched: re-subscribing resumes it.
        assert ctx.sleep_mode == "full"

    @pytest.mark.asyncio
    async def test_free_workspace_edges_only_skips(self, mock_db):
        """edges_only never ran the retention phases, so there is nothing left to run."""
        orchestrator, ctx = self._orchestrator(mock_db, "free", "edges_only")
        assert await orchestrator._get_sleep_mode(str(uuid4())) == "skip"
        assert ctx.sleep_mode == "edges_only"

    @pytest.mark.asyncio
    async def test_suspended_run_keeps_retention_and_pauses_the_llm_phases(self, mock_db):
        """Retention / privacy cleanup is housekeeping, not the paid LLM work:
        it keeps purging on schedule while Sleep is suspended."""
        executed: list[str] = []
        patches = {
            "edge_discovery": "EdgeDiscoveryPhase",
            "dedup_merge": "DedupMergePhase",
            "merge_retention": "MergeRetentionPhase",
            "forget_retention": "ForgetRetentionPhase",
            "measurement_retention": "MeasurementRetentionPhase",
            "importance_reeval": "ImportanceReevalPhase",
            "consolidation": "ConsolidationPhase",
        }
        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.ReindexPhase") as MockRI,
        ):
            reporter = AsyncMock()
            MockReporter.return_value = reporter
            phase_patches = []
            for name, cls in patches.items():
                inst = AsyncMock()

                async def _execute(cfg, uid, ws, ctx, budget, n=name, **kwargs):
                    executed.append(n)
                    return PhaseResult(phase_name=n)

                inst.execute = _execute
                p = patch(f"services.sleep.orchestrator.{cls}", return_value=inst)
                p.start()
                phase_patches.append(p)
            MockRI.return_value.execute = AsyncMock(return_value=PhaseResult(phase_name="reindex"))
            try:
                orchestrator = SleepOrchestrator(mock_db)
                orchestrator._get_sleep_mode = AsyncMock(return_value="suspended")
                orchestrator._get_context_embedding_info = AsyncMock(
                    return_value=(None, "kagura_memories")
                )
                await orchestrator.run("user-1", "ws-1", str(uuid4()), config=_make_config())
            finally:
                for p in phase_patches:
                    p.stop()

        assert executed == ["merge_retention", "forget_retention", "measurement_retention"]
        results = reporter.complete_report.call_args[0][1]
        skipped = {r.phase_name: r.skip_reason for r in results if r.skipped}
        assert set(skipped) == {
            "edge_discovery",
            "dedup_merge",
            "importance_reeval",
            "consolidation",
        }
        assert set(skipped.values()) == {"sleep_mode_suspended"}

    @pytest.mark.asyncio
    async def test_pro_workspace_runs_its_mode(self, mock_db):
        orchestrator, _ = self._orchestrator(mock_db, "pro", "edges_only")
        assert await orchestrator._get_sleep_mode(str(uuid4())) == "edges_only"


class TestPhaseFailureIsolation:
    """#1229: every phase runs inside its own SAVEPOINT (begin_nested), so a
    phase that dies mid-flush (or aborts the DB transaction with a direct
    statement error) rolls back ONLY its own pending work. The SleepReport
    row, earlier phases' work, and the outer transaction survive — a
    session-level rollback here would discard the flushed report row
    (scheduler path) or expire the pre-committed report instance (manual
    path), cascading FK violations / MissingGreenlet through the rest of
    the run.
    """

    @staticmethod
    def _db_with_nested():
        db = AsyncMock()
        # begin_nested() is a sync call returning an async context manager.
        db.begin_nested = MagicMock(return_value=AsyncMock())
        return db

    def _orchestrator(self, db):
        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter"),
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase"),
            patch("services.sleep.orchestrator.DedupMergePhase"),
            patch("services.sleep.orchestrator.ImportanceReevalPhase"),
            patch("services.sleep.orchestrator.ConsolidationPhase"),
            patch("services.sleep.orchestrator.ReindexPhase"),
        ):
            return SleepOrchestrator(db)

    @staticmethod
    def _failing_phase():
        phase = AsyncMock()
        phase.execute = AsyncMock(side_effect=RuntimeError("died mid-flush"))
        return phase

    @pytest.mark.asyncio
    async def test_failed_phase_is_contained_by_its_savepoint(self):
        db = self._db_with_nested()
        orch = self._orchestrator(db)

        result = await orch._run_phase(
            "dedup_merge",
            self._failing_phase(),
            _make_config(),
            "user-1",
            None,
            None,
            SleepBudget(),
            uuid4(),
        )

        assert result.success is False
        assert "died mid-flush" in (result.error or "")
        db.begin_nested.assert_called_once()
        # No session-level rollback: that would discard the report row and
        # earlier phases' pending work along with the failed phase's.
        db.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_phase_runs_inside_a_savepoint(self):
        db = self._db_with_nested()
        orch = self._orchestrator(db)
        phase = AsyncMock()
        phase.execute = AsyncMock(return_value=PhaseResult(phase_name="dedup_merge"))

        result = await orch._run_phase(
            "dedup_merge",
            phase,
            _make_config(),
            "user-1",
            None,
            None,
            SleepBudget(),
            uuid4(),
        )

        assert result.success is True
        db.begin_nested.assert_called_once()
        db.rollback.assert_not_awaited()


class TestPointDeletesAfterTheCommit:
    """#2565: the archive and merge points a phase queued are deleted only
    after the caller's commit, and only for a phase whose savepoint held."""

    _ENTRY = (uuid4(), uuid4(), "kagura_memories")

    @staticmethod
    def _orchestrator():
        db = AsyncMock()
        db.begin_nested = MagicMock(return_value=AsyncMock())
        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter"),
        ):
            return SleepOrchestrator(db)

    async def _run(self, orch, *, fails: bool) -> PhaseResult:
        phase = MagicMock()
        phase.pending_point_deletes = [self._ENTRY]
        phase.execute = AsyncMock(
            side_effect=RuntimeError("phase failed") if fails else None,
            return_value=PhaseResult(phase_name="consolidation"),
        )
        return await orch._run_phase(
            "consolidation", phase, _make_config(), "user-1", None, None, SleepBudget(), uuid4()
        )

    @pytest.mark.asyncio
    async def test_a_completed_phases_points_wait_for_the_commit(self):
        orch = self._orchestrator()
        result = await self._run(orch, fails=False)

        assert result.success is True
        assert orch.pending_point_deletes == [self._ENTRY]

    @pytest.mark.asyncio
    async def test_a_failed_phases_points_went_with_its_savepoint(self):
        orch = self._orchestrator()
        result = await self._run(orch, fails=True)

        assert result.success is False
        assert orch.pending_point_deletes == []

    @pytest.mark.asyncio
    async def test_delete_committed_points_is_forgets_post_commit_step(self):
        orch = self._orchestrator()
        await self._run(orch, fails=False)

        with patch("services.memory_service.MemoryService") as service:
            service.return_value.delete_forgotten_points = AsyncMock()
            await orch.delete_committed_points()
            await orch.delete_committed_points()  # nothing left: no second call

        service.assert_called_once_with(orch.db)
        service.return_value.delete_forgotten_points.assert_awaited_once_with([self._ENTRY])
        assert orch.pending_point_deletes == []


class TestRaisedPhaseKeepsItsSpend:
    """#2060: a phase that raises still reports the LLM spend it already made.

    The savepoint discards the phase's DB work, but the provider billed the
    tokens; the failed ``PhaseResult`` used to be bare, so that spend never
    reached ``sleep_reports`` / ``sleep_report_llm_usage``.
    """

    @staticmethod
    def _orchestrator():
        db = AsyncMock()
        db.begin_nested = MagicMock(return_value=AsyncMock())
        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter"),
        ):
            return SleepOrchestrator(db)

    @staticmethod
    def _spending_phase():
        from services.sleep.reporter import LLMCallBreakdown, LLMSpendTracker

        class SpendThenRaise(LLMSpendTracker):
            async def execute(self, config, user_id, workspace_id, context_id, budget, **kwargs):
                # Two judge calls billed (one succeeded, one failed with usage)
                # and one unbilled failure, then the phase dies.
                budget.consume(llm_calls=2)
                self._tokens_used = 170
                self._llm_failures = 2
                self._llm_breakdown = LLMCallBreakdown(
                    provider="openai",
                    model="gpt-5-nano",
                    input_tokens=120,
                    output_tokens=50,
                    calls=1,
                )
                raise RuntimeError("died after the judge")

        return SpendThenRaise()

    @pytest.mark.asyncio
    async def test_failed_phase_result_carries_the_spend(self):
        budget = SleepBudget()
        budget.consume(llm_calls=5)  # earlier phases

        result = await self._orchestrator()._run_phase(
            "consolidation",
            self._spending_phase(),
            _make_config(),
            "user-1",
            None,
            None,
            budget,
            uuid4(),
        )

        assert result.success is False
        assert "died after the judge" in (result.error or "")
        # Only this phase's share of the shared budget.
        assert result.llm_calls_used == 2
        assert result.tokens_used == 170
        assert result.llm_call_failures == 2
        assert len(result.llm_breakdown) == 1
        assert result.llm_breakdown[0].total_tokens == 170
        # Grading invariant: breakdown calls never exceed llm_calls_used.
        assert sum(b.calls for b in result.llm_breakdown) <= result.llm_calls_used

    @pytest.mark.asyncio
    async def test_phase_without_spend_tracking_stays_bare(self):
        phase = MagicMock()
        phase.execute = AsyncMock(side_effect=RuntimeError("boom"))

        result = await self._orchestrator()._run_phase(
            "merge_retention",
            phase,
            _make_config(),
            "user-1",
            None,
            None,
            SleepBudget(),
            uuid4(),
        )

        assert result.success is False
        assert result.tokens_used == 0
        assert result.llm_breakdown == []

    def test_llm_phases_track_spend(self):
        from services.sleep.consolidation import ConsolidationPhase
        from services.sleep.dedup_merge import DedupMergePhase
        from services.sleep.edge_discovery import EdgeDiscoveryPhase
        from services.sleep.importance_reeval import ImportanceReevalPhase
        from services.sleep.reporter import LLMSpendTracker

        for phase_cls in (
            EdgeDiscoveryPhase,
            DedupMergePhase,
            ImportanceReevalPhase,
            ConsolidationPhase,
        ):
            assert issubclass(phase_cls, LLMSpendTracker), phase_cls.__name__

    @pytest.mark.parametrize("phase_name", ["edge_discovery", "dedup_merge"])
    def test_record_spend_carries_embedding_usage(self, phase_name):
        from services.sleep.dedup_merge import DedupMergePhase
        from services.sleep.edge_discovery import EdgeDiscoveryPhase

        phase_cls = EdgeDiscoveryPhase if phase_name == "edge_discovery" else DedupMergePhase
        with patch(f"services.sleep.{phase_name}.EmbeddingService") as MockEmb:
            MockEmb.return_value.provider = "openai"
            MockEmb.return_value.model = "text-embedding-3-small"
            phase = phase_cls(AsyncMock(), MagicMock())
        phase._embedding_calls_used = 3
        phase._embedding_tokens_used = 900

        result = PhaseResult(phase_name=phase_name, success=False)
        phase.record_spend(result)

        assert result.embedding_calls_used == 3
        assert result.embedding_tokens == 900
        assert result.embedding_provider == "openai"
        assert result.embedding_model == "text-embedding-3-small"
        # No LLM call happened: no fabricated breakdown row.
        assert result.llm_breakdown == []
        assert result.tokens_used == 0


class TestPerPhaseBudgets:
    """#3019: every phase gets its own budget. The memory allowance is the full
    ``sleep_max_memories_per_run`` for each phase; the LLM calls are shared in
    equal parts among the phases of the run that call a model, and what one
    leaves unused rolls forward. A phase that calls no model is never skipped
    for budget."""

    _CLASSES = {
        "edge_discovery": "EdgeDiscoveryPhase",
        "related_index": "RelatedIndexPhase",
        "dedup_merge": "DedupMergePhase",
        "merge_retention": "MergeRetentionPhase",
        "forget_retention": "ForgetRetentionPhase",
        "measurement_retention": "MeasurementRetentionPhase",
        "importance_reeval": "ImportanceReevalPhase",
        "consolidation": "ConsolidationPhase",
    }

    async def _run(self, mock_db, monkeypatch, config, spend):
        """Run a full-mode pass with phases that spend per ``spend[name](budget)``.

        Returns {phase name: (max_llm_calls, max_memories, memories_used at
        entry)} for every phase that executed, and the report's phase results.
        """
        monkeypatch.setenv("SLEEP_RELATED_INDEX_ENABLED", "true")
        seen: dict[str, tuple[int, int, int]] = {}
        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.ReindexPhase") as MockRI,
        ):
            reporter = AsyncMock()
            report = MagicMock()
            report.id = uuid4()
            reporter.create_report = AsyncMock(return_value=report)
            MockReporter.return_value = reporter
            started = []
            for name, cls in self._CLASSES.items():
                inst = MagicMock()

                async def _execute(cfg, uid, ws, ctx, budget, n=name, **kwargs):
                    seen[n] = (budget.max_llm_calls, budget.max_memories, budget.memories_used)
                    processed = spend.get(n, lambda _b: 0)(budget)
                    return PhaseResult(phase_name=n, memories_processed=processed)

                inst.execute = _execute
                inst.pending_point_deletes = []
                p = patch(f"services.sleep.orchestrator.{cls}", return_value=inst)
                p.start()
                started.append(p)
            MockRI.return_value.execute = AsyncMock(return_value=PhaseResult(phase_name="reindex"))
            try:
                orchestrator = SleepOrchestrator(mock_db)
                orchestrator._get_sleep_mode = AsyncMock(return_value="full")
                orchestrator._get_context_embedding_info = AsyncMock(
                    return_value=(None, "kagura_memories")
                )
                await orchestrator.run("user-1", "ws-1", str(uuid4()), config=config)
            finally:
                for p in started:
                    p.stop()
        return seen, reporter.complete_report.call_args[0][1]

    @staticmethod
    def _all(budget: SleepBudget) -> int:
        """Use the whole budget: every LLM call and every memory."""
        budget.consume(llm_calls=budget.max_llm_calls, memories=budget.max_memories)
        return budget.max_memories

    @pytest.mark.asyncio
    async def test_edge_discovery_using_its_whole_allowance_starves_no_other_phase(
        self, mock_db, monkeypatch
    ):
        """Acceptance 1: edge discovery's sample at the memory budget (it
        charges all 200 memories and all of its LLM calls); dedup, the
        importance re-evaluation, consolidation and the related index still
        run, each with the full memory allowance."""
        config = _make_config()
        config.sleep_edge_discovery_sample_size = 200
        config.sleep_consolidation_judge_enabled = True

        spend_all = dict.fromkeys(("edge_discovery", "dedup_merge", "importance_reeval"), self._all)
        seen, results = await self._run(mock_db, monkeypatch, config, spend_all)

        assert set(seen) == set(self._CLASSES)
        assert not [r.phase_name for r in results if r.skipped]
        for name in ("related_index", "dedup_merge", "importance_reeval", "consolidation"):
            assert seen[name][1:] == (200, 0), name
        # Four phases call a model: 50 calls in shares of 13, 13, 12, 12.
        assert [
            seen[n][0]
            for n in ("edge_discovery", "dedup_merge", "importance_reeval", "consolidation")
        ] == [13, 13, 12, 12]
        # Each phase charged its own allowance (a run total above the setting).
        assert sum(r.memories_processed for r in results) == 600

    @pytest.mark.asyncio
    async def test_a_phase_without_model_calls_runs_with_zero_model_budget(
        self, mock_db, monkeypatch
    ):
        """Acceptance 2: one LLM call for the run, used up by edge discovery.
        Dedup and importance (model phases with nothing left) are skipped for
        budget; consolidation's rule pass, the three retention phases and the
        related index run with no LLM calls."""
        config = _make_config()
        config.sleep_max_llm_calls_per_run = 1
        config.sleep_consolidation_judge_enabled = True

        seen, results = await self._run(mock_db, monkeypatch, config, {"edge_discovery": self._all})

        skipped = {r.phase_name: r.skip_reason for r in results if r.skipped}
        assert skipped == {
            "dedup_merge": "budget_exhausted",
            "importance_reeval": "budget_exhausted",
        }
        for name in (
            "related_index",
            "merge_retention",
            "forget_retention",
            "measurement_retention",
            "consolidation",
        ):
            assert seen[name] == (0, 200, 0), name

    @pytest.mark.asyncio
    async def test_unused_calls_roll_forward_to_the_next_model_phase(self, mock_db, monkeypatch):
        """Edge discovery spends 3 of its 13; dedup gets 13 + 10. The run as a
        whole never exceeds sleep_max_llm_calls_per_run."""
        config = _make_config()
        config.sleep_consolidation_judge_enabled = True

        def three(budget: SleepBudget) -> int:
            budget.consume(llm_calls=3)
            return 0

        seen, _ = await self._run(
            mock_db,
            monkeypatch,
            config,
            {"edge_discovery": three, "dedup_merge": self._all, "importance_reeval": self._all},
        )

        assert seen["dedup_merge"][0] == 23
        assert seen["importance_reeval"][0] == 12
        assert seen["consolidation"][0] == 12
        assert 3 + 23 + 12 + 12 == 50

    @pytest.mark.asyncio
    async def test_only_the_phases_that_call_a_model_share_the_calls(self, mock_db, monkeypatch):
        """Edge discovery and importance off, judge off: dedup is the only
        model phase and gets all 50 calls; consolidation runs its rule pass
        with none."""
        config = _make_config()
        config.sleep_edge_discovery_enabled = False
        config.sleep_importance_reeval_enabled = False
        config.sleep_consolidation_judge_enabled = False

        seen, results = await self._run(mock_db, monkeypatch, config, {})

        assert seen["dedup_merge"][0] == 50
        assert seen["consolidation"][0] == 0
        assert seen["edge_discovery"][0] == 0
        assert not [r for r in results if r.skip_reason == "budget_exhausted"]


class TestCoverageState:
    """#3019: the orchestrator reads the partition's positions after the graph
    learning lock, hands dedup its lanes, and writes the positions the run
    leaves before completing the report. A phase never writes them."""

    async def _run(
        self,
        mock_db,
        *,
        sleep_mode="full",
        dedup=None,
        dedup_raises=False,
        judge=None,
        judge_raises=False,
    ):
        from services.sleep.coverage import DedupCoverage

        events: list[str] = []
        report = MagicMock()
        report.id = uuid4()
        report.coverage_state = None
        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase") as MockED,
            patch("services.sleep.orchestrator.DedupMergePhase") as MockDM,
            patch("services.sleep.orchestrator.ImportanceReevalPhase") as MockIR,
            patch("services.sleep.orchestrator.ConsolidationPhase") as MockCP,
            patch("services.sleep.orchestrator.ReindexPhase") as MockRI,
            patch(
                "services.sleep.orchestrator.lock_graph_learning",
                AsyncMock(side_effect=lambda *a: events.append("lock")),
            ),
        ):
            reporter = AsyncMock()
            reporter.create_report = AsyncMock(return_value=report)
            # What complete_report saw when it ran (the report and the phase
            # results are shared objects; reading them after run() returns
            # cannot tell whether the state was set before or after the call).
            self.at_complete: dict = {}

            async def complete(rep, results):
                events.append("complete")
                dedup_result = next((r for r in results if r.phase_name == "dedup_merge"), None)
                details = dedup_result.details if dedup_result is not None else None
                self.at_complete = {
                    "coverage_state": copy.deepcopy(rep.coverage_state),
                    "dedup_coverage": copy.deepcopy(
                        details.get("coverage") if isinstance(details, dict) else None
                    ),
                }

            reporter.complete_report = AsyncMock(side_effect=complete)
            MockReporter.return_value = reporter
            for mock, name in (
                (MockED, "edge_discovery"),
                (MockIR, "importance_reeval"),
                (MockCP, "consolidation"),
            ):
                mock.return_value.execute = AsyncMock(return_value=PhaseResult(phase_name=name))
            instance = MagicMock()
            instance.pending_point_deletes = []
            if isinstance(dedup, DedupCoverage):
                instance.coverage_outcome = dedup

            async def dedup_execute(*args, **kwargs):
                if dedup_raises:
                    raise RuntimeError("dedup crashed")
                return PhaseResult(phase_name="dedup_merge", details={"merged": 0})

            instance.execute = dedup_execute
            MockDM.return_value = instance
            if judge is not None:
                MockCP.return_value.coverage_outcome = judge
                MockCP.return_value.execute = AsyncMock(
                    side_effect=RuntimeError("judge crashed") if judge_raises else None,
                    return_value=PhaseResult(phase_name="consolidation", details={}),
                )
            MockRI.return_value.execute = AsyncMock(return_value=PhaseResult(phase_name="reindex"))

            orchestrator = SleepOrchestrator(mock_db)
            orchestrator._get_sleep_mode = AsyncMock(return_value=sleep_mode)
            orchestrator._get_context_embedding_info = AsyncMock(return_value=(None, "c"))
            await orchestrator.run("user-1", "ws-1", str(uuid4()), config=_make_config())
        self.consolidation_class = MockCP
        return report, MockDM, reporter, events

    @pytest.mark.asyncio
    async def test_dedup_gets_the_stored_lanes(self, mock_db, coverage_lookup):
        from services.sleep.coverage import CoverageState, DedupLanes, LaneEntry

        lanes = DedupLanes(sweep=LaneEntry(passes=3), turn="sweep")
        coverage_lookup.return_value = CoverageState(dedup=lanes)

        _, MockDM, _, _ = await self._run(mock_db)

        coverage_lookup.assert_awaited_once()
        assert MockDM.call_args.kwargs["coverage"] == lanes

    @pytest.mark.asyncio
    async def test_no_lookup_when_neither_phase_can_run(self, mock_db, coverage_lookup):
        await self._run(mock_db, sleep_mode="edges_only")
        coverage_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_window_leaves_the_column_null(self, mock_db):
        report, _, _, _ = await self._run(mock_db)
        assert report.coverage_state is None

    @pytest.mark.asyncio
    async def test_the_window_becomes_the_next_state_before_the_report_completes(self, mock_db):
        from datetime import timedelta

        from services.sleep.coverage import SETTLED, DedupCoverage, parse_state
        from utils.datetime import utcnow

        row = (utcnow() - timedelta(days=3), uuid4())
        window = DedupCoverage(
            changed=[row], sweep=[], floor=None, exhausted=True, outcomes={row[1]: SETTLED}
        )
        report, _, reporter, events = await self._run(mock_db, dedup=window)

        assert events.index("lock") < events.index("complete")
        # Both were already set when complete_report ran, and nothing changed
        # them afterwards.
        state = parse_state(self.at_complete["coverage_state"])
        assert state.dedup.changed.position == row
        assert state.dedup.turn == "sweep"
        assert self.at_complete["dedup_coverage"]["changed"]["advanced"] == 1
        assert report.coverage_state == self.at_complete["coverage_state"]
        results = reporter.complete_report.call_args.args[1]
        dedup = next(r for r in results if r.phase_name == "dedup_merge")
        assert dedup.details["coverage"] == self.at_complete["dedup_coverage"]

    @pytest.mark.asyncio
    async def test_a_phase_that_raised_after_its_read_stalls(self, mock_db):
        from datetime import timedelta

        from services.sleep.coverage import FAILURE, DedupCoverage, parse_state
        from utils.datetime import utcnow

        row = (utcnow() - timedelta(days=3), uuid4())
        window = DedupCoverage(
            changed=[row], sweep=[], floor=None, exhausted=False, outcomes={row[1]: FAILURE}
        )
        report, _, reporter, _ = await self._run(mock_db, dedup=window, dedup_raises=True)

        state = parse_state(report.coverage_state)
        assert state.dedup.changed.position is None
        assert state.dedup.changed.stalls == 1
        # The phase did not return: the turn stays.
        assert state.dedup.turn == "changed"
        # What the run did to the lanes is in the stored blob although the
        # phase raised; its point-5 keys stay absent.
        results = reporter.complete_report.call_args.args[1]
        dedup = next(r for r in results if r.phase_name == "dedup_merge")
        assert dedup.error == "dedup crashed"
        assert set(dedup.details) == {"coverage"}
        assert dedup.details["coverage"]["changed"]["taken"] == 1
        assert dedup.details["coverage"]["changed"]["advanced"] == 0
        assert dedup.details["coverage"]["skipped_after_stalls"] == 0

    @pytest.mark.asyncio
    async def test_a_raised_phase_reports_the_window_its_stalls_passed(
        self, mock_db, coverage_lookup
    ):
        """Invariant 1's one exception, the stall rule, is counted in the
        report: on the third run that raises, the window is passed and the
        stored dedup blob says how many rows were passed that way."""
        from datetime import timedelta

        from services.sleep.coverage import (
            STALL_LIMIT,
            CoverageState,
            DedupCoverage,
            DedupLanes,
            LaneEntry,
            parse_state,
        )
        from utils.datetime import utcnow

        coverage_lookup.return_value = CoverageState(
            dedup=DedupLanes(
                changed=LaneEntry(stalls=STALL_LIMIT - 1),
                sweep=LaneEntry(stalls=STALL_LIMIT - 1),
            )
        )
        changed = (utcnow() - timedelta(days=3), uuid4())
        sweep = (utcnow() - timedelta(days=30), uuid4())
        window = DedupCoverage(changed=[changed], sweep=[sweep], floor=None, exhausted=False)

        report, _, reporter, _ = await self._run(mock_db, dedup=window, dedup_raises=True)

        state = parse_state(report.coverage_state)
        assert state.dedup.changed.position == changed
        assert state.dedup.sweep.position == sweep
        assert (state.dedup.changed.stalls, state.dedup.sweep.stalls) == (0, 0)
        # Set before complete_report stored the blob.
        assert self.at_complete["dedup_coverage"]["skipped_after_stalls"] == 2
        results = reporter.complete_report.call_args.args[1]
        dedup = next(r for r in results if r.phase_name == "dedup_merge")
        assert set(dedup.details) == {"coverage"}
        assert dedup.details["coverage"]["skipped_after_stalls"] == 2

    @pytest.mark.asyncio
    async def test_a_raised_judge_reports_the_rows_its_stalls_passed(
        self, mock_db, coverage_lookup
    ):
        from datetime import timedelta

        from services.sleep.coverage import (
            STALL_LIMIT,
            CoverageState,
            JudgeCoverage,
            LaneEntry,
            parse_state,
        )
        from utils.datetime import utcnow

        coverage_lookup.return_value = CoverageState(judge=LaneEntry(stalls=STALL_LIMIT - 1))
        rows = [(utcnow() - timedelta(days=d), uuid4()) for d in (8, 7)]

        report, _, reporter, _ = await self._run(
            mock_db, judge=JudgeCoverage(keys=rows), judge_raises=True
        )

        state = parse_state(report.coverage_state)
        assert state.judge.stalls == 0
        results = reporter.complete_report.call_args.args[1]
        consolidation = next(r for r in results if r.phase_name == "consolidation")
        assert consolidation.error == "judge crashed"
        assert consolidation.details == {
            "coverage": {"judge": {"taken": 2, "advanced": 2}, "skipped_after_stalls": 2}
        }

    @pytest.mark.asyncio
    async def test_the_judge_gets_its_position_and_leaves_the_next(self, mock_db, coverage_lookup):
        from datetime import timedelta

        from services.sleep.coverage import (
            SETTLED,
            CoverageState,
            DedupLanes,
            JudgeCoverage,
            LaneEntry,
            parse_state,
        )
        from utils.datetime import utcnow

        entry = LaneEntry(at=utcnow() - timedelta(days=9), id=uuid4())
        lanes = DedupLanes(sweep=LaneEntry(passes=2))
        coverage_lookup.return_value = CoverageState(dedup=lanes, judge=entry)
        rows = [(utcnow() - timedelta(days=d), uuid4()) for d in (8, 7)]
        judge = JudgeCoverage(keys=rows, outcomes={rows[0][1]: SETTLED})

        report, _, reporter, _ = await self._run(mock_db, judge=judge)

        assert self.consolidation_class.call_args.kwargs["coverage"] == entry
        state = parse_state(report.coverage_state)
        assert state.judge.position == rows[0]
        # Dedup produced no window: its lanes are carried as they were.
        assert state.dedup == lanes
        results = reporter.complete_report.call_args.args[1]
        consolidation = next(r for r in results if r.phase_name == "consolidation")
        assert consolidation.details["coverage"] == {
            "judge": {"taken": 2, "advanced": 1},
            "skipped_after_stalls": 0,
        }


class TestSupersedeApplyPhase3020:
    """#3020: the supersede-apply phase runs last, before the reindex, only
    when the deployment switch is exactly True; it keeps one coverage entry."""

    async def _run(
        self,
        mock_db,
        *,
        switch=True,
        sleep_mode="full",
        outcome=None,
        raises=False,
        consolidation=None,
        judge_window=None,
    ):
        events: list[str] = []
        report = MagicMock()
        report.id = uuid4()
        report.coverage_state = None
        config = _make_config()
        config.sleep_supersede_apply_enabled = switch
        with (
            patch("services.sleep.orchestrator.LLMService"),
            patch("services.sleep.orchestrator.SleepReporter") as MockReporter,
            patch("services.sleep.orchestrator.EdgeDiscoveryPhase") as MockED,
            patch("services.sleep.orchestrator.DedupMergePhase") as MockDM,
            patch("services.sleep.orchestrator.MergeRetentionPhase") as MockMR,
            patch("services.sleep.orchestrator.ForgetRetentionPhase") as MockFR,
            patch("services.sleep.orchestrator.MeasurementRetentionPhase") as MockMS,
            patch("services.sleep.orchestrator.ImportanceReevalPhase") as MockIR,
            patch("services.sleep.orchestrator.ConsolidationPhase") as MockCP,
            patch("services.sleep.orchestrator.SupersedeApplyPhase") as MockSA,
            patch("services.sleep.orchestrator.ReindexPhase") as MockRI,
        ):
            reporter = AsyncMock()
            reporter.create_report = AsyncMock(return_value=report)
            MockReporter.return_value = reporter
            for mock, name in (
                (MockED, "edge_discovery"),
                (MockDM, "dedup_merge"),
                (MockMR, "merge_retention"),
                (MockFR, "forget_retention"),
                (MockMS, "measurement_retention"),
                (MockIR, "importance_reeval"),
                (MockCP, "consolidation"),
            ):

                async def execute(*args, n=name, **kwargs):
                    events.append(n)
                    if n == "consolidation" and consolidation is not None:
                        return consolidation
                    return PhaseResult(phase_name=n)

                mock.return_value = MagicMock(pending_point_deletes=[], coverage_outcome=None)
                mock.return_value.execute = execute
            MockCP.return_value.coverage_outcome = judge_window

            apply = MagicMock(pending_point_deletes=[], coverage_outcome=outcome)

            async def apply_execute(*args, **kwargs):
                events.append("supersede_apply")
                if raises:
                    raise RuntimeError("apply crashed")
                return PhaseResult(phase_name="supersede_apply", details={"applied": 0})

            apply.execute = apply_execute
            MockSA.return_value = apply

            async def reindex_execute(*args, **kwargs):
                events.append("reindex")
                return PhaseResult(phase_name="reindex")

            MockRI.return_value.execute = reindex_execute

            orchestrator = SleepOrchestrator(mock_db)
            orchestrator._get_sleep_mode = AsyncMock(return_value=sleep_mode)
            orchestrator._get_context_embedding_info = AsyncMock(return_value=(None, "c"))
            await orchestrator.run("user-1", "ws-1", str(uuid4()), config=config)
        results = reporter.complete_report.call_args.args[1]
        return report, MockSA, events, results

    @pytest.mark.asyncio
    async def test_switched_on_it_runs_last_before_the_reindex(self, mock_db):
        _, MockSA, events, results = await self._run(mock_db)
        assert events[-3:] == ["consolidation", "supersede_apply", "reindex"]
        assert [r.phase_name for r in results][-2:] == ["supersede_apply", "reindex"]
        MockSA.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("switch", [False, None, "true", 1, MagicMock()])
    async def test_anything_but_true_leaves_it_out(self, mock_db, switch):
        """``MagicMock()`` is what every other orchestrator test's config
        holds (truthy attributes): those keep today's phase list."""
        _, MockSA, events, results = await self._run(mock_db, switch=switch)
        MockSA.assert_not_called()
        assert "supersede_apply" not in events
        assert "supersede_apply" not in [r.phase_name for r in results]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["edges_only", "suspended"])
    async def test_outside_full_mode_it_is_listed_as_skipped(self, mock_db, mode):
        _, MockSA, events, results = await self._run(mock_db, sleep_mode=mode)
        MockSA.assert_not_called()
        skipped = next(r for r in results if r.phase_name == "supersede_apply")
        assert skipped.skipped is True
        assert skipped.skip_reason == f"sleep_mode_{mode}"

    @pytest.mark.asyncio
    async def test_it_gets_its_stored_entry(self, mock_db, coverage_lookup):
        from services.sleep.coverage import CoverageState, LaneEntry

        entry = LaneEntry(passes=4)
        coverage_lookup.return_value = CoverageState(apply=entry)
        _, MockSA, _, _ = await self._run(mock_db)
        assert MockSA.call_args.kwargs["coverage"] == entry

    @pytest.mark.asyncio
    async def test_its_outcome_alone_writes_the_next_state(self, mock_db):
        from datetime import timedelta

        from services.sleep.coverage import SETTLED, ApplyCoverage, parse_state
        from utils.datetime import utcnow

        rows = [(utcnow() - timedelta(days=d), uuid4()) for d in (3, 2)]
        outcome = ApplyCoverage(
            keys=rows, exhausted=False, outcomes={rows[0][1]: SETTLED, rows[1][1]: SETTLED}
        )
        report, _, _, results = await self._run(mock_db, outcome=outcome)

        state = parse_state(report.coverage_state)
        assert state is not None
        assert state.apply.position == rows[1]
        assert report.coverage_state["supersede_apply"]["sweep"]["id"] == str(rows[1][1])
        apply = next(r for r in results if r.phase_name == "supersede_apply")
        assert apply.details["coverage"] == {
            "sweep": {"taken": 2, "advanced": 2, "pass_completed": False, "passes": 0},
            "skipped_after_stalls": 0,
        }
        assert apply.details["applied"] == 0

    @pytest.mark.asyncio
    async def test_a_raised_phase_stalls(self, mock_db):
        from datetime import timedelta

        from services.sleep.coverage import ApplyCoverage, parse_state
        from utils.datetime import utcnow

        row = (utcnow() - timedelta(days=3), uuid4())
        outcome = ApplyCoverage(keys=[row], exhausted=True)
        report, _, _, results = await self._run(mock_db, outcome=outcome, raises=True)

        state = parse_state(report.coverage_state)
        assert state.apply.position is None
        assert state.apply.stalls == 1
        apply = next(r for r in results if r.phase_name == "supersede_apply")
        assert apply.error == "apply crashed"
        assert set(apply.details) == {"coverage"}
        assert apply.details["coverage"]["sweep"]["advanced"] == 0

    @pytest.mark.asyncio
    async def test_switched_on_without_an_outcome_the_column_stays_null(self, mock_db):
        report, _, _, _ = await self._run(mock_db)
        assert report.coverage_state is None

    @pytest.mark.asyncio
    async def test_a_run_already_graded_failed_skips_it_and_carries_its_entry(
        self, mock_db, coverage_lookup
    ):
        """Every judge call of the run raised: the run grades ``failed``, which
        ``rollback_sleep_run`` does not take, so no link is written under it."""
        from datetime import timedelta

        from services.sleep.coverage import (
            SETTLED,
            CoverageState,
            JudgeCoverage,
            LaneEntry,
            parse_state,
        )
        from utils.datetime import utcnow

        entry = LaneEntry(passes=4)
        coverage_lookup.return_value = CoverageState(apply=entry)
        row = (utcnow() - timedelta(days=2), uuid4())
        failed = PhaseResult(phase_name="consolidation", llm_call_failures=3)
        report, MockSA, events, results = await self._run(
            mock_db,
            consolidation=failed,
            judge_window=JudgeCoverage(keys=[row], outcomes={row[1]: SETTLED}),
        )

        MockSA.assert_not_called()
        assert "supersede_apply" not in events
        skipped = next(r for r in results if r.phase_name == "supersede_apply")
        assert (skipped.skipped, skipped.skip_reason) == (True, "run_failed")
        assert skipped.details is None
        # The judge's outcome wrote the state; the apply entry is carried.
        assert parse_state(report.coverage_state).apply == entry

    @pytest.mark.asyncio
    async def test_a_degraded_run_still_runs_it(self, mock_db):
        from services.sleep.reporter import LLMCallBreakdown

        degraded = PhaseResult(
            phase_name="consolidation",
            llm_call_failures=1,
            llm_breakdown=[LLMCallBreakdown(provider="p", model="m", calls=2)],
        )
        _, MockSA, events, _ = await self._run(mock_db, consolidation=degraded)
        MockSA.assert_called_once()
        assert events[-2:] == ["supersede_apply", "reindex"]


class TestJudgeTotalFailure3020:
    """#3020: the condition that grades a run ``failed``, shared by the report
    and the orchestrator (which skips the supersede-apply phase under it)."""

    @pytest.mark.parametrize(
        ("failures", "calls", "expected"),
        [(0, 0, False), (2, 0, True), (2, 1, False), (0, 3, False)],
    )
    def test_every_judge_call_raised(self, failures, calls, expected):
        from services.sleep.reporter import LLMCallBreakdown, judge_total_failure

        breakdown = [LLMCallBreakdown(provider="p", model="m", calls=calls)] if calls else []
        results = [
            PhaseResult(phase_name="dedup_merge", llm_call_failures=failures),
            PhaseResult(phase_name="consolidation", llm_breakdown=breakdown),
        ]
        assert judge_total_failure(results) is expected
