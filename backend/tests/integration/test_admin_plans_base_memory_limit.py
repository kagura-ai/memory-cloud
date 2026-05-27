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
async def stale_memory_limit_workspace(db_session: AsyncSession, request) -> dict:
    """A workspace whose ``memory_limit`` column holds the stale FREE value (1000).

    Parametrize ``request.param`` with the plan name to exercise; defaults to
    ``pro`` when used without ``indirect`` parametrization. ``make_workspace``
    seeds ``memory_limit=1000`` regardless of plan — exactly the drift the
    kagura prod incident exposed (non-FREE plan, FREE-tier column value).
    """
    plan_name = getattr(request, "param", "pro")
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id, plan_name=plan_name)
    assert ws.memory_limit == 1000, "fixture premise: column carries the stale FREE value"
    db_session.add(ws)
    await db_session.commit()

    return {"workspace_id": str(ws.id), "plan_name": plan_name}


class TestBaseMemoryLimitReadsPlanTier:
    """``base.memory_limit`` must come from the plan tier, not the stale column (#801)."""

    # BASIC (10000) and PRO (100000) both differ from the stale FREE value (1000),
    # so each distinguishes a tier-read from a column-read. FREE is omitted: its
    # tier value IS 1000, so it cannot tell the two sources apart (degenerate).
    @pytest.mark.parametrize("stale_memory_limit_workspace", ["basic", "pro"], indirect=True)
    @pytest.mark.asyncio
    async def test_base_memory_limit_reflects_plan_tier_not_stale_column(
        self,
        db_session: AsyncSession,
        stale_memory_limit_workspace: dict,
    ) -> None:
        result = await get_workspace_quotas(
            workspace_id=stale_memory_limit_workspace["workspace_id"],
            admin_user=mock_admin(),
            db=db_session,
        )

        tier = get_plan_tier(stale_memory_limit_workspace["plan_name"])
        assert tier.memory_limit != 1000, (
            "test premise: this tier's memory_limit must differ from the stale "
            "1000 column value, otherwise this test proves nothing"
        )
        assert result.base.memory_limit == tier.memory_limit, (
            "base.memory_limit must read plan_tier.memory_limit, not the stale "
            "Workspace.memory_limit column (#801: 100x dialog preview bug)"
        )

    @pytest.mark.asyncio
    async def test_effective_memory_limit_tracks_plan_tier_base(
        self,
        db_session: AsyncSession,
        stale_memory_limit_workspace: dict,
    ) -> None:
        """Preview invariant: effective == base + addon, with base from the tier.

        The default fixture has no addon (addon.memory_bonus == 0), so this both
        pins effective == base and proves base tracks the tier (not the stale
        column): pre-fix, base would be 1000 while effective is the PRO tier
        100000, breaking the equality.
        """
        result = await get_workspace_quotas(
            workspace_id=stale_memory_limit_workspace["workspace_id"],
            admin_user=mock_admin(),
            db=db_session,
        )

        pro_tier = get_plan_tier("pro")
        assert result.base.memory_limit == pro_tier.memory_limit
        assert (
            result.effective.memory_limit == result.base.memory_limit + result.addon.memory_bonus
        ), "effective.memory_limit must equal base + addon once base reads plan_tier (#801)"

    @pytest.mark.asyncio
    async def test_effective_includes_nonzero_addon_on_tier_base(
        self,
        db_session: AsyncSession,
    ) -> None:
        """effective == tier base + addon when a non-zero addon is present (#801).

        Exercises the addon term explicitly (the parametrized fixtures carry
        addon=0). ``effective_memory_limit`` is ``_zero_floor(plan_tier, addon)``
        and ``base`` now reads the tier, so a PRO workspace with a 20000 memory
        addon must report base=100000, addon=20000, effective=120000 — even with
        the stale 1000 cache column still on the row.
        """
        user = make_user()
        db_session.add(user)
        await db_session.flush()

        ws = make_workspace(owner_user_id=user.user_id, plan_name="pro")
        ws.addon_memory_bonus = 20_000  # valid multiple of perUnit (10000)
        db_session.add(ws)
        await db_session.commit()

        result = await get_workspace_quotas(
            workspace_id=str(ws.id),
            admin_user=mock_admin(),
            db=db_session,
        )

        pro_tier = get_plan_tier("pro")
        assert result.base.memory_limit == pro_tier.memory_limit
        assert result.addon.memory_bonus == 20_000
        assert result.effective.memory_limit == pro_tier.memory_limit + 20_000, (
            "effective must add the non-zero addon to the tier base, not the stale "
            "Workspace.memory_limit column (#801)"
        )
