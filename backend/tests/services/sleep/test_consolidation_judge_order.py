"""#3019: the consolidation judge continues where it stopped.

The borderline rows are judged in ``(created_at, id)`` order starting after
the judge's position and wrapping, so a judge whose calls do not cover every
borderline row reaches the tail on later runs instead of seeing the same head
every night. Which rows are promoted or kept is unchanged.
"""

from __future__ import annotations

import random
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.consolidation import BATCH_SIZE, ConsolidationPhase
from services.sleep.coverage import (
    BUDGET,
    FAILURE,
    PASSED,
    SETTLED,
    LaneEntry,
    advance_judge,
    sweep_key,
)
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow


def _rows(count: int):
    base = utcnow() - timedelta(days=5)
    rows = []
    for i in range(count):
        m = MagicMock()
        m.id = uuid4()
        m.summary = "s"
        m.type = "note"
        m.importance = 0.5
        m.access_count = 0
        m.reference_count = 0
        m.scope = "working"
        m.created_at = base + timedelta(minutes=i)
        rows.append(m)
    return rows


def _config(judge=True):
    config = MagicMock()
    config.sleep_llm_provider = "openai"
    config.sleep_llm_model = "m"
    config.sleep_consolidation_judge_enabled = judge
    return config


def _phase(rows, entry: LaneEntry | None = None, *, failing_batches=()):
    with (
        patch("services.sleep.consolidation.MemoryRepository"),
        patch("services.sleep.consolidation.GraphService"),
    ):
        phase = ConsolidationPhase(AsyncMock(), AsyncMock(), coverage=entry)
    phase.memory_repo = AsyncMock()
    phase.memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=list)
    shuffled = list(rows)
    random.shuffle(shuffled)
    phase._fetch_working_memories = AsyncMock(return_value=shuffled)
    seen: list[list] = []

    async def judge(batch, user_id, context_id, workspace_id, budget, config):
        seen.append([m.id for m in batch])
        budget.consume(llm_calls=1)
        if len(seen) - 1 in failing_batches:
            phase._llm_failures += 1
        return {}

    phase._llm_judge_batch = judge
    return phase, seen


async def _execute(phase, *, calls=50, judge=True):
    with patch("services.sleep.consolidation.GraphService") as graph:
        graph.return_value.stats = AsyncMock(return_value={"total_edges": 0})
        return await phase.execute(
            _config(judge), "u", None, None, SleepBudget(max_llm_calls=calls)
        )


def _ids(rows):
    return [m.id for m in rows]


class TestJudgeOrder:
    @pytest.mark.asyncio
    async def test_oldest_first_from_the_start(self):
        rows = _rows(7)
        phase, seen = _phase(rows)
        await _execute(phase)
        assert seen == [_ids(rows[:BATCH_SIZE]), _ids(rows[BATCH_SIZE:])]

    @pytest.mark.asyncio
    async def test_starts_after_the_position_and_wraps(self):
        rows = _rows(8)
        key = sweep_key(rows[5])
        phase, seen = _phase(rows, LaneEntry(at=key[0], id=key[1]))
        await _execute(phase)
        assert seen == [_ids([rows[6], rows[7], *rows[:3]]), _ids(rows[3:6])]

    @pytest.mark.asyncio
    async def test_the_position_after_a_cut(self):
        rows = _rows(12)
        phase, seen = _phase(rows)
        result = await _execute(phase, calls=1)

        assert seen == [_ids(rows[:BATCH_SIZE])]
        outcomes = phase.coverage_outcome.outcomes
        assert [outcomes[m.id] for m in rows] == [SETTLED] * BATCH_SIZE + [BUDGET] * 7
        entry, report = advance_judge(LaneEntry(), phase.coverage_outcome, returned=True)
        assert entry.position == sweep_key(rows[BATCH_SIZE - 1])
        assert report == {"judge": {"taken": 12, "advanced": BATCH_SIZE}, "skipped_after_stalls": 0}
        assert result.details["llm_pass_cut_for_budget"] is True

        # The next run starts where this one stopped.
        phase, seen = _phase(rows, entry)
        await _execute(phase, calls=1)
        assert seen == [_ids(rows[BATCH_SIZE : 2 * BATCH_SIZE])]

    @pytest.mark.asyncio
    async def test_back_to_the_start_after_a_full_round(self):
        rows = _rows(6)
        key = sweep_key(rows[2])
        phase, _ = _phase(rows, LaneEntry(at=key[0], id=key[1]))
        await _execute(phase)
        entry, _ = advance_judge(
            LaneEntry(at=key[0], id=key[1]), phase.coverage_outcome, returned=True
        )
        assert entry.position is None

    @pytest.mark.asyncio
    async def test_a_failed_batch_passes_when_another_answered(self):
        rows = _rows(7)
        phase, _ = _phase(rows, failing_batches=(0,))
        await _execute(phase)
        outcomes = phase.coverage_outcome.outcomes
        assert [outcomes[m.id] for m in rows] == [PASSED] * BATCH_SIZE + [SETTLED] * 2

    @pytest.mark.asyncio
    async def test_a_judge_outage_is_a_failure(self):
        rows = _rows(7)
        phase, _ = _phase(rows, failing_batches=(0, 1))
        await _execute(phase)
        assert set(phase.coverage_outcome.outcomes.values()) == {FAILURE}

    @pytest.mark.asyncio
    async def test_no_outcome_when_the_judge_is_off(self):
        phase, seen = _phase(_rows(3))
        await _execute(phase, judge=False)
        assert seen == []
        assert phase.coverage_outcome is None

    @pytest.mark.asyncio
    async def test_no_outcome_without_borderline_rows(self):
        rows = _rows(2)
        for m in rows:
            m.reference_count = 5  # promoted by rule
        phase, seen = _phase(rows)
        await _execute(phase)
        assert seen == []
        assert phase.coverage_outcome is None
