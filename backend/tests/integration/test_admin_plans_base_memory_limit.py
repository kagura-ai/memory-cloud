"""Regression test for #801 / #805 — base.memory_limit must read plan_tier.

``GET /admin/plans/workspaces/{id}/quotas`` builds its ``base`` ``QuotaBreakdown``
from the plan tier definition for every dimension. Pre-#801, ``memory_limit`` was
the lone exception: it read the per-workspace ``Workspace.memory_limit`` cache
column, which could drift from the tier (the kagura prod incident 2026-05-23 — a
PRO workspace carrying the stale FREE value 1000, a 100x dialog-preview bug).

#801 fixed the read to use ``plan_tier.memory_limit``; **#805 then dropped the
``Workspace.memory_limit`` column entirely**, so the drift is now structurally
impossible — there is no column left to go stale. This test pins the surviving
contract: ``base.memory_limit`` reflects ``plan_tier.memory_limit``, consistent
with the eight sibling fields, and ``effective == base + addon``.

Hits a real Postgres test DB because the original bug was a field-by-field source
mismatch that mock-DB tests in ``tests/api/test_admin_plans*.py`` cannot detect.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import get_workspace_quotas
from config.plan_tiers import get_plan_tier

from ._admin_helpers import make_user, make_workspace, mock_admin


@pytest_asyncio.fixture
async def quota_workspace(db_session: AsyncSession, request) -> dict:
    """A workspace on a given plan (no per-workspace memory_limit column post-#805).

    Parametrize ``request.param`` with the plan name to exercise; defaults to
    ``pro`` when used without ``indirect`` parametrization.
    """
    plan_name = getattr(request, "param", "pro")
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    ws = make_workspace(owner_user_id=user.user_id, plan_name=plan_name)
    db_session.add(ws)
    await db_session.commit()

    return {"workspace_id": str(ws.id), "plan_name": plan_name}


class TestBaseMemoryLimitReadsPlanTier:
    """``base.memory_limit`` must come from the plan tier (#801/#805)."""

    # BASIC (10000) and PRO (100000) both differ from the old stale FREE value
    # (1000), so each distinguishes a tier-read from the legacy column-read.
    # FREE is omitted: its tier value IS 1000, so it cannot tell the two sources
    # apart (degenerate).
    @pytest.mark.parametrize("quota_workspace", ["basic", "pro"], indirect=True)
    @pytest.mark.asyncio
    async def test_base_memory_limit_reflects_plan_tier(
        self,
        db_session: AsyncSession,
        quota_workspace: dict,
    ) -> None:
        result = await get_workspace_quotas(
            workspace_id=quota_workspace["workspace_id"],
            admin_user=mock_admin(),
            db=db_session,
        )

        tier = get_plan_tier(quota_workspace["plan_name"])
        assert tier.memory_limit != 1000, (
            "test premise: this tier's memory_limit must differ from the old "
            "1000 column default, otherwise this test proves nothing"
        )
        assert result.base.memory_limit == tier.memory_limit, (
            "base.memory_limit must read plan_tier.memory_limit, consistent with "
            "the sibling dimensions (#801 fix; #805 dropped the column entirely)"
        )

    @pytest.mark.asyncio
    async def test_effective_memory_limit_tracks_plan_tier_base(
        self,
        db_session: AsyncSession,
        quota_workspace: dict,
    ) -> None:
        """Preview invariant: effective == base + addon, with base from the tier.

        The default fixture has no addon (addon.memory_bonus == 0), so this both
        pins effective == base and proves base tracks the tier.
        """
        result = await get_workspace_quotas(
            workspace_id=quota_workspace["workspace_id"],
            admin_user=mock_admin(),
            db=db_session,
        )

        pro_tier = get_plan_tier("pro")
        assert result.base.memory_limit == pro_tier.memory_limit
        assert (
            result.effective.memory_limit == result.base.memory_limit + result.addon.memory_bonus
        ), "effective.memory_limit must equal base + addon, base reading plan_tier (#801)"

    @pytest.mark.asyncio
    async def test_effective_includes_nonzero_addon_on_tier_base(
        self,
        db_session: AsyncSession,
    ) -> None:
        """effective == tier base + addon when a non-zero addon is present (#801).

        Exercises the addon term explicitly (the parametrized fixtures carry
        addon=0). ``effective_memory_limit`` is ``_zero_floor(plan_tier, addon)``
        and ``base`` reads the tier, so a PRO workspace with a 20000 memory addon
        must report base=100000, addon=20000, effective=120000.
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
            "effective must add the non-zero addon to the tier base (#801)"
        )

    # The DROP COLUMN itself (workspaces.memory_limit removed, reversibly) is
    # validated by tests/integration/test_alembic_migrations.py, which applies
    # the full chain incl. e27_805 on an ephemeral DB and exercises downgrade.
