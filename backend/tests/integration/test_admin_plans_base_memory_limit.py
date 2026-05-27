"""Regression test for #801 — base.memory_limit must read plan_tier, not the column.

``GET /admin/plans/workspaces/{id}/quotas`` builds its ``base`` ``QuotaBreakdown``
from the plan tier definition for every dimension EXCEPT ``memory_limit``, which
(pre-#801) read the per-workspace ``Workspace.memory_limit`` cache column at
``admin_plans.py``. When that column is stale (e.g. a workspace upgraded to PRO
without the column being synced — see the kagura prod incident 2026-05-23), the
admin dialog's per-input "effective" preview is computed off a FREE-tier base
(1000) while the real effective is PRO-tier (100000), a 100x discrepancy.

This pins the fix: ``base.memory_limit`` must reflect ``plan_tier.memory_limit``,
consistent with the eight sibling fields.

Hits a real Postgres test DB because the bug is a field-by-field source mismatch
that mock-DB tests in ``tests/api/test_admin_plans*.py`` cannot detect.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import get_workspace_quotas
from config.plan_tiers import get_plan_tier

from ._admin_helpers import make_user, make_workspace, mock_admin


@pytest_asyncio.fixture
async def pro_workspace_stale_memory_limit(db_session: AsyncSession) -> dict:
    """A ``pro`` workspace whose ``memory_limit`` column holds the stale FREE value.

    ``make_workspace`` defaults to ``plan_name="pro"`` and ``memory_limit=1000``
    — exactly the drift the kagura prod incident exposed (PRO plan, FREE-tier
    column value).
    """
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id, plan_name="pro")
    assert ws.memory_limit == 1000, "fixture premise: column carries the stale FREE value"
    db_session.add(ws)
    await db_session.commit()

    return {"workspace_id": str(ws.id)}


class TestBaseMemoryLimitReadsPlanTier:
    """``base.memory_limit`` must come from the plan tier, not the stale column (#801)."""

    @pytest.mark.asyncio
    async def test_base_memory_limit_reflects_plan_tier_not_stale_column(
        self,
        db_session: AsyncSession,
        pro_workspace_stale_memory_limit: dict,
    ) -> None:
        result = await get_workspace_quotas(
            workspace_id=pro_workspace_stale_memory_limit["workspace_id"],
            admin_user=mock_admin(),
            db=db_session,
        )

        pro_tier = get_plan_tier("pro")
        assert pro_tier.memory_limit != 1000, (
            "test premise: PRO tier memory_limit must differ from the stale 1000 "
            "column value, otherwise this test proves nothing"
        )
        assert result.base.memory_limit == pro_tier.memory_limit, (
            "base.memory_limit must read plan_tier.memory_limit, not the stale "
            "Workspace.memory_limit column (#801: 100x dialog preview bug)"
        )

    @pytest.mark.asyncio
    async def test_effective_memory_limit_equals_base_plus_addon(
        self,
        db_session: AsyncSession,
        pro_workspace_stale_memory_limit: dict,
    ) -> None:
        """Spot-check the preview invariant: effective == base + addon (#801)."""
        result = await get_workspace_quotas(
            workspace_id=pro_workspace_stale_memory_limit["workspace_id"],
            admin_user=mock_admin(),
            db=db_session,
        )

        assert (
            result.effective.memory_limit == result.base.memory_limit + result.addon.memory_bonus
        ), "effective.memory_limit must equal base + addon once base reads plan_tier (#801)"
