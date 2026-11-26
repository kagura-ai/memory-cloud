"""Tests for Sleep Maintenance Phase 4: Consolidation.

Issue #103: Rule-based parity with legacy consolidation_task,
LLM borderline path, bridge node protection.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.consolidation import (
    ADOPTION_PROMOTE_MIN,
    ADOPTION_PROMOTE_WITH_IMPORTANCE,
    ConsolidationPhase,
)
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
    ):
        phase = ConsolidationPhase(mock_db, mock_llm)
        phase.memory_repo = AsyncMock()
    return phase


def _make_config(provider="openai", model="gpt-5-nano", judge=True):
    config = MagicMock()
    config.sleep_llm_provider = provider
    config.sleep_llm_model = model
    # #3021: the judge switch (the NeuralMemoryConfig default is off).
    config.sleep_consolidation_judge_enabled = judge
    return config


def _make_working_memory(
    memory_id=None,
    importance=0.5,
    access_count=0,
    reference_count=0,
    age_days=10,
    created_at=None,
    tags=None,
):
    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = "test working memory"
    m.type = "note"
    m.importance = importance
    m.access_count = access_count
    m.reference_count = reference_count  # #1049: adoption signal the gate reads
    m.scope = "working"
    m.tags = tags or []
    # Age-accurate created_at so execute()'s (utcnow()-created_at).days matches age_days.
    m.created_at = created_at if created_at is not None else (utcnow() - timedelta(days=age_days))
    return m


async def _run_execute(phase, memories, *, reporter=None, report_id=None):
    """Drive ConsolidationPhase.execute() deterministically (LLM off, no graph).

    Patches GraphService (no edges → neural_metrics None for all). Returns the
    PhaseResult so tests can assert absolute promote rates via result.details +
    promote calls; ``soft_delete`` is a mock so a test can assert it never runs.
    """
    phase._fetch_working_memories = AsyncMock(return_value=memories)
    phase.memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=list)
    phase.memory_repo.soft_delete = AsyncMock(return_value=1)
    # LLM off → borderline memories stay put
    config = _make_config(provider="")
    budget = SleepBudget()
    with patch("services.sleep.consolidation.GraphService") as GS:
        GS.return_value.stats = AsyncMock(return_value={"total_edges": 0})
        return await phase.execute(
            config, "user-1", "ws-1", "ctx-1", budget, reporter=reporter, report_id=report_id
        )


class TestConsolidationPhase:
    """Test ConsolidationPhase execution."""

    @pytest.mark.asyncio
    async def test_no_working_memories(self, consolidation_phase):
        config = _make_config()
        budget = SleepBudget()

        consolidation_phase._fetch_working_memories = AsyncMock(return_value=[])

        result = await consolidation_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["message"] == "no_working_memories"


class TestAdoptionPromotionGate:
    """#1049: execute()-driven 2-population eval of the ADOPTION-gated promotion.

    Replaces the old tautological inline-formula tests (which re-implemented the
    boolean in the test and passed regardless of the real thresholds). These
    assert ABSOLUTE promote/delete rates via result.details + promote/delete call
    args, importing the real threshold constants (contract, not a copy).
    """

    @pytest.mark.asyncio
    async def test_rare_but_adopted_promotes_and_is_never_deleted(self, consolidation_phase):
        # Rare (zero surfacing) but ADOPTED — including an OLD one — must promote
        # via the adoption gate and must NEVER be deleted (hard assert).
        mems = [
            _make_working_memory(
                reference_count=ADOPTION_PROMOTE_MIN, access_count=0, importance=0.1, age_days=5
            ),
            _make_working_memory(
                reference_count=ADOPTION_PROMOTE_WITH_IMPORTANCE,
                access_count=0,
                importance=0.6,
                age_days=5,
            ),
            _make_working_memory(  # aged + adopted
                reference_count=1, access_count=0, importance=0.1, age_days=40
            ),
        ]
        result = await _run_execute(consolidation_phase, mems)

        assert result.details["rule_promoted"] == len(mems)
        assert result.details["rule_deleted"] == 0
        # #1985: one id-ordered statement for the whole rule pass.
        consolidation_phase.memory_repo.bulk_promote_to_persistent.assert_awaited_once_with(
            [m.id for m in mems]
        )
        consolidation_phase.memory_repo.soft_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_surfaced_but_ignored_does_not_promote(self, consolidation_phase):
        # High surfacing (access_count) but ZERO adoption, low importance, young.
        # Under the OLD access_count>=5 rule these promoted; under #1049 they must
        # NOT — surfacing alone no longer counts as "used".
        mems = [
            _make_working_memory(reference_count=0, access_count=10, importance=0.3, age_days=5),
            _make_working_memory(reference_count=0, access_count=50, importance=0.2, age_days=10),
        ]
        result = await _run_execute(consolidation_phase, mems)

        assert result.details["rule_promoted"] == 0
        assert result.details["rule_deleted"] == 0  # young → not deleted either
        consolidation_phase.memory_repo.bulk_promote_to_persistent.assert_not_called()


class TestPromotionBoundary:
    """#1049: pin the exact adoption cutoff using the imported constant."""

    @pytest.mark.asyncio
    async def test_adoption_at_min_promotes(self, consolidation_phase):
        mem = _make_working_memory(
            reference_count=ADOPTION_PROMOTE_MIN, access_count=0, importance=0.1, age_days=5
        )
        result = await _run_execute(consolidation_phase, [mem])
        assert result.details["rule_promoted"] == 1

    @pytest.mark.asyncio
    async def test_just_below_min_without_importance_does_not_promote(self, consolidation_phase):
        # reference_count one below the importance-agnostic min, importance under the
        # floor → no rule fires (boundary).
        mem = _make_working_memory(
            reference_count=ADOPTION_PROMOTE_MIN - 1, access_count=0, importance=0.1, age_days=5
        )
        result = await _run_execute(consolidation_phase, [mem])
        assert result.details["rule_promoted"] == 0


class TestNothingIsArchived:
    """#3021: a memory is kept until its owner or writer deletes it. An old,
    never-adopted, isolated working memory — what the archive rule used to
    take — is borderline now, and the report keys stay at 0."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("age_days", [30, 40, 400])
    async def test_old_unadopted_isolated_memory_is_kept(self, consolidation_phase, age_days):
        mem = _make_working_memory(
            reference_count=0, access_count=0, importance=0.1, age_days=age_days
        )
        reporter = MagicMock()
        reporter.add_action = AsyncMock()

        result = await _run_execute(
            consolidation_phase, [mem], reporter=reporter, report_id=uuid4()
        )

        consolidation_phase.memory_repo.soft_delete.assert_not_called()
        consolidation_phase.memory_repo.bulk_promote_to_persistent.assert_not_called()
        reporter.add_action.assert_not_awaited()
        assert result.details["borderline"] == 1
        assert result.details["rule_deleted"] == 0
        assert result.details["llm_archived"] == 0
        assert result.details["llm_archive_guarded"] == 0

    @pytest.mark.asyncio
    async def test_the_cutoff_setting_no_longer_exists(self, consolidation_phase, monkeypatch):
        """``CONSOLIDATION_ADOPTION_DELETE_CUTOFF`` set by an old deployment is
        ignored: nothing reads it."""
        monkeypatch.setenv("CONSOLIDATION_ADOPTION_DELETE_CUTOFF", "2000-01-01T00:00:00")
        mem = _make_working_memory(reference_count=0, importance=0.1, age_days=60)

        result = await _run_execute(consolidation_phase, [mem])

        consolidation_phase.memory_repo.soft_delete.assert_not_called()
        assert result.details["rule_deleted"] == 0

    def test_the_phase_has_no_archive_path(self):
        import services.sleep.consolidation as consolidation

        for name in ("_adoption_delete_cutoff", "_archival_eligible", "ARCHIVE_MIN_AGE_DAYS"):
            assert not hasattr(consolidation, name), name
        assert not hasattr(ConsolidationPhase, "_archive_tombstone")


class TestLLMJudgeParsing:
    """Parse contract of ``_llm_judge_batch`` — exercised through the REAL
    parser (#1233 replaced inline simulations that could silently drift
    from production)."""

    async def _parse(self, consolidation_phase, memory, decisions):
        resp = MagicMock()
        resp.parsed = {"decisions": decisions}
        resp.total_tokens = 10
        consolidation_phase.llm_service.complete_json = AsyncMock(return_value=resp)
        return await consolidation_phase._llm_judge_batch(
            [memory], "user-1", "ctx-1", "ws-1", SleepBudget(), _make_config()
        )

    @pytest.mark.asyncio
    async def test_valid_promote_response(self, consolidation_phase):
        mem = _make_working_memory()
        decisions = await self._parse(
            consolidation_phase,
            mem,
            [{"label": "A", "action": "promote", "reason": "durable knowledge"}],
        )
        assert decisions == {mem.id: "promote"}

    @pytest.mark.asyncio
    async def test_valid_keep_response(self, consolidation_phase):
        mem = _make_working_memory()
        decisions = await self._parse(
            consolidation_phase,
            mem,
            [{"label": "A", "action": "keep", "reason": "uncertain"}],
        )
        assert decisions == {mem.id: "keep"}

    @pytest.mark.asyncio
    async def test_archive_action_dropped_at_parse(self, consolidation_phase):
        """#1233: 'archive' is no longer offered to the judge — a response
        that uses it anyway is dropped at parse (#3021: nothing is archived)."""
        mem = _make_working_memory()
        decisions = await self._parse(
            consolidation_phase,
            mem,
            [{"label": "A", "action": "archive", "reason": "stale"}],
        )
        assert decisions == {}

    @pytest.mark.asyncio
    async def test_invalid_action_ignored(self, consolidation_phase):
        mem = _make_working_memory()
        decisions = await self._parse(
            consolidation_phase,
            mem,
            [{"label": "A", "action": "destroy", "reason": "bad action"}],
        )
        assert decisions == {}

    @pytest.mark.asyncio
    async def test_invalid_label_ignored(self, consolidation_phase):
        mem = _make_working_memory()
        decisions = await self._parse(
            consolidation_phase,
            mem,
            [{"label": "Z", "action": "promote", "reason": "hallucinated"}],
        )
        assert decisions == {}


class TestConsolidationPromptContract:
    """#1233: the judge prompt does not offer an 'archive' action (#3021:
    consolidation archives nothing)."""

    def test_prompt_no_longer_offers_archive(self):
        from services.sleep.prompts import (
            CONSOLIDATION_JUDGE_SYSTEM,
            CONSOLIDATION_JUDGE_USER,
        )

        assert '"archive"' not in CONSOLIDATION_JUDGE_USER
        assert '"promote" | "keep"' in CONSOLIDATION_JUDGE_USER
        assert "archive" not in CONSOLIDATION_JUDGE_SYSTEM.lower()


def _isolation_memory(*, importance, access_count, age_days, reference_count=0):
    """Build a working Memory with a concrete created_at so the age computed
    inside ConsolidationPhase.execute() matches `age_days` exactly.

    reference_count defaults to 0 so the adoption gate (#1049) stays inert and
    these tests isolate the NEURAL-metric promotion/deletion path.
    """
    m = MagicMock()
    m.id = uuid4()
    m.summary = "isolation test memory"
    m.type = "note"
    m.importance = importance
    m.access_count = access_count
    m.reference_count = reference_count
    m.scope = "working"
    m.created_at = utcnow() - timedelta(days=age_days)
    return m


def _assert_graph_service_isolation(mock_graph_service):
    """Pin that GraphService was constructed with the isolation identifiers.

    The #659 requirement is 3-level isolation: execute() must build
    ``GraphService(user_id, db, workspace_id, context_id)``. Without this
    assertion a regression to ``GraphService(user_id, db)`` — or swapped /
    dropped ws/ctx — would still pass the behavioral checks while silently
    violating the isolation contract (Copilot review on PR #838).
    """
    mock_graph_service.assert_called_once()
    args, kwargs = mock_graph_service.call_args
    # consolidation.py constructs it positionally:
    #   GraphService(user_id, self.db, workspace_id, context_id)
    passed_workspace = args[2] if len(args) > 2 else kwargs.get("workspace_id")
    passed_context = args[3] if len(args) > 3 else kwargs.get("context_id")
    assert passed_workspace == "ws", "GraphService must receive workspace_id (3-level isolation)"
    assert passed_context == "ctx", "GraphService must receive context_id (3-level isolation)"


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
            patch(
                "services.sleep.consolidation.GraphService", return_value=graph
            ) as mock_graph_service,
        ):
            phase = ConsolidationPhase(mock_db, mock_llm)
            phase.memory_repo = AsyncMock()
            phase._fetch_working_memories = AsyncMock(return_value=[mem])
            # LLM off → borderline memories are left untouched.
            result = await phase.execute(_make_config(provider=""), "u", "ws", "ctx", SleepBudget())

        _assert_graph_service_isolation(mock_graph_service)
        phase.memory_repo.soft_delete.assert_not_called()
        phase.memory_repo.bulk_promote_to_persistent.assert_not_called()
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
            patch(
                "services.sleep.consolidation.GraphService", return_value=graph
            ) as mock_graph_service,
        ):
            phase = ConsolidationPhase(mock_db, mock_llm)
            phase.memory_repo = AsyncMock()
            # #1985: the bulk statement returns the ids it promoted.
            phase.memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=lambda ids: ids)
            phase._fetch_working_memories = AsyncMock(return_value=[mem])
            result = await phase.execute(_make_config(provider=""), "u", "ws", "ctx", SleepBudget())

        _assert_graph_service_isolation(mock_graph_service)
        phase.memory_repo.bulk_promote_to_persistent.assert_awaited_once_with([mem.id])
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
            patch(
                "services.sleep.consolidation.GraphService", return_value=graph
            ) as mock_graph_service,
        ):
            phase = ConsolidationPhase(mock_db, mock_llm)
            phase.memory_repo = AsyncMock()
            phase._fetch_working_memories = AsyncMock(return_value=[mem])
            await phase.execute(_make_config(provider=""), "u", "ws", "ctx", SleepBudget())

        _assert_graph_service_isolation(mock_graph_service)
        graph.get_node_metrics.assert_awaited()
        assert graph.get_node_metrics.await_count == 1


class TestConsolidationJudgeSwitch:
    """#3021: the LLM judge runs only when ``sleep_consolidation_judge_enabled``
    is on and a Sleep LLM is configured; rule promotion runs either way. A
    judge verdict other than promote leaves the memory working."""

    @staticmethod
    async def _run(phase, memories, config, decisions=None):
        phase._fetch_working_memories = AsyncMock(return_value=memories)
        phase.memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=lambda ids: ids)
        phase.memory_repo.soft_delete = AsyncMock(return_value=1)
        phase._llm_judge_batch = AsyncMock(return_value=decisions or {})
        with patch("services.sleep.consolidation.GraphService") as GS:
            GS.return_value.stats = AsyncMock(return_value={"total_edges": 0})
            return await phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

    @pytest.mark.asyncio
    async def test_judge_off_runs_only_the_rule_pass(self, consolidation_phase):
        adopted = _make_working_memory(reference_count=ADOPTION_PROMOTE_MIN, importance=0.1)
        borderline = _make_working_memory(reference_count=0, importance=0.1, age_days=40)

        result = await self._run(
            consolidation_phase, [adopted, borderline], _make_config(judge=False)
        )

        consolidation_phase._llm_judge_batch.assert_not_awaited()
        consolidation_phase.memory_repo.bulk_promote_to_persistent.assert_awaited_once_with(
            [adopted.id]
        )
        assert result.details["rule_promoted"] == 1
        assert result.details["borderline"] == 1
        assert result.llm_calls_used == 0

    @pytest.mark.asyncio
    async def test_judge_on_without_a_provider_does_not_run(self, consolidation_phase):
        mem = _make_working_memory(reference_count=0, importance=0.1)

        await self._run(consolidation_phase, [mem], _make_config(provider="", judge=True))

        consolidation_phase._llm_judge_batch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_judge_on_promotes_its_picks(self, consolidation_phase):
        mem = _make_working_memory(reference_count=0, importance=0.1)

        result = await self._run(
            consolidation_phase, [mem], _make_config(judge=True), {mem.id: "promote"}
        )

        consolidation_phase._llm_judge_batch.assert_awaited_once()
        assert result.details["llm_promoted"] == 1

    @pytest.mark.asyncio
    async def test_an_archive_verdict_changes_nothing(self, consolidation_phase):
        """Should a decision source ever answer "archive" again, the memory
        stays as it is and nothing is counted."""
        mem = _make_working_memory(reference_count=0, importance=0.1, age_days=400)

        result = await self._run(
            consolidation_phase, [mem], _make_config(judge=True), {mem.id: "archive"}
        )

        consolidation_phase.memory_repo.soft_delete.assert_not_called()
        consolidation_phase.memory_repo.bulk_promote_to_persistent.assert_not_called()
        assert result.details["llm_promoted"] == 0
        assert result.details["llm_archived"] == 0
        assert result.details["llm_archive_guarded"] == 0

    @pytest.mark.asyncio
    async def test_the_config_default_keeps_the_judge_off(self, consolidation_phase):
        from neural.config import NeuralMemoryConfig

        mem = _make_working_memory(reference_count=0, importance=0.1)
        config = NeuralMemoryConfig(sleep_llm_provider="openai", sleep_llm_model="m")

        await self._run(consolidation_phase, [mem], config)

        consolidation_phase._llm_judge_batch.assert_not_awaited()


class TestOpenMemos:
    """#2584 / #3021: an open review memo (``status:open``) is kept like every
    other memory, and adoption still promotes it."""

    OPEN = ["tech-debt", "status:open", "source:review", "area:sleep"]

    @pytest.mark.asyncio
    async def test_open_memo_is_kept(self, consolidation_phase):
        memo = _make_working_memory(reference_count=0, importance=0.1, age_days=40, tags=self.OPEN)

        result = await _run_execute(consolidation_phase, [memo])

        consolidation_phase.memory_repo.soft_delete.assert_not_called()
        assert result.details["borderline"] == 1

    @pytest.mark.asyncio
    async def test_open_memo_can_still_be_promoted(self, consolidation_phase):
        memo = _make_working_memory(
            reference_count=ADOPTION_PROMOTE_MIN, importance=0.1, age_days=40, tags=self.OPEN
        )

        result = await _run_execute(consolidation_phase, [memo])

        assert result.details["rule_promoted"] == 1
