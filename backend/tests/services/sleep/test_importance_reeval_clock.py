"""#3019 point 4: importance re-evaluation keeps its own clock.

The importance write no longer stamps ``updated_at``; one statement per
answered batch sets ``importance_reevaluated_at`` for every row of the batch,
before the per-row writes. The fetch is ordered (never judged first, then the
longest unjudged) and limited to what the phase's calls can judge, plus one
probe row that reports ``more_due``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update

from services.sleep.importance_reeval import (
    BATCH_SIZE,
    REEVALUATED_STATEMENT,
    ImportanceReevalPhase,
)
from services.sleep.reporter import SleepBudget


def _config():
    config = MagicMock()
    config.sleep_importance_reeval_enabled = True
    config.importance_ema_alpha = 0.3
    config.sleep_llm_provider = "openai"
    config.sleep_llm_model = "m"
    return config


def _row(importance=0.5):
    m = MagicMock()
    m.id = uuid4()
    m.user_id = "u"
    m.importance = importance
    m.summary = "s"
    m.type = "note"
    m.access_count = 0
    m.scope = "working"
    return m


def _phase(rows):
    phase = ImportanceReevalPhase(AsyncMock(), AsyncMock())
    phase._chunk_cascade = AsyncMock(return_value=False)
    phase._fetch_candidates = AsyncMock(return_value=rows)
    return phase


def _executed(phase):
    return [call.args[0] for call in phase.db.execute.await_args_list]


def _compiled_values(stmt: Update) -> set[str]:
    return set(stmt.compile(dialect=postgresql.dialect()).params)


class TestTheImportanceWrite:
    @pytest.mark.asyncio
    async def test_no_updated_at_and_the_marker_comes_first_for_the_whole_batch(self):
        rows = [_row(), _row(), _row()]
        phase = _phase(rows)
        phase._evaluate_batch = AsyncMock(return_value={rows[0].id: 0.9})

        await phase.execute(_config(), "u", None, None, SleepBudget())

        statements = _executed(phase)
        assert statements[0] is REEVALUATED_STATEMENT
        params = phase.db.execute.await_args_list[0].args[1]
        assert sorted(params["ids"]) == sorted(m.id for m in rows)
        assert params["user_id"] == "u"
        writes = [s for s in statements[1:] if isinstance(s, Update)]
        assert len(writes) == 1
        values = _compiled_values(writes[0])
        assert "importance" in values
        assert "updated_at" not in values
        assert "importance_reevaluated_at" not in values

    @pytest.mark.asyncio
    async def test_a_failed_batch_marks_nothing(self):
        rows = [_row(), _row()]
        phase = _phase(rows)
        phase.llm_service.complete_json = AsyncMock(side_effect=RuntimeError("down"))

        await phase.execute(_config(), "u", None, None, SleepBudget())

        assert REEVALUATED_STATEMENT not in _executed(phase)

    @pytest.mark.asyncio
    async def test_an_answer_with_no_valid_score_still_marks_its_rows(self):
        rows = [_row(), _row()]
        phase = _phase(rows)
        phase._evaluate_batch = AsyncMock(return_value={})

        result = await phase.execute(_config(), "u", None, None, SleepBudget())

        assert _executed(phase) == [REEVALUATED_STATEMENT]
        assert result.details["no_action_reasons"] == {"no_score_returned": 2}

    @pytest.mark.asyncio
    async def test_the_marker_statement_locks_in_id_order(self):
        sql = str(REEVALUATED_STATEMENT)
        assert "ORDER BY id FOR UPDATE" in sql
        assert "user_id = :user_id" in sql
        assert "deleted_at IS NULL" in sql
        assert "updated_at" not in sql


class TestTheFetchBound:
    @pytest.mark.asyncio
    async def test_more_due_than_one_call_can_judge(self):
        rows = [_row() for _ in range(25)]
        phase = _phase(rows)
        phase._evaluate_batch = AsyncMock(return_value={})

        result = await phase.execute(_config(), "u", None, None, SleepBudget(max_llm_calls=1))

        assert phase._fetch_candidates.await_args.kwargs["limit"] == BATCH_SIZE + 1
        assert phase._evaluate_batch.await_count == 1
        assert len(phase._evaluate_batch.await_args.args[0]) == BATCH_SIZE
        assert result.details["candidates"] == BATCH_SIZE
        assert result.details["more_due"] is True
        assert result.details["budget_cut"] is True
        assert "not_judged_budget" not in result.details["no_action_reasons"]

    @pytest.mark.asyncio
    async def test_everything_due_fits(self):
        rows = [_row() for _ in range(BATCH_SIZE)]
        phase = _phase(rows)
        phase._evaluate_batch = AsyncMock(return_value={})

        result = await phase.execute(_config(), "u", None, None, SleepBudget(max_llm_calls=1))

        assert result.details["more_due"] is False
        assert result.details["budget_cut"] is False

    @pytest.mark.asyncio
    async def test_the_memory_allowance_bounds_the_fetch(self):
        phase = _phase([])
        await phase.execute(
            _config(), "u", None, None, SleepBudget(max_llm_calls=50, max_memories=7)
        )
        assert phase._fetch_candidates.await_args.kwargs["limit"] == 8

    @pytest.mark.asyncio
    async def test_no_call_left_judges_nothing_and_says_more_are_due(self):
        rows = [_row() for _ in range(3)]
        phase = _phase(rows)
        phase._evaluate_batch = AsyncMock(return_value={})

        result = await phase.execute(_config(), "u", None, None, SleepBudget(max_llm_calls=0))

        phase._evaluate_batch.assert_not_called()
        assert result.details["candidates"] == 0
        assert result.details["more_due"] is True
        assert result.details["budget_cut"] is True


class TestTheDuePredicateAndOrder:
    @pytest.mark.asyncio
    async def test_fetch_statement(self):
        phase = ImportanceReevalPhase(AsyncMock(), AsyncMock())
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        phase.db.execute = AsyncMock(return_value=result)

        await phase._fetch_candidates("u", None, None, limit=11)

        stmt = phase.db.execute.await_args.args[0]
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert (
            "memories.importance_reevaluated_at IS NULL "
            "OR memories.importance_reevaluated_at <" in sql
        )
        assert "coalesce(memories.updated_at, memories.created_at) <" in sql
        assert (
            "ORDER BY memories.importance_reevaluated_at ASC NULLS FIRST, "
            "memories.created_at ASC, memories.id ASC" in sql
        )
        assert "LIMIT" in sql


class TestRollback:
    @pytest.mark.asyncio
    async def test_undo_writes_the_marker_not_updated_at(self):
        from mcp_server.tools.sleep import _undo_update_importance

        db = AsyncMock()
        restored = MagicMock()
        restored.one_or_none.return_value = None
        db.execute = AsyncMock(return_value=restored)
        action = MagicMock()
        action.memory_id = uuid4()
        action.details = {"old_importance": 0.4}
        ctx = MagicMock()
        ctx.user_id = "u"
        summary = {"importance_kept": 0, "importance_restored": 0}

        await _undo_update_importance(db, action, ctx, summary)

        values = _compiled_values(db.execute.await_args.args[0])
        assert "importance" in values
        assert "importance_reevaluated_at" in values
        assert "updated_at" not in values
