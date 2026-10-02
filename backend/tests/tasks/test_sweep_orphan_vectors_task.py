"""#1798: the daily orphan vector sweep task — scheduled, guarded, and switchable."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orphan_vector_sweep import SCHEDULED_MAX_ORPHAN_RATIO, SweepResult
from tasks.neural_tasks import schedule_neural_tasks, sweep_orphan_vectors_task
from tests.tasks.conftest import mock_get_db_factory

_ENV = "ORPHAN_VECTOR_SWEEP_ENABLED"


@pytest.fixture
def sweep():
    result = SweepResult(dry_run=False, grace=timedelta(hours=1))
    with patch(
        "services.orphan_vector_sweep.sweep_orphan_points", AsyncMock(return_value=result)
    ) as mock:
        yield mock


class TestSweepOrphanVectorsTask:
    @pytest.mark.asyncio
    async def test_deletes_with_the_scheduled_ratio_guard(self, sweep, monkeypatch):
        monkeypatch.delenv(_ENV, raising=False)
        db = MagicMock()

        with patch("tasks.neural_tasks.get_db", mock_get_db_factory(db)):
            await sweep_orphan_vectors_task()

        sweep.assert_awaited_once_with(
            db, dry_run=False, max_orphan_ratio=SCHEDULED_MAX_ORPHAN_RATIO
        )

    @pytest.mark.asyncio
    async def test_env_switch_turns_it_off(self, sweep, monkeypatch):
        monkeypatch.setenv(_ENV, "false")

        await sweep_orphan_vectors_task()

        sweep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_sweep_does_not_escape_the_scheduler(self, sweep, monkeypatch):
        monkeypatch.delenv(_ENV, raising=False)
        sweep.side_effect = RuntimeError("qdrant down")

        with patch("tasks.neural_tasks.get_db", mock_get_db_factory(MagicMock())):
            await sweep_orphan_vectors_task()

    def test_scheduled_daily_after_the_tombstone_purge(self):
        scheduler = MagicMock()

        schedule_neural_tasks(scheduler)

        jobs = {call.kwargs["id"]: call for call in scheduler.add_job.call_args_list}
        assert jobs["sweep_orphan_vectors"].args[0] is sweep_orphan_vectors_task
        fields = {f.name: str(f) for f in jobs["sweep_orphan_vectors"].kwargs["trigger"].fields}
        assert (fields["hour"], fields["minute"]) == ("4", "30")
