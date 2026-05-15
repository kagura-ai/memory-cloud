"""Tests for utils.plan_resolver (Issue #661).

Pure unit tests — no DB, no live settings. The single ``execute()``
call inside ``get_user_workspace_summary`` is mocked so each test
fully owns the (owned_count, plan_name) input/output.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from config.plan_tiers import PlanName
from utils.plan_resolver import get_user_workspace_summary


def _mock_db(plan_names: list[str]):
    """Mock AsyncSession whose execute().scalars().all() returns plan_names."""
    db = MagicMock()
    db.execute = AsyncMock()

    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=plan_names)

    execute_result = MagicMock()
    execute_result.scalars = MagicMock(return_value=scalars_result)

    db.execute.return_value = execute_result
    return db


@pytest.mark.asyncio
async def test_zero_workspaces_returns_count_zero_and_free():
    """User with no owned workspaces → (0, FREE)."""
    db = _mock_db([])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 0
    assert plan == PlanName.FREE


@pytest.mark.asyncio
async def test_single_free_workspace():
    """Single Free owned workspace → (1, FREE)."""
    db = _mock_db([PlanName.FREE])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 1
    assert plan == PlanName.FREE


@pytest.mark.asyncio
async def test_single_pro_workspace():
    """Single Pro owned workspace → (1, PRO)."""
    db = _mock_db([PlanName.PRO])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 1
    assert plan == PlanName.PRO


@pytest.mark.asyncio
async def test_mixed_free_and_basic_returns_basic():
    """Free + Basic owned → (2, BASIC)."""
    db = _mock_db([PlanName.FREE, PlanName.BASIC])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 2
    assert plan == PlanName.BASIC


@pytest.mark.asyncio
async def test_mixed_basic_and_pro_returns_pro():
    """Basic + Pro owned → (2, PRO)."""
    db = _mock_db([PlanName.BASIC, PlanName.PRO])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 2
    assert plan == PlanName.PRO


@pytest.mark.asyncio
async def test_all_three_tiers_returns_pro():
    """Free + Basic + Pro owned → (3, PRO)."""
    db = _mock_db([PlanName.FREE, PlanName.BASIC, PlanName.PRO])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 3
    assert plan == PlanName.PRO


@pytest.mark.asyncio
async def test_unknown_plan_name_does_not_outrank_free():
    """Unknown plan_name cannot silently win against FREE.

    Defensive case: a corrupted/unknown plan_name (e.g. legacy
    ``"enterprise"`` from the older UserPlan model, or a manual DB
    edit) must NOT outrank a valid FREE workspace. The known FREE
    still wins.
    """
    db = _mock_db(["enterprise", PlanName.FREE])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 2
    assert plan == PlanName.FREE


@pytest.mark.asyncio
async def test_only_unknown_plan_names_fallback_to_free():
    """If ALL owned workspaces have unknown plan_names, the resolver
    falls back to FREE rather than returning the unknown string.

    The unknown string would otherwise crash ``get_plan_tier`` in
    downstream callers (Issue #661 Reviewer 2 finding [C1]). The
    fallback also emits a structured warn log (see resolver source);
    we exercise the return-value path here because structlog's
    pytest-capture behaviour depends on the global logger setup and
    pinning on log content makes this test brittle.
    """
    db = _mock_db(["mystery-tier"])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 1
    assert plan == PlanName.FREE


@pytest.mark.asyncio
async def test_only_unknown_plan_names_count_reflects_real_rows():
    """Fallback path still returns the actual owned-row count, not 0.

    Important for the dashboard: a user with 3 corrupted-tier rows
    must see ``used=3`` so they know workspaces exist, even though
    the resolved tier degrades to FREE.
    """
    db = _mock_db(["mystery-1", "mystery-2", "mystery-3"])
    count, plan = await get_user_workspace_summary(db, "user-1")
    assert count == 3
    assert plan == PlanName.FREE
