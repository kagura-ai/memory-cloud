"""#1209: merge-loser retention purge phase.

Pins:

1. Default (``sleep_merge_retention_days = 0``) is DISABLED — the phase
   skips and deletes nothing (pre-#1209 behavior preserved: merges stay
   reversible forever unless the operator declares a window).
2. When enabled, only rows soft-deleted by sleep maintenance and older than
   the cutoff are purged, and the purge is audited as ONE batch-summary
   action (never per-row).
3. Nothing to purge → no audit action, zero counts.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.sleep.merge_retention import MergeRetentionPhase


def _config(days: int) -> MagicMock:
    cfg = MagicMock()
    cfg.sleep_merge_retention_days = days
    return cfg


def _budget() -> MagicMock:
    budget = MagicMock()
    budget.exhausted = False
    return budget


@pytest.mark.asyncio
async def test_disabled_by_default_skips() -> None:
    db = AsyncMock()
    phase = MergeRetentionPhase(db)
    result = await phase.execute(_config(0), "user", None, None, _budget())
    assert result.skipped is True
    assert result.skip_reason == "retention_disabled"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_purges_and_audits_batch_summary() -> None:
    db = AsyncMock()
    purged_ids = [uuid4(), uuid4(), uuid4()]
    select_result = MagicMock()
    select_result.all.return_value = [(mid,) for mid in purged_ids]
    db.execute.side_effect = [select_result, MagicMock()]  # select, delete

    reporter = MagicMock()
    reporter.add_action = AsyncMock()
    report_id = uuid4()

    phase = MergeRetentionPhase(db)
    result = await phase.execute(
        _config(30), "user", None, None, _budget(), reporter=reporter, report_id=report_id
    )

    assert result.skipped is False
    # Purged dead rows must NOT consume the shared per-run budget — the
    # orchestrator feeds memories_processed into budget.consume, and a
    # first-enable backlog purge would otherwise starve the live-memory
    # phases (importance_reeval / consolidation) that run after this one.
    assert result.memories_processed == 0
    assert result.details["purged"] == 3
    assert result.details["retention_days"] == 30

    reporter.add_action.assert_awaited_once()
    kwargs = reporter.add_action.await_args.kwargs
    assert kwargs["phase"] == "merge_retention"
    assert kwargs["action_type"] == "purge"
    assert kwargs["details"]["purged"] == 3
    # Batch summary — one action total, regardless of row count.
    assert reporter.add_action.await_count == 1


@pytest.mark.asyncio
async def test_nothing_to_purge_records_no_action() -> None:
    db = AsyncMock()
    select_result = MagicMock()
    select_result.all.return_value = []
    db.execute.return_value = select_result

    reporter = MagicMock()
    reporter.add_action = AsyncMock()

    phase = MergeRetentionPhase(db)
    result = await phase.execute(
        _config(7), "user", None, None, _budget(), reporter=reporter, report_id=uuid4()
    )

    assert result.details["purged"] == 0
    assert result.memories_processed == 0
    reporter.add_action.assert_not_awaited()
    # Only the select ran — no delete statement was issued.
    assert db.execute.await_count == 1
