"""Pure-read contract for `GET /api/v1/usage/current` (Issue #586).

PR #588 made ``EffectiveQuotaService.get_effective_quotas`` pure-read at the
service layer. The route layer carried a sister antipattern: when the
caller had no ``UserPlan`` row, ``get_current_usage`` lazy-created one and
COMMITted from the GET path. This file pins the new pure-read contract
with the **four-pillar** mock pattern:

1. ``commit.assert_not_awaited()`` — no explicit commit reaches the DB.
2. ``add.assert_not_called()`` — and no ``db.add`` either, because the
   ``get_db()`` dependency runs ``await session.commit()`` on normal exit
   (``backend/src/db/base.py:142``). An ``add`` without an explicit commit
   would still persist the row through that auto-commit, masking the bug.
   Pillar 4 is the explicit defense against that.
3. ``execute.await_count`` — read-side calls still happen (we are not
   regressing into a degenerate no-op endpoint).
4. Functional value check — the response uses ``settings.default_*``
   values when the plan row is missing.

The default ``UserPlan`` row is now created in
``auth.roles.RoleManager._ensure_user_postgres`` (same transaction as the
``User`` INSERT). Existing users without a row fall through to the
in-memory fallback this file pins.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.usage import get_current_usage


@pytest.fixture
def session_user():
    """Session user dict with no current workspace.

    Setting ``current_workspace_id=None`` short-circuits
    ``_build_analysis_usage`` / ``_build_sleep_contexts_usage`` (both return
    ``None`` early when no workspace is selected) and skips the
    ``EffectiveQuotaService`` block. That keeps the unit test focused on
    the UserPlan fetch + the per-day / per-week ``UsageStats`` counts.
    """
    return {"user_id": "u-pure-read", "current_workspace_id": None}


def _mock_db_with_plan_lookup_returning(plan):
    """Build a MagicMock AsyncSession whose first execute() returns ``plan``
    (the UserPlan lookup) and every subsequent execute() returns a row with
    ``scalar() == 0`` (the count queries).

    SQLAlchemy patterns used by the handler:
    - ``result.scalar_one_or_none()`` for the UserPlan SELECT.
    - ``result.scalar()`` for ``count(...)`` queries.
    - ``result.one_or_none()`` returning a Row with
      ``owned_count`` and ``workspace_slot_bonus`` attributes for the
      workspace cap helper (Issue #675's
      ``get_user_workspace_cap_summary`` JOIN).

    The same Result-shaped mock satisfies all three because each method is
    stubbed to return the values the handler expects.
    """
    db = MagicMock()

    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan
    plan_result.scalar.return_value = 0

    count_result = MagicMock()
    count_result.scalar_one_or_none.return_value = None
    count_result.scalar.return_value = 0

    # Stand-in row for the cap-summary helper's JOIN: zero owned workspaces,
    # zero bonus → effective cap = 1 (base). Setting this on the same shared
    # ``count_result`` lets any subsequent execute() in the chain satisfy the
    # cap-summary call without disturbing the existing count-query consumers.
    cap_row = MagicMock()
    cap_row.owned_count = 0
    cap_row.workspace_slot_bonus = 0
    count_result.one_or_none.return_value = cap_row

    # First call returns the plan lookup; the headroom of 32 zero-count
    # results absorbs the 9-10 count queries the handler currently issues
    # plus future additions, so a query added in an unrelated PR doesn't
    # break this test for the wrong reason.
    db.execute = AsyncMock(side_effect=[plan_result] + [count_result] * 32)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestGetCurrentUsagePureReadContract:
    """Pin the four-pillar pure-read contract for `GET /usage/current`."""

    @pytest.mark.asyncio
    async def test_no_commit_no_add_when_plan_missing(self, session_user):
        """Pillars 1 + 2: missing-plan branch emits no commit and no add."""
        db = _mock_db_with_plan_lookup_returning(None)

        await get_current_usage(user=session_user, db=db)

        db.commit.assert_not_awaited()
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_executes_read_queries(self, session_user):
        """Pillar 3: read-side queries still happen (lower-bound 10)."""
        db = _mock_db_with_plan_lookup_returning(None)

        await get_current_usage(user=session_user, db=db)

        assert db.execute.await_count >= 10

    @pytest.mark.asyncio
    async def test_response_uses_settings_defaults_when_plan_missing(
        self, session_user, monkeypatch
    ):
        """Pillar 4: missing-plan branch propagates ``settings.default_*``."""
        from api.routes import usage as usage_module

        # Sentinel ints proxy through the missing-plan branch into the response.
        # Floats for the warning/critical thresholds prevent MagicMock leaking
        # into ``calculate_usage_status``'s percentage comparison.
        sentinel = MagicMock()
        sentinel.default_plan_memory_limit = 4242
        sentinel.default_plan_daily_api_limit = 1234
        sentinel.default_plan_weekly_api_limit = 5678
        sentinel.usage_warning_threshold = 0.8
        sentinel.usage_critical_threshold = 0.95
        monkeypatch.setattr(usage_module, "get_settings", lambda: sentinel)

        db = _mock_db_with_plan_lookup_returning(None)
        response = await get_current_usage(user=session_user, db=db)

        assert response.plan.plan_name == "free"
        assert response.plan.memory_limit == 4242
        assert response.plan.daily_total_limit == 1234
        assert response.plan.weekly_total_limit == 5678

    @pytest.mark.asyncio
    async def test_no_commit_no_add_when_plan_exists(self, session_user):
        """Sanity twin: existing-plan branch must also stay pure-read.

        Guards against a future edit that adds a write side-effect to the
        existing-plan branch (e.g. ``last_seen_at`` update) from quietly
        regressing the contract.
        """
        existing_plan = MagicMock()
        existing_plan.plan_name = "pro"
        existing_plan.memory_limit = 9999
        existing_plan.daily_api_limit = 9999
        existing_plan.weekly_api_limit = 99999

        db = _mock_db_with_plan_lookup_returning(existing_plan)

        await get_current_usage(user=session_user, db=db)

        db.commit.assert_not_awaited()
        db.add.assert_not_called()
