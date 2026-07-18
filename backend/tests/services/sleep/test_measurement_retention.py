"""#1355: measurement-series retention purge phase.

Pins (mirroring test_forget_retention.py):

1. Default (``sleep_measurement_retention_days = 0``) is DISABLED — the
   #1333 append-only contract (retain forever) is preserved unless the
   operator declares a window.
2. The sweep runs ONLY for context-scoped runs: measurements carry no
   user/workspace column, so a broader run has no safe ownership
   predicate and must skip.
3. When enabled + context-scoped, old observations are hard-deleted and
   audited as ONE batch-summary action with counts only — never metric
   names or values (user-authored free-form data).
4. Nothing purged → no audit action.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.sleep.measurement_retention import MeasurementRetentionPhase


def _config(days: int) -> MagicMock:
    cfg = MagicMock()
    cfg.sleep_measurement_retention_days = days
    return cfg


def _budget() -> MagicMock:
    budget = MagicMock()
    budget.exhausted = False
    return budget


@pytest.mark.asyncio
async def test_disabled_by_default_skips() -> None:
    db = AsyncMock()
    phase = MeasurementRetentionPhase(db)
    result = await phase.execute(_config(0), "user", None, str(uuid4()), _budget())
    assert result.skipped is True
    assert result.skip_reason == "retention_disabled"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_context_scoped_run_skips() -> None:
    db = AsyncMock()
    phase = MeasurementRetentionPhase(db)
    result = await phase.execute(_config(30), "user", str(uuid4()), None, _budget())
    assert result.skipped is True
    assert result.skip_reason == "no_context_scope"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_purges_old_observations_and_audits_batch_summary() -> None:
    db = AsyncMock()
    delete_result = MagicMock()
    delete_result.rowcount = 5
    db.execute.return_value = delete_result

    reporter = MagicMock()
    reporter.add_action = AsyncMock()
    report_id = uuid4()
    ctx = str(uuid4())

    phase = MeasurementRetentionPhase(db)
    result = await phase.execute(
        _config(90), "user", None, ctx, _budget(), reporter=reporter, report_id=report_id
    )

    assert result.skipped is False
    # Purged rows never consume the shared per-run budget.
    assert result.memories_processed == 0
    assert result.details["purged"] == 5
    assert result.details["retention_days"] == 90

    delete_sql = str(db.execute.await_args.args[0])
    assert "measurements" in delete_sql
    assert "measured_at" in delete_sql
    assert "context_id" in delete_sql

    reporter.add_action.assert_awaited_once()
    kwargs = reporter.add_action.await_args.kwargs
    assert kwargs["phase"] == "measurement_retention"
    assert kwargs["action_type"] == "purge"
    assert kwargs["details"]["purged"] == 5
    assert kwargs["details"]["retention_days"] == 90
    assert "cutoff" in kwargs["details"]


@pytest.mark.asyncio
async def test_reads_real_config_field_and_is_wired_into_full_phases() -> None:
    """#1336 dead-code lesson, closed both ways: the phase must read the
    REAL NeuralMemoryConfig field (a getattr against a renamed field would
    silently return 0 = disabled forever while MagicMock tests stay
    green), and the orchestrator must actually schedule the phase."""
    from neural.config import NeuralMemoryConfig
    from services.sleep.orchestrator import FULL_PHASES

    db = AsyncMock()
    delete_result = MagicMock()
    delete_result.rowcount = 1
    db.execute.return_value = delete_result

    phase = MeasurementRetentionPhase(db)
    real_config = NeuralMemoryConfig(sleep_measurement_retention_days=30)
    result = await phase.execute(real_config, "user", None, str(uuid4()), _budget())

    assert result.skipped is False
    assert result.details["retention_days"] == 30
    assert "measurement_retention" in FULL_PHASES


@pytest.mark.asyncio
async def test_nothing_to_purge_records_no_action() -> None:
    db = AsyncMock()
    delete_result = MagicMock()
    delete_result.rowcount = 0
    db.execute.return_value = delete_result

    reporter = MagicMock()
    reporter.add_action = AsyncMock()

    phase = MeasurementRetentionPhase(db)
    result = await phase.execute(
        _config(7), "user", None, str(uuid4()), _budget(), reporter=reporter, report_id=uuid4()
    )

    assert result.skipped is False
    assert result.details["purged"] == 0
    reporter.add_action.assert_not_awaited()
