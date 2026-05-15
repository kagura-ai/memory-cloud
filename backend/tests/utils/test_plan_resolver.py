"""Tests for utils.plan_resolver (Issue #661)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from config.plan_tiers import PlanName
from utils.plan_resolver import get_user_effective_plan


def _mock_db(plan_names: list[str]):
    """Build a mock AsyncSession whose .execute().scalars().all() returns plan_names."""
    db = MagicMock()
    db.execute = AsyncMock()

    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=plan_names)

    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars_result)

    db.execute.return_value = execute_result
    return db


@pytest.mark.asyncio
async def test_zero_workspaces_returns_free():
    """User with no owned workspaces defaults to FREE."""
    db = _mock_db([])
    plan = await get_user_effective_plan(db, "user-1")
    assert plan == PlanName.FREE


@pytest.mark.asyncio
async def test_single_free_workspace_returns_free():
    """Single Free owned workspace → FREE."""
    db = _mock_db([PlanName.FREE])
    plan = await get_user_effective_plan(db, "user-1")
    assert plan == PlanName.FREE


@pytest.mark.asyncio
async def test_single_pro_workspace_returns_pro():
    """Single Pro owned workspace → PRO."""
    db = _mock_db([PlanName.PRO])
    plan = await get_user_effective_plan(db, "user-1")
    assert plan == PlanName.PRO


@pytest.mark.asyncio
async def test_mixed_free_and_basic_returns_basic():
    """Free + Basic owned → BASIC (highest)."""
    db = _mock_db([PlanName.FREE, PlanName.BASIC])
    plan = await get_user_effective_plan(db, "user-1")
    assert plan == PlanName.BASIC


@pytest.mark.asyncio
async def test_mixed_basic_and_pro_returns_pro():
    """Basic + Pro owned → PRO."""
    db = _mock_db([PlanName.BASIC, PlanName.PRO])
    plan = await get_user_effective_plan(db, "user-1")
    assert plan == PlanName.PRO


@pytest.mark.asyncio
async def test_all_three_tiers_returns_pro():
    """Free + Basic + Pro owned → PRO."""
    db = _mock_db([PlanName.FREE, PlanName.BASIC, PlanName.PRO])
    plan = await get_user_effective_plan(db, "user-1")
    assert plan == PlanName.PRO


@pytest.mark.asyncio
async def test_unknown_plan_name_ranks_below_free():
    """Unknown plan_name values cannot silently win against FREE.

    Defensive case: if a row has a corrupted/unknown plan_name (e.g. a
    legacy ``enterprise`` from the older UserPlan model leaking in, or
    a manual DB edit), it must NOT outrank a valid FREE workspace.
    """
    db = _mock_db(["enterprise", PlanName.FREE])
    plan = await get_user_effective_plan(db, "user-1")
    assert plan == PlanName.FREE


@pytest.mark.asyncio
async def test_only_unknown_plan_name_is_returned_as_is():
    """If all owned workspaces have unknown plan_names, the function
    returns one of them rather than masking the data with FREE.

    This surfaces corrupted data to callers rather than silently
    treating it as Free; the caller's ``get_plan_tier`` lookup will
    then raise, making the corruption visible instead of degrading
    quietly to Free permissions.
    """
    db = _mock_db(["mystery-tier"])
    plan = await get_user_effective_plan(db, "user-1")
    assert plan == "mystery-tier"
