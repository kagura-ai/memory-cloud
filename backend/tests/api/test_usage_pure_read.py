"""Pure-read contract for `GET /api/v1/usage/current` (Issue #586, #668).

PR #588 made ``EffectiveQuotaService.get_effective_quotas`` pure-read at the
service layer. The route layer carried a sister antipattern: when the caller
had no ``UserPlan`` row, ``get_current_usage`` lazy-created one and COMMITted
from the GET path.

Issue #668 removed the legacy ``user_plans`` table entirely. The handler no
longer reads or writes any per-user plan row — the ``plan`` block is now
sourced from the caller's current workspace via ``EffectiveQuotaService``,
falling back to the FREE plan tier when no workspace is selected. That makes
the pure-read contract structural (there is no write path left to regress),
but we still pin it with the four-pillar mock pattern:

1. ``commit.assert_not_awaited()`` — no explicit commit reaches the DB.
2. ``add.assert_not_called()`` — and no ``db.add`` either, because the
   ``get_db()`` dependency runs ``await session.commit()`` on normal exit
   (``backend/src/db/base.py``). An ``add`` without an explicit commit would
   still persist through that auto-commit, masking the bug.
3. ``execute.await_count`` — read-side calls still happen (we are not
   regressing into a degenerate no-op endpoint).
4. Functional value check — with no current workspace the response uses the
   FREE plan-tier limits (``config.plan_tiers.get_plan_tier("free")``).
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
    ``None`` early when no workspace is selected), skips the
    ``EffectiveQuotaService`` block, and skips the ``Workspace.plan_name``
    lookup — so the response falls through to the FREE-tier defaults this
    file pins.
    """
    return {"user_id": "u-pure-read", "current_workspace_id": None}


def _mock_db():
    """Build a MagicMock AsyncSession whose every execute() returns a result
    with ``scalar() == 0`` (the count queries) and a cap-summary row.

    SQLAlchemy patterns used by the handler on the no-workspace path:
    - ``result.scalar()`` for ``count(...)`` queries.
    - ``result.one_or_none()`` returning a Row with ``owned_count`` and
      ``workspace_slot_bonus`` attributes for the workspace cap helper
      (Issue #675's ``get_user_workspace_cap_summary`` JOIN).
    """
    db = MagicMock()

    count_result = MagicMock()
    count_result.scalar_one_or_none.return_value = None
    count_result.scalar.return_value = 0

    # Stand-in row for the cap-summary helper's JOIN: zero owned workspaces,
    # zero bonus → effective cap = 1 (base).
    cap_row = MagicMock()
    cap_row.owned_count = 0
    cap_row.workspace_slot_bonus = 0
    count_result.one_or_none.return_value = cap_row

    # Headroom of 32 zero-count results absorbs the count queries the handler
    # issues plus future additions, so a query added in an unrelated PR doesn't
    # break this test for the wrong reason.
    db.execute = AsyncMock(side_effect=[count_result] * 32)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestGetCurrentUsagePureReadContract:
    """Pin the four-pillar pure-read contract for `GET /usage/current`."""

    @pytest.mark.asyncio
    async def test_no_commit_no_add(self, session_user):
        """Pillars 1 + 2: the GET path emits no commit and no add."""
        db = _mock_db()

        await get_current_usage(user=session_user, db=db)

        db.commit.assert_not_awaited()
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_executes_read_queries(self, session_user):
        """Pillar 3: read-side queries still happen (lower-bound 8)."""
        db = _mock_db()

        await get_current_usage(user=session_user, db=db)

        assert db.execute.await_count >= 8

    @pytest.mark.asyncio
    async def test_response_uses_free_tier_when_no_workspace(self, session_user):
        """Pillar 4: with no current workspace the plan block is FREE-tier."""
        from config.plan_tiers import get_plan_tier

        free = get_plan_tier("free")
        db = _mock_db()

        response = await get_current_usage(user=session_user, db=db)

        assert response.plan.plan_name == "free"
        assert response.plan.memory_limit == free.memory_limit
        assert response.plan.daily_total_limit == (
            free.mcp_calls_per_day + free.rest_calls_per_day + free.public_calls_per_day
        )
        assert response.plan.weekly_total_limit == (
            free.mcp_calls_per_week + free.rest_calls_per_week + free.public_calls_per_week
        )
