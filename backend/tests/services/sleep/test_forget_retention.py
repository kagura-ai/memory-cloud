"""#1336 Gap 1: user-forgotten tombstone retention purge phase.

Pins (mirroring test_merge_retention.py):

1. Default (``sleep_forget_retention_days = 0``) is DISABLED — forget()
   tombstones are retained forever unless the operator declares a window
   (pre-#1336 behavior preserved).
2. When enabled, only USER-forgotten rows (``deleted_by`` is the user's sub,
   or NULL on legacy rows — never the ``sleep_maintenance`` merge sentinel)
   older than the cutoff are purged, and the purge is audited as ONE
   batch-summary action.
3. Nothing to purge → no audit action.

forget() already removed the Qdrant point and neural edges at soft-delete
time, so the phase only reaps the residual PG row — the tombstone whose
``details`` (coordinates included) would otherwise persist at rest forever.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.sleep.forget_retention import ForgetRetentionPhase


def _config(days: int) -> MagicMock:
    cfg = MagicMock()
    cfg.sleep_forget_retention_days = days
    return cfg


def _budget() -> MagicMock:
    budget = MagicMock()
    budget.exhausted = False
    return budget


@pytest.mark.asyncio
async def test_disabled_by_default_skips() -> None:
    db = AsyncMock()
    phase = ForgetRetentionPhase(db)
    result = await phase.execute(_config(0), "user", None, None, _budget())
    assert result.skipped is True
    assert result.skip_reason == "retention_disabled"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_purges_user_forgotten_rows_and_audits_batch_summary() -> None:
    db = AsyncMock()
    purged_ids = [uuid4(), uuid4()]
    select_result = MagicMock()
    select_result.all.return_value = [(mid,) for mid in purged_ids]
    db.execute.side_effect = [select_result, MagicMock()]  # select, delete

    reporter = MagicMock()
    reporter.add_action = AsyncMock()
    report_id = uuid4()

    phase = ForgetRetentionPhase(db)
    result = await phase.execute(
        _config(30), "user", None, None, _budget(), reporter=reporter, report_id=report_id
    )

    assert result.skipped is False
    # Purged dead rows must NOT consume the shared per-run budget (same
    # rationale as merge_retention: a first-enable backlog purge must not
    # starve the live-memory phases).
    assert result.memories_processed == 0
    assert result.details["purged"] == 2
    assert result.details["retention_days"] == 30

    # The selection must EXCLUDE merge losers (they have their own window)
    # and INCLUDE legacy NULL deleted_by tombstones.
    select_sql = str(db.execute.await_args_list[0].args[0])
    assert "deleted_by" in select_sql
    delete_sql = str(db.execute.await_args_list[1].args[0])
    # TOCTOU guard: the DELETE re-asserts the predicate, not just ids.
    assert "deleted_at" in delete_sql

    reporter.add_action.assert_awaited_once()
    kwargs = reporter.add_action.await_args.kwargs
    assert kwargs["phase"] == "forget_retention"
    assert kwargs["action_type"] == "purge"
    assert kwargs["details"]["purged"] == 2


@pytest.mark.asyncio
async def test_nothing_to_purge_records_no_action() -> None:
    db = AsyncMock()
    select_result = MagicMock()
    select_result.all.return_value = []
    db.execute.return_value = select_result

    reporter = MagicMock()
    reporter.add_action = AsyncMock()

    phase = ForgetRetentionPhase(db)
    result = await phase.execute(
        _config(7), "user", None, None, _budget(), reporter=reporter, report_id=uuid4()
    )

    assert result.skipped is False
    assert result.details["purged"] == 0
    reporter.add_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_excludes_soft_deleted_contexts() -> None:
    """#1336 review: context soft-delete (#84 recovery design) tombstones
    memories with deleted_by=<user sub> too, but deliberately KEEPS their
    Qdrant points — purging those PG rows would orphan live vectors and
    destroy context recovery. The sweep must be restricted to memories in
    live contexts."""
    db = AsyncMock()
    select_result = MagicMock()
    select_result.all.return_value = []
    db.execute.return_value = select_result

    phase = ForgetRetentionPhase(db)
    await phase.execute(_config(7), "user", None, None, _budget())

    select_sql = str(db.execute.await_args_list[0].args[0])
    assert "IN (SELECT contexts.id" in select_sql
    assert "contexts.deleted_at IS NULL" in select_sql
