"""Coverage tests for Sleep Maintenance Phase 3: Importance Re-evaluation.

Issue #103. Complements ``test_importance_reeval.py`` by exercising the
uncovered branches of ``services.sleep.importance_reeval``:

- ``_fetch_candidates`` SQL filtering (staleness, importance window,
  soft-delete, workspace/context scoping) against a real ``db_session``.
- The full ``execute`` happy path (EMA smoothing → PostgreSQL update →
  Qdrant payload update → reporter actions), with Qdrant + the LLM mocked.
- Budget exhaustion / batching control flow.
- ``_evaluate_batch`` parsing: valid scores, label validation, out-of-range
  importance rejection, non-numeric rejection, and the LLM-failure fallback.
- The Qdrant-update-failure warning branch.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from models.auth import Context, Workspace
from models.memory import Memory
from services.llm_service import LLMResponse
from services.sleep.importance_reeval import (
    BATCH_SIZE,
    IMPORTANCE_MAX,
    IMPORTANCE_MIN,
    STALENESS_DAYS,
    ImportanceReevalPhase,
)
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(enabled=True, alpha=0.3, provider="openai", model="gpt-5-nano"):
    config = MagicMock()
    config.sleep_importance_reeval_enabled = enabled
    config.importance_ema_alpha = alpha
    config.sleep_llm_provider = provider
    config.sleep_llm_model = model
    return config


def _make_llm_response(parsed: dict, total_tokens: int = 42) -> LLMResponse:
    """Build a realistic LLMResponse the phase can consume."""
    return LLMResponse(
        parsed=parsed,
        total_tokens=total_tokens,
        input_tokens=30,
        output_tokens=12,
        cached_input_tokens=0,
        provider="openai",
        model="gpt-5-nano",
        tokenizer_version="o200k_base",
    )


def _mem_kwargs(
    *,
    user_id: str,
    importance: float = 0.5,
    updated_delta_days: int = STALENESS_DAYS + 1,
    deleted: bool = False,
    workspace_id=None,
    context_id=None,
    summary: str = "a memory summary",
):
    """Construct kwargs for a real Memory row with controllable staleness."""
    updated_at = utcnow() - timedelta(days=updated_delta_days)
    return {
        "id": uuid4(),
        "user_id": user_id,
        "summary": summary,
        "content": "full content body",
        "type": "note",
        "importance": importance,
        "access_count": 3,
        "scope": "working",
        "client": "pytest",
        "updated_at": updated_at,
        "deleted_at": updated_at if deleted else None,
        "workspace_id": workspace_id,
        "context_id": context_id,
    }


@pytest.fixture
def reeval_phase_mockdb():
    """Phase wired to fully-mocked DB + LLM (for control-flow tests)."""
    db = AsyncMock()
    llm = AsyncMock()
    with patch(
        "services.sleep.importance_reeval.update_memory_payload_in_qdrant",
        new_callable=AsyncMock,
    ):
        phase = ImportanceReevalPhase(db, llm)
    # ``execute`` lazily initialises these before any ``_evaluate_batch``
    # call; tests that invoke ``_evaluate_batch`` directly need them set.
    phase._tokens_used = 0
    phase._llm_breakdown = None
    return phase


def _make_mock_memory(memory_id=None, importance=0.5, summary="s", access_count=2):
    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = summary
    m.type = "note"
    m.importance = importance
    m.access_count = access_count
    m.scope = "working"
    return m


# ---------------------------------------------------------------------------
# _fetch_candidates — real DB filtering
# ---------------------------------------------------------------------------


class TestFetchCandidates:
    """Real-DB coverage of the candidate-selection WHERE clauses."""

    async def test_returns_stale_midrange_memory(self, db_session):
        """A stale, mid-importance, non-deleted memory is a candidate."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=0.5)
        db_session.add(Memory(**kw))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, None, None)

        assert [c.id for c in candidates] == [kw["id"]]

    async def test_excludes_fresh_memory(self, db_session):
        """A recently-updated memory is below the staleness cutoff."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=0.5, updated_delta_days=1)
        db_session.add(Memory(**kw))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, None, None)

        assert candidates == []

    async def test_excludes_importance_below_min(self, db_session):
        """importance < IMPORTANCE_MIN is well-calibrated, not a candidate."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=IMPORTANCE_MIN - 0.05)
        db_session.add(Memory(**kw))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, None, None)

        assert candidates == []

    async def test_excludes_importance_above_max(self, db_session):
        """importance > IMPORTANCE_MAX is well-calibrated, not a candidate."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=IMPORTANCE_MAX + 0.05)
        db_session.add(Memory(**kw))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, None, None)

        assert candidates == []

    async def test_boundary_importance_min_and_max_included(self, db_session):
        """Both inclusive boundaries (>= MIN, <= MAX) qualify."""
        user_id = f"u-{uuid4()}"
        kw_min = _mem_kwargs(user_id=user_id, importance=IMPORTANCE_MIN)
        kw_max = _mem_kwargs(user_id=user_id, importance=IMPORTANCE_MAX)
        db_session.add(Memory(**kw_min))
        db_session.add(Memory(**kw_max))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, None, None)

        assert {c.id for c in candidates} == {kw_min["id"], kw_max["id"]}

    async def test_excludes_soft_deleted(self, db_session):
        """deleted_at IS NOT NULL excludes the row."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, deleted=True)
        db_session.add(Memory(**kw))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, None, None)

        assert candidates == []

    async def test_excludes_other_user(self, db_session):
        """user_id filter isolates owners."""
        user_id = f"u-{uuid4()}"
        other = f"u-{uuid4()}"
        db_session.add(Memory(**_mem_kwargs(user_id=other)))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, None, None)

        assert candidates == []

    async def test_workspace_filter(self, db_session):
        """Passing workspace_id narrows to that workspace only."""
        user_id = f"u-{uuid4()}"
        ws = uuid4()
        in_ws = _mem_kwargs(user_id=user_id, workspace_id=ws)
        out_ws = _mem_kwargs(user_id=user_id, workspace_id=uuid4())
        db_session.add(Memory(**in_ws))
        db_session.add(Memory(**out_ws))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, str(ws), None)

        assert [c.id for c in candidates] == [in_ws["id"]]

    async def test_context_filter(self, db_session):
        """Passing context_id narrows to that context only."""
        user_id = f"u-{uuid4()}"
        ws_id = uuid4()
        ctx = uuid4()
        other_ctx = uuid4()
        # Real workspace + contexts so the memories.context_id FK is satisfied.
        db_session.add(Workspace(id=ws_id, name="ws", owner_user_id=user_id))
        await db_session.flush()
        db_session.add(Context(id=ctx, workspace_id=ws_id, name="ctx-a"))
        db_session.add(Context(id=other_ctx, workspace_id=ws_id, name="ctx-b"))
        await db_session.flush()

        in_ctx = _mem_kwargs(user_id=user_id, context_id=ctx)
        out_ctx = _mem_kwargs(user_id=user_id, context_id=other_ctx)
        db_session.add(Memory(**in_ctx))
        db_session.add(Memory(**out_ctx))
        await db_session.flush()

        phase = ImportanceReevalPhase(db_session, AsyncMock())
        candidates = await phase._fetch_candidates(user_id, None, str(ctx))

        assert [c.id for c in candidates] == [in_ctx["id"]]


# ---------------------------------------------------------------------------
# execute — full integration against real DB
# ---------------------------------------------------------------------------


class TestExecuteIntegration:
    """End-to-end ``execute`` flow with mocked LLM + Qdrant, real DB."""

    async def test_disabled_short_circuits(self, db_session):
        """Disabled config returns a skipped result without touching the DB."""
        phase = ImportanceReevalPhase(db_session, AsyncMock())
        result = await phase.execute(_make_config(enabled=False), "u", None, None, SleepBudget())
        assert result.skipped is True
        assert result.skip_reason == "importance_reeval_disabled"

    async def test_no_candidates_returns_message(self, db_session):
        """No stale memories → details.message == no_stale_memories."""
        phase = ImportanceReevalPhase(db_session, AsyncMock())
        result = await phase.execute(_make_config(), f"u-{uuid4()}", None, None, SleepBudget())
        assert result.details == {"message": "no_stale_memories"}
        assert result.memories_processed == 0

    async def test_happy_path_updates_importance_and_qdrant(self, db_session):
        """A candidate is smoothed via EMA, persisted, and pushed to Qdrant."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=0.5)
        db_session.add(Memory(**kw))
        await db_session.flush()

        llm = AsyncMock()
        # The phase shuffles labels but always assigns 'A' first; with one
        # memory the only label is 'A'. LLM says importance 0.8.
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response({"scores": [{"label": "A", "importance": 0.8}]})
        )

        qdrant = AsyncMock()
        reporter = AsyncMock()
        report_id = uuid4()
        budget = SleepBudget()

        with patch("services.sleep.importance_reeval.update_memory_payload_in_qdrant", qdrant):
            phase = ImportanceReevalPhase(db_session, llm, collection_name="custom_coll")
            result = await phase.execute(
                _make_config(alpha=0.3),
                user_id,
                None,
                None,
                budget,
                reporter=reporter,
                report_id=report_id,
            )

        # EMA: 0.3*0.8 + 0.7*0.5 = 0.59
        expected = 0.3 * 0.8 + 0.7 * 0.5
        row = (await db_session.execute(select(Memory).where(Memory.id == kw["id"]))).scalar_one()
        assert row.importance == pytest.approx(expected)

        assert result.memories_processed == 1
        assert kw["id"] in result.changed_memory_ids
        assert result.details["updated"] == 1
        assert result.details["candidates"] == 1
        assert result.llm_calls_used == 1
        assert result.tokens_used == 42
        # #471 breakdown attached.
        assert len(result.llm_breakdown) == 1
        assert result.llm_breakdown[0].calls == 1

        # Qdrant invoked with the smoothed value + custom collection.
        qdrant.assert_awaited_once()
        _, qkwargs = qdrant.call_args
        assert qkwargs["collection_name"] == "custom_coll"
        assert qkwargs["payload_updates"]["importance"] == pytest.approx(expected)

        # Reporter action recorded with the right details.
        reporter.add_action.assert_awaited_once()
        _, akwargs = reporter.add_action.call_args
        assert akwargs["action_type"] == "update_importance"
        assert akwargs["details"]["llm_score"] == 0.8
        assert akwargs["details"]["old_importance"] == 0.5

    async def test_default_collection_name_used_when_none(self, db_session):
        """collection_name=None falls back to 'kagura_memories' for Qdrant."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=0.4)
        db_session.add(Memory(**kw))
        await db_session.flush()

        llm = AsyncMock()
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response({"scores": [{"label": "A", "importance": 0.6}]})
        )
        qdrant = AsyncMock()

        with patch("services.sleep.importance_reeval.update_memory_payload_in_qdrant", qdrant):
            phase = ImportanceReevalPhase(db_session, llm)  # no collection
            await phase.execute(_make_config(), user_id, None, None, SleepBudget())

        _, qkwargs = qdrant.call_args
        assert qkwargs["collection_name"] == "kagura_memories"

    async def test_qdrant_failure_is_swallowed_and_db_still_updated(self, db_session):
        """A Qdrant error is logged but does not abort the importance update."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=0.5)
        db_session.add(Memory(**kw))
        await db_session.flush()

        llm = AsyncMock()
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response({"scores": [{"label": "A", "importance": 1.0}]})
        )
        qdrant = AsyncMock(side_effect=RuntimeError("qdrant down"))

        with patch("services.sleep.importance_reeval.update_memory_payload_in_qdrant", qdrant):
            phase = ImportanceReevalPhase(db_session, llm)
            result = await phase.execute(
                _make_config(alpha=0.3), user_id, None, None, SleepBudget()
            )

        # Despite the Qdrant failure, PG row is updated and counted.
        expected = 0.3 * 1.0 + 0.7 * 0.5
        row = (await db_session.execute(select(Memory).where(Memory.id == kw["id"]))).scalar_one()
        assert row.importance == pytest.approx(expected)
        assert result.memories_processed == 1

    async def test_no_reporter_skips_action(self, db_session):
        """Without reporter/report_id the update still happens, no action call."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=0.5)
        db_session.add(Memory(**kw))
        await db_session.flush()

        llm = AsyncMock()
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response({"scores": [{"label": "A", "importance": 0.7}]})
        )

        with patch(
            "services.sleep.importance_reeval.update_memory_payload_in_qdrant",
            new_callable=AsyncMock,
        ):
            phase = ImportanceReevalPhase(db_session, llm)
            # report_id present but reporter None → branch is False.
            result = await phase.execute(
                _make_config(),
                user_id,
                None,
                None,
                SleepBudget(),
                reporter=None,
                report_id=uuid4(),
            )

        assert result.memories_processed == 1

    async def test_llm_returns_no_scores_no_update(self, db_session):
        """Empty LLM scores → candidate counted but nothing updated."""
        user_id = f"u-{uuid4()}"
        kw = _mem_kwargs(user_id=user_id, importance=0.5)
        db_session.add(Memory(**kw))
        await db_session.flush()

        llm = AsyncMock()
        llm.complete_json = AsyncMock(return_value=_make_llm_response({"scores": []}))

        with patch(
            "services.sleep.importance_reeval.update_memory_payload_in_qdrant",
            new_callable=AsyncMock,
        ):
            phase = ImportanceReevalPhase(db_session, llm)
            result = await phase.execute(_make_config(), user_id, None, None, SleepBudget())

        assert result.memories_processed == 0
        assert result.details["candidates"] == 1
        assert result.details["updated"] == 0
        row = (await db_session.execute(select(Memory).where(Memory.id == kw["id"]))).scalar_one()
        assert row.importance == pytest.approx(0.5)  # unchanged

    async def test_clamp_keeps_importance_within_unit_interval(self, db_session):
        """A high old + high LLM score never pushes importance above 1.0."""
        user_id = f"u-{uuid4()}"
        # old 0.8 (still a candidate, == IMPORTANCE_MAX), alpha 1.0, llm 1.0.
        kw = _mem_kwargs(user_id=user_id, importance=0.8)
        db_session.add(Memory(**kw))
        await db_session.flush()

        llm = AsyncMock()
        llm.complete_json = AsyncMock(
            return_value=_make_llm_response({"scores": [{"label": "A", "importance": 1.0}]})
        )

        with patch(
            "services.sleep.importance_reeval.update_memory_payload_in_qdrant",
            new_callable=AsyncMock,
        ):
            phase = ImportanceReevalPhase(db_session, llm)
            await phase.execute(_make_config(alpha=1.0), user_id, None, None, SleepBudget())

        row = (await db_session.execute(select(Memory).where(Memory.id == kw["id"]))).scalar_one()
        assert 0.0 <= row.importance <= 1.0
        assert row.importance == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# execute — budget control flow (mocked DB)
# ---------------------------------------------------------------------------


class TestExecuteBudget:
    """Batching + budget-exhaustion branches."""

    async def test_budget_exhausted_before_first_batch_breaks(self, reeval_phase_mockdb):
        """can_afford False on entry → loop breaks, zero updates."""
        phase = reeval_phase_mockdb
        candidates = [_make_mock_memory() for _ in range(3)]
        phase._fetch_candidates = AsyncMock(return_value=candidates)
        phase._evaluate_batch = AsyncMock(return_value={})

        # Budget already at its ceiling: can_afford(llm_calls=1) is False.
        budget = SleepBudget(max_llm_calls=0)
        result = await phase.execute(_make_config(), "u", None, None, budget)

        phase._evaluate_batch.assert_not_called()
        assert result.memories_processed == 0
        assert result.details["candidates"] == 3
        assert result.details["updated"] == 0

    async def test_multiple_batches_processed(self, reeval_phase_mockdb):
        """More than BATCH_SIZE candidates → multiple _evaluate_batch calls."""
        phase = reeval_phase_mockdb
        total = BATCH_SIZE + 3
        candidates = [_make_mock_memory(importance=0.5) for _ in range(total)]
        phase._fetch_candidates = AsyncMock(return_value=candidates)
        # Return empty scores so the inner update loop is a no-op (DB mocked).
        phase._evaluate_batch = AsyncMock(return_value={})

        budget = SleepBudget(max_llm_calls=50)
        await phase.execute(_make_config(), "u", None, None, budget)

        assert phase._evaluate_batch.await_count == 2  # one full batch + remainder

    async def test_unknown_memory_id_in_scores_skipped(self, reeval_phase_mockdb):
        """A score for an id not in the batch is ignored (continue branch)."""
        phase = reeval_phase_mockdb
        mem = _make_mock_memory(importance=0.5)
        phase._fetch_candidates = AsyncMock(return_value=[mem])
        # Score keyed by a stranger UUID not present in the batch.
        phase._evaluate_batch = AsyncMock(return_value={uuid4(): 0.9})

        budget = SleepBudget()
        result = await phase.execute(_make_config(), "u", None, None, budget)

        phase.db.execute.assert_not_called()
        assert result.memories_processed == 0


# ---------------------------------------------------------------------------
# _evaluate_batch — LLM call + parsing branches (mocked DB)
# ---------------------------------------------------------------------------


class TestEvaluateBatch:
    """Direct coverage of the LLM-call and score-parsing logic."""

    async def test_valid_scores_mapped_to_ids(self, reeval_phase_mockdb):
        """Valid labels/scores are mapped back to the memory ids."""
        phase = reeval_phase_mockdb
        m_a = _make_mock_memory(importance=0.5)
        m_b = _make_mock_memory(importance=0.6)
        batch = [m_a, m_b]
        # Labels are assigned A,B in order before shuffle.
        phase.llm_service.complete_json = AsyncMock(
            return_value=_make_llm_response(
                {
                    "scores": [
                        {"label": "A", "importance": 0.9},
                        {"label": "B", "importance": 0.1},
                    ]
                }
            )
        )

        budget = SleepBudget()
        scores = await phase._evaluate_batch(batch, "u", None, None, budget, _make_config())

        assert scores == {m_a.id: 0.9, m_b.id: 0.1}
        assert budget.llm_calls_used == 1
        assert phase._tokens_used == 42

    async def test_unknown_label_rejected(self, reeval_phase_mockdb):
        """A label not assigned to any memory is dropped."""
        phase = reeval_phase_mockdb
        m_a = _make_mock_memory()
        phase.llm_service.complete_json = AsyncMock(
            return_value=_make_llm_response(
                {
                    "scores": [
                        {"label": "A", "importance": 0.7},
                        {"label": "Z", "importance": 0.7},  # no such label
                    ]
                }
            )
        )
        budget = SleepBudget()
        scores = await phase._evaluate_batch([m_a], "u", None, None, budget, _make_config())
        assert scores == {m_a.id: 0.7}

    async def test_out_of_range_importance_rejected(self, reeval_phase_mockdb):
        """importance outside [0,1] is discarded."""
        phase = reeval_phase_mockdb
        m_a = _make_mock_memory()
        m_b = _make_mock_memory()
        phase.llm_service.complete_json = AsyncMock(
            return_value=_make_llm_response(
                {
                    "scores": [
                        {"label": "A", "importance": 1.5},  # > 1.0
                        {"label": "B", "importance": -0.2},  # < 0.0
                    ]
                }
            )
        )
        budget = SleepBudget()
        scores = await phase._evaluate_batch([m_a, m_b], "u", None, None, budget, _make_config())
        assert scores == {}

    async def test_non_numeric_importance_rejected(self, reeval_phase_mockdb):
        """A non-numeric / missing importance value is discarded."""
        phase = reeval_phase_mockdb
        m_a = _make_mock_memory()
        m_b = _make_mock_memory()
        phase.llm_service.complete_json = AsyncMock(
            return_value=_make_llm_response(
                {
                    "scores": [
                        {"label": "A", "importance": "high"},  # str
                        {"label": "B"},  # missing → None
                    ]
                }
            )
        )
        budget = SleepBudget()
        scores = await phase._evaluate_batch([m_a, m_b], "u", None, None, budget, _make_config())
        assert scores == {}

    async def test_bool_importance_rejected_as_out_of_range(self, reeval_phase_mockdb):
        """``True`` is an int(1) in range; ``importance=2`` (int) is rejected.

        Documents the numeric ``isinstance(..., (int, float))`` path: an
        integer score of 0 or 1 is accepted, an integer of 2 is rejected.
        """
        phase = reeval_phase_mockdb
        m_a = _make_mock_memory()
        m_b = _make_mock_memory()
        phase.llm_service.complete_json = AsyncMock(
            return_value=_make_llm_response(
                {
                    "scores": [
                        {"label": "A", "importance": 1},  # int, in range → 1.0
                        {"label": "B", "importance": 2},  # int, out of range
                    ]
                }
            )
        )
        budget = SleepBudget()
        scores = await phase._evaluate_batch([m_a, m_b], "u", None, None, budget, _make_config())
        assert scores == {m_a.id: 1.0}

    async def test_llm_failure_returns_empty_and_does_not_consume(self, reeval_phase_mockdb):
        """An LLM exception is caught: empty dict, budget untouched."""
        phase = reeval_phase_mockdb
        m_a = _make_mock_memory()
        phase.llm_service.complete_json = AsyncMock(side_effect=RuntimeError("boom"))

        budget = SleepBudget()
        scores = await phase._evaluate_batch([m_a], "u", None, None, budget, _make_config())

        assert scores == {}
        assert budget.llm_calls_used == 0  # consume() never reached
        assert phase._llm_breakdown is None

    async def test_missing_scores_key_defaults_empty(self, reeval_phase_mockdb):
        """A parsed payload without a 'scores' key yields no scores."""
        phase = reeval_phase_mockdb
        m_a = _make_mock_memory()
        phase.llm_service.complete_json = AsyncMock(
            return_value=_make_llm_response({})  # no "scores"
        )
        budget = SleepBudget()
        scores = await phase._evaluate_batch([m_a], "u", None, None, budget, _make_config())
        assert scores == {}
        assert budget.llm_calls_used == 1  # call succeeded, just empty

    async def test_prompt_passes_provider_model_and_token_accounting(self, reeval_phase_mockdb):
        """Provider/model from config reach the LLM; tokens accumulate."""
        phase = reeval_phase_mockdb
        m_a = _make_mock_memory()
        phase.llm_service.complete_json = AsyncMock(
            return_value=_make_llm_response(
                {"scores": [{"label": "A", "importance": 0.5}]}, total_tokens=77
            )
        )
        cfg = _make_config(provider="anthropic", model="claude-x")
        budget = SleepBudget()
        await phase._evaluate_batch([m_a], "user-9", "ctx", "ws", budget, cfg)

        _, kwargs = phase.llm_service.complete_json.call_args
        assert kwargs["provider"] == "anthropic"
        assert kwargs["model"] == "claude-x"
        assert kwargs["user_id"] == "user-9"
        assert kwargs["context_id"] == "ctx"
        assert kwargs["workspace_id"] == "ws"
        assert phase._tokens_used == 77
        assert phase._llm_breakdown is not None
        assert phase._llm_breakdown.calls == 1


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Sanity bounds on the module-level tuning constants."""

    def test_constants(self):
        assert IMPORTANCE_MIN == 0.2
        assert IMPORTANCE_MAX == 0.8
        assert STALENESS_DAYS == 7
        assert BATCH_SIZE == 10
        assert IMPORTANCE_MIN < IMPORTANCE_MAX
