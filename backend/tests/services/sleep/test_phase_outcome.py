"""#3019 point 5: every Sleep phase's details say what it considered and why.

Each phase that ran carries ``candidates_considered``, ``no_action_reasons``
(non-zero counts only) and ``budget_cut`` on every exit path; the reporter
stores the two retention results it used to drop inside
``merge_retention_result``.
"""

from __future__ import annotations

import itertools
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.reporter import PhaseResult, SleepBudget, SleepReporter, phase_outcome
from utils.datetime import utcnow

KEYS = {"candidates_considered", "no_action_reasons", "budget_cut"}


def _outcome(details: dict) -> dict:
    return {key: details[key] for key in KEYS}


def _llm_response(parsed: dict) -> MagicMock:
    resp = MagicMock()
    resp.parsed = parsed
    resp.total_tokens = 10
    resp.input_tokens = 6
    resp.output_tokens = 4
    resp.cached_input_tokens = 0
    resp.cache_write_tokens = 0
    resp.provider = "openai"
    resp.model = "m"
    resp.tokenizer_version = None
    return resp


def _memory(**attrs) -> MagicMock:
    m = MagicMock()
    m.id = attrs.pop("id", None) or uuid4()
    m.summary = "s"
    m.type = "note"
    m.importance = 0.5
    m.access_count = 0
    m.reference_count = 0
    m.scope = "working"
    m.tags = []
    m.created_at = utcnow() - timedelta(days=1)
    m.updated_at = None
    m.source_type = None
    for key, value in attrs.items():
        setattr(m, key, value)
    return m


class TestPhaseOutcomeHelper:
    def test_defaults(self):
        assert phase_outcome(0) == {
            "candidates_considered": 0,
            "no_action_reasons": {},
            "budget_cut": False,
        }

    def test_zero_counts_are_dropped(self):
        assert phase_outcome(4, {"a": 0, "b": 2}, budget_cut=True) == {
            "candidates_considered": 4,
            "no_action_reasons": {"b": 2},
            "budget_cut": True,
        }


class TestReporterStoresRetentionResults:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "order",
        list(
            itertools.permutations(["merge_retention", "forget_retention", "measurement_retention"])
        ),
    )
    async def test_nested_inside_merge_retention_whatever_the_order(self, order):
        db = AsyncMock()
        db.add = MagicMock()
        report = SimpleNamespace(
            id=uuid4(),
            source="sleep",
            context_id=None,
            error_message=None,
            embedding_provider=None,
            edge_discovery_result=None,
            merge_retention_result=None,
        )
        purged = {"merge_retention": 1, "forget_retention": 2, "measurement_retention": 3}
        results = [
            PhaseResult(phase_name=name, details={"purged": purged[name], **phase_outcome(0)})
            for name in order
        ]

        await SleepReporter(db).complete_report(report, results)

        blob = report.merge_retention_result
        assert blob["details"]["purged"] == 1
        assert blob["forget_retention"]["details"]["purged"] == 2
        assert blob["measurement_retention"]["details"]["purged"] == 3

    @pytest.mark.asyncio
    async def test_retention_without_merge_retention_still_stored(self):
        db = AsyncMock()
        db.add = MagicMock()
        report = SimpleNamespace(
            id=uuid4(),
            source="sleep",
            context_id=None,
            error_message=None,
            embedding_provider=None,
            merge_retention_result=None,
        )
        results = [PhaseResult(phase_name="forget_retention", skipped=True, skip_reason="x")]

        await SleepReporter(db).complete_report(report, results)

        assert report.merge_retention_result["forget_retention"]["skip_reason"] == "x"


# ---------------------------------------------------------------------------
# edge_discovery
# ---------------------------------------------------------------------------


@pytest.fixture
def edge_phase():
    from services.sleep.edge_discovery import EdgeDiscoveryPhase

    with (
        patch("services.sleep.edge_discovery.NeuralEdgeRepository"),
        patch("services.sleep.edge_discovery.EmbeddingService"),
    ):
        phase = EdgeDiscoveryPhase(AsyncMock(), AsyncMock())
    phase.edge_repo = AsyncMock()
    phase.embedding_service = AsyncMock()
    return phase


def _edge_config(provider="openai"):
    config = MagicMock()
    config.sleep_edge_discovery_enabled = True
    config.sleep_edge_discovery_sample_size = 10
    config.sleep_llm_provider = provider
    config.sleep_llm_model = "m"
    return config


class TestEdgeDiscoveryOutcome:
    @pytest.mark.asyncio
    async def test_nothing_sampled(self, edge_phase):
        edge_phase._sample_memories = AsyncMock(return_value=[])
        result = await edge_phase.execute(_edge_config(), "u", None, None, SleepBudget())
        assert result.details["message"] == "no_memories_to_sample"
        assert _outcome(result.details) == phase_outcome(0)

    @pytest.mark.asyncio
    async def test_no_candidates_counts_rows_without_neighbour_and_failed_searches(
        self, edge_phase
    ):
        a, b, c = _memory(), _memory(), _memory()
        edge_phase._sample_memories = AsyncMock(return_value=[a, b, c])
        edge_phase.embedding_service.embed_with_usage = AsyncMock(
            side_effect=[([0.1], 1), RuntimeError("down"), ([0.1], 1)]
        )
        hits = [[{"id": str(a.id), "score": 0.99}], []]
        with patch(
            "services.sleep.edge_discovery.search_memories_qdrant",
            AsyncMock(side_effect=hits),
        ):
            result = await edge_phase.execute(_edge_config(), "u", None, None, SleepBudget())
        assert result.details["message"] == "no_edge_candidates"
        assert _outcome(result.details) == phase_outcome(
            3, {"no_neighbour_in_range": 2, "search_failed": 1}
        )

    @pytest.mark.asyncio
    async def test_all_connected(self, edge_phase):
        a = _memory()
        edge_phase._sample_memories = AsyncMock(return_value=[a])
        edge_phase._find_candidates = AsyncMock(
            return_value=[(a.id, uuid4(), 0.7), (a.id, uuid4(), 0.7)]
        )
        edge_phase._filter_existing_edges = AsyncMock(return_value=[])
        result = await edge_phase.execute(_edge_config(), "u", None, None, SleepBudget())
        assert result.details["message"] == "all_candidates_already_connected"
        assert _outcome(result.details) == phase_outcome(1, {"already_connected": 2})

    @pytest.mark.asyncio
    async def test_judged_rejected_failed_and_cut_for_budget(self, edge_phase):
        from services.sleep.edge_discovery import BATCH_SIZE, BatchStats

        a = _memory()
        pairs = [(a.id, uuid4(), 0.7) for _ in range(BATCH_SIZE * 3 + 1)]
        edge_phase._sample_memories = AsyncMock(return_value=[a])
        edge_phase._find_candidates = AsyncMock(return_value=pairs + [(a.id, uuid4(), 0.7)])
        edge_phase._filter_existing_edges = AsyncMock(return_value=pairs)
        stats = iter([BatchStats(rejected=2, pairs_outside_sample=3), BatchStats(failures=1)])

        async def judge(batch, memory_map, user_id, context_id, workspace_id, budget, config):
            budget.consume(llm_calls=1)
            return [], next(stats)

        edge_phase._llm_judge_batch = judge
        result = await edge_phase.execute(
            _edge_config(), "u", None, None, SleepBudget(max_llm_calls=2)
        )
        assert _outcome(result.details) == phase_outcome(
            1,
            {
                "already_connected": 1,
                "judge_rejected": 2,
                "other_side_outside_sample": 3,
                "judge_failed": BATCH_SIZE,
                "not_judged_budget": BATCH_SIZE + 1,
            },
            budget_cut=True,
        )

    @pytest.mark.asyncio
    async def test_write_failures_counted(self, edge_phase):
        a = _memory()
        pairs = [(a.id, uuid4(), 0.7), (a.id, uuid4(), 0.7)]
        edge_phase._sample_memories = AsyncMock(return_value=[a])
        edge_phase._find_candidates = AsyncMock(return_value=pairs)
        edge_phase._filter_existing_edges = AsyncMock(return_value=pairs)
        edge_phase.edge_repo.create_or_update_edge = AsyncMock(
            side_effect=[None, RuntimeError("x")]
        )
        result = await edge_phase.execute(_edge_config(provider=""), "u", None, None, SleepBudget())
        assert result.details["edges_created"] == 1
        assert _outcome(result.details) == phase_outcome(1, {"write_failed": 1})

    @pytest.mark.asyncio
    async def test_judge_batch_counts_pairs_outside_the_sample(self, edge_phase):
        a, b = _memory(), _memory()
        outside = uuid4()
        edge_phase.llm_service.complete_json = AsyncMock(return_value=_llm_response({"edges": []}))
        _, stats = await edge_phase._llm_judge_batch(
            [(a.id, b.id, 0.7), (a.id, outside, 0.7)],
            {a.id: a, b.id: b},
            "u",
            None,
            None,
            SleepBudget(),
            _edge_config(),
        )
        assert stats.pairs_outside_sample == 1


# ---------------------------------------------------------------------------
# related_index
# ---------------------------------------------------------------------------


class TestRelatedIndexOutcome:
    @pytest.mark.asyncio
    async def test_endpoints_unavailable_counted(self, monkeypatch):
        from services.sleep.related_index import RelatedIndexPhase

        monkeypatch.setenv("SLEEP_RELATED_INDEX_ENABLED", "true")
        db = AsyncMock()
        db.add = MagicMock()
        src = _memory(context_id=uuid4(), workspace_id=uuid4(), content="c", details=None)
        edges = [
            SimpleNamespace(id=1, src_id=src.id, dst_id=uuid4()),
            SimpleNamespace(id=2, src_id=src.id, dst_id=src.id),
        ]
        rows = MagicMock()
        rows.all.return_value = [(e,) for e in edges]
        db.execute = AsyncMock(return_value=rows)
        scalars = MagicMock()
        scalars.all.return_value = [src]
        db.scalars = AsyncMock(return_value=scalars)
        result = await RelatedIndexPhase(db).execute(
            None, "u", str(uuid4()), str(uuid4()), SleepBudget(), report_id=uuid4()
        )
        assert result.details["indexed"] == 0
        assert _outcome(result.details) == phase_outcome(2, {"endpoint_unavailable": 2})


# ---------------------------------------------------------------------------
# retention phases
# ---------------------------------------------------------------------------


class TestRetentionOutcome:
    @pytest.mark.asyncio
    async def test_merge_retention_counts_tombstones_past_the_cutoff(self):
        from services.sleep.merge_retention import MergeRetentionPhase

        db = AsyncMock()
        select_result = MagicMock()
        select_result.all.return_value = [(uuid4(),), (uuid4(),)]
        db.execute = AsyncMock(side_effect=[select_result, MagicMock()])
        config = MagicMock()
        config.sleep_merge_retention_days = 30
        result = await MergeRetentionPhase(db).execute(config, "u", None, None, SleepBudget())
        assert _outcome(result.details) == phase_outcome(2)

    @pytest.mark.asyncio
    async def test_forget_retention_nothing_past_the_cutoff(self):
        from services.sleep.forget_retention import ForgetRetentionPhase

        db = AsyncMock()
        select_result = MagicMock()
        select_result.all.return_value = []
        db.execute = AsyncMock(return_value=select_result)
        config = MagicMock()
        config.sleep_forget_retention_days = 30
        result = await ForgetRetentionPhase(db).execute(config, "u", None, None, SleepBudget())
        assert _outcome(result.details) == phase_outcome(0)

    @pytest.mark.asyncio
    async def test_measurement_retention_counts_observations_and_keeps_the_action(self):
        from services.sleep.measurement_retention import MeasurementRetentionPhase

        db = AsyncMock()
        deleted = MagicMock()
        deleted.rowcount = 4
        db.execute = AsyncMock(return_value=deleted)
        reporter = MagicMock()
        reporter.add_action = AsyncMock()
        config = MagicMock()
        config.sleep_measurement_retention_days = 30
        result = await MeasurementRetentionPhase(db).execute(
            config, "u", None, str(uuid4()), SleepBudget(), reporter=reporter, report_id=uuid4()
        )
        assert _outcome(result.details) == phase_outcome(4)
        # The audit action keeps its counts-only shape.
        assert set(reporter.add_action.await_args.kwargs["details"]) == {
            "purged",
            "retention_days",
            "cutoff",
        }


# ---------------------------------------------------------------------------
# importance_reeval
# ---------------------------------------------------------------------------


@pytest.fixture
def reeval_phase():
    from services.sleep.importance_reeval import ImportanceReevalPhase

    phase = ImportanceReevalPhase(AsyncMock(), AsyncMock())
    phase._chunk_cascade = AsyncMock(return_value=False)
    return phase


def _reeval_config():
    config = MagicMock()
    config.sleep_importance_reeval_enabled = True
    config.importance_ema_alpha = 0.3
    config.sleep_llm_provider = "openai"
    config.sleep_llm_model = "m"
    return config


def _scores_for(batch_rows, how_many):
    labels = "ABCDEFGHIJ"
    return _llm_response(
        {"scores": [{"label": labels[i], "importance": 0.6} for i in range(how_many)]}
    )


class TestImportanceReevalOutcome:
    @pytest.mark.asyncio
    async def test_nothing_due(self, reeval_phase):
        reeval_phase._fetch_candidates = AsyncMock(return_value=[])
        result = await reeval_phase.execute(_reeval_config(), "u", None, None, SleepBudget())
        assert result.details["message"] == "no_stale_memories"
        assert _outcome(result.details) == phase_outcome(0)

    @pytest.mark.asyncio
    async def test_no_score_returned_and_changed_since_fetch(self, reeval_phase):
        rows = [_memory(), _memory(), _memory()]
        reeval_phase._fetch_candidates = AsyncMock(return_value=rows)
        reeval_phase._evaluate_batch = AsyncMock(return_value={rows[0].id: 0.6, rows[1].id: 0.6})
        refused = MagicMock()
        refused.rowcount = 0
        written = MagicMock()
        written.rowcount = 1
        # The batch's re-evaluation marker, then the two importance writes.
        reeval_phase.db.execute = AsyncMock(side_effect=[MagicMock(), written, refused])
        with patch("services.sleep.importance_reeval.update_memory_payload_in_qdrant", AsyncMock()):
            result = await reeval_phase.execute(_reeval_config(), "u", None, None, SleepBudget())
        assert _outcome(result.details) == phase_outcome(
            3, {"no_score_returned": 1, "changed_since_fetch": 1}
        )

    @pytest.mark.asyncio
    async def test_failed_batch_counts_its_rows(self, reeval_phase):
        rows = [_memory(), _memory()]
        reeval_phase._fetch_candidates = AsyncMock(return_value=rows)
        reeval_phase.llm_service.complete_json = AsyncMock(side_effect=RuntimeError("down"))
        result = await reeval_phase.execute(_reeval_config(), "u", None, None, SleepBudget())
        assert _outcome(result.details) == phase_outcome(2, {"judge_failed": 2})


# ---------------------------------------------------------------------------
# consolidation
# ---------------------------------------------------------------------------


@pytest.fixture
def consolidation_phase():
    from services.sleep.consolidation import ConsolidationPhase

    with (
        patch("services.sleep.consolidation.MemoryRepository"),
        patch("services.sleep.consolidation.GraphService"),
    ):
        phase = ConsolidationPhase(AsyncMock(), AsyncMock())
    phase.memory_repo = AsyncMock()
    phase.memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=list)
    return phase


def _consolidation_config(judge=True):
    config = MagicMock()
    config.sleep_llm_provider = "openai"
    config.sleep_llm_model = "m"
    config.sleep_consolidation_judge_enabled = judge
    return config


async def _consolidate(phase, rows, *, judge=True, budget=None):
    phase._fetch_working_memories = AsyncMock(return_value=rows)
    with patch("services.sleep.consolidation.GraphService") as graph:
        graph.return_value.stats = AsyncMock(return_value={"total_edges": 0})
        return await phase.execute(
            _consolidation_config(judge), "u", None, None, budget or SleepBudget()
        )


class TestConsolidationOutcome:
    @pytest.mark.asyncio
    async def test_no_working_memories(self, consolidation_phase):
        result = await _consolidate(consolidation_phase, [])
        assert result.details == {"message": "no_working_memories", **phase_outcome(0)}

    @pytest.mark.asyncio
    async def test_judge_off_counts_the_borderline_rows(self, consolidation_phase):
        result = await _consolidate(consolidation_phase, [_memory(), _memory()], judge=False)
        assert _outcome(result.details) == phase_outcome(2, {"judge_off": 2})

    @pytest.mark.asyncio
    async def test_kept_and_cut_for_budget(self, consolidation_phase):
        from services.sleep.consolidation import BATCH_SIZE

        rows = [_memory() for _ in range(BATCH_SIZE + 2)]
        consolidation_phase.llm_service.complete_json = AsyncMock(
            return_value=_llm_response(
                {
                    "decisions": [
                        {"label": "A", "action": "promote"},
                        {"label": "B", "action": "keep"},
                    ]
                }
            )
        )
        result = await _consolidate(consolidation_phase, rows, budget=SleepBudget(max_llm_calls=1))
        assert result.details["llm_pass_cut_for_budget"] is True
        assert _outcome(result.details) == phase_outcome(
            BATCH_SIZE + 2,
            {"kept_by_judge": BATCH_SIZE - 1, "not_judged_budget": 2},
            budget_cut=True,
        )

    @pytest.mark.asyncio
    async def test_failed_batch_and_refused_promotion(self, consolidation_phase):
        promoted = _memory(reference_count=5)
        borderline = [_memory(), _memory()]
        consolidation_phase.memory_repo.bulk_promote_to_persistent = AsyncMock(return_value=[])
        consolidation_phase.llm_service.complete_json = AsyncMock(side_effect=RuntimeError("x"))
        result = await _consolidate(consolidation_phase, [promoted, *borderline])
        assert _outcome(result.details) == phase_outcome(
            3, {"judge_failed": 2, "changed_since_fetch": 1}
        )


# ---------------------------------------------------------------------------
# dedup_merge
# ---------------------------------------------------------------------------


@pytest.fixture
def dedup_phase():
    from services.sleep.dedup_merge import DedupMergePhase

    with (
        patch("services.sleep.dedup_merge.NeuralEdgeRepository"),
        patch("services.sleep.dedup_merge.EmbeddingService"),
    ):
        phase = DedupMergePhase(AsyncMock(), AsyncMock())
    phase.edge_repo = AsyncMock()
    phase.embedding_service = AsyncMock()
    phase._count_pinned = AsyncMock(return_value=0)
    return phase


def _dedup_config(provider="openai"):
    config = MagicMock()
    config.sleep_dedup_enabled = True
    config.sleep_dedup_similarity_threshold = 0.92
    config.sleep_dedup_supersede_enabled = False
    config.sleep_llm_provider = provider
    config.sleep_llm_model = "m"
    config.sleep_max_memories_per_run = 500
    config.sleep_archive_exempt_tags = []
    return config


class TestDedupOutcome:
    @pytest.mark.asyncio
    async def test_not_enough_memories(self, dedup_phase):
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[_memory()])
        result = await dedup_phase.execute(_dedup_config(), "u", None, None, SleepBudget())
        assert result.details == {"message": "not_enough_memories", "count": 1, **phase_outcome(1)}

    @pytest.mark.asyncio
    async def test_no_pairs_counts_rows_and_hits(self, dedup_phase):
        a, b, c = _memory(), _memory(), _memory()
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[a, b, c])
        dedup_phase.embedding_service.embed_with_usage = AsyncMock(
            side_effect=[([0.1], 1), RuntimeError("down"), ([0.1], 1)]
        )
        hits = [[{"id": str(a.id), "score": 1.0}, {"id": str(uuid4()), "score": 0.95}], []]
        with patch(
            "services.sleep.dedup_merge.search_memories_qdrant", AsyncMock(side_effect=hits)
        ):
            result = await dedup_phase.execute(_dedup_config(), "u", None, None, SleepBudget())
        assert result.details["message"] == "no_duplicate_candidates"
        assert _outcome(result.details) == phase_outcome(
            3,
            {"no_similar_neighbour": 1, "search_failed": 1, "neighbour_outside_window": 1},
        )

    @pytest.mark.asyncio
    async def test_kept_both_and_cut_for_budget(self, dedup_phase):
        a, b, c, d = _memory(), _memory(), _memory(), _memory()
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[a, b, c, d])
        dedup_phase._find_similar_pairs = AsyncMock(
            return_value=[(a.id, b.id, 0.95), (c.id, d.id, 0.95)]
        )
        dedup_phase.llm_service.complete_json = AsyncMock(
            return_value=_llm_response({"judgments": []})
        )
        result = await dedup_phase.execute(
            _dedup_config(), "u", None, None, SleepBudget(max_llm_calls=1)
        )
        assert _outcome(result.details) == phase_outcome(
            4, {"judge_kept_both": 1, "not_judged_budget": 1}, budget_cut=True
        )

    @pytest.mark.asyncio
    async def test_failed_judge_and_unverifiable_pick(self, dedup_phase):
        a, b, c, d, e = _memory(), _memory(), _memory(), _memory(), _memory()
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[a, b, c, d, e])
        dedup_phase._find_similar_pairs = AsyncMock(
            return_value=[(a.id, b.id, 0.95), (b.id, c.id, 0.95), (d.id, e.id, 0.95)]
        )

        def respond(**kwargs):
            # The three-row cluster: the judge picks the pair that has no
            # candidate score (no stored vectors, so it cannot be verified).
            unscored = re.search(r"\((\w), (\w)\): similarity=0\.000", kwargs["prompt"])
            if unscored is None:
                raise RuntimeError("down")
            first, second = unscored.groups()
            return _llm_response(
                {"judgments": [{"pair": [first, second], "verdict": "merge", "winner": first}]}
            )

        dedup_phase.llm_service.complete_json = AsyncMock(side_effect=respond)
        result = await dedup_phase.execute(_dedup_config(), "u", None, None, SleepBudget())
        assert _outcome(result.details) == phase_outcome(5, {"judge_failed": 1, "unverifiable": 1})
