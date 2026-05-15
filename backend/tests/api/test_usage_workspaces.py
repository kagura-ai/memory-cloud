"""Tests for the user-level workspace cap field in /api/v1/usage/current (Issue #661).

Focused unit tests for ``_build_workspaces_usage`` (the helper that
populates ``CurrentUsage.workspaces``). The helper takes an async db
session and a user_id, and returns a ``WorkspacesUsage`` with
``used`` (owned count), ``limit`` (tier cap), and ``remaining``.

These tests mock the two DB calls the helper issues:
  1. SELECT count(Workspace.id) WHERE owner_user_id = X AND deleted_at IS NULL
  2. plan_resolver's SELECT Workspace.plan_name (same WHERE)

Tier→cap mapping (Free=1 / Basic=3 / Pro=10) is exercised through the
real ``PLAN_TIERS`` so this file also pins that mapping for the
user-facing /usage/current response.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.usage import _build_workspaces_usage
from config.plan_tiers import PlanName


def _mock_db(owned_count: int, plan_names: list[str]):
    """Mock AsyncSession whose two execute() calls return:
    1) a count result with .scalar() == owned_count
    2) a plan_resolver result with .scalars().all() == plan_names
    """
    db = MagicMock()
    db.execute = AsyncMock()

    count_result = MagicMock()
    count_result.scalar = MagicMock(return_value=owned_count)

    plan_scalars = MagicMock()
    plan_scalars.all = MagicMock(return_value=plan_names)
    plan_result = MagicMock()
    plan_result.scalars = MagicMock(return_value=plan_scalars)

    db.execute.side_effect = [count_result, plan_result]
    return db


@pytest.mark.asyncio
async def test_free_user_zero_owned():
    """Free default, 0 owned → used=0, limit=1, remaining=1."""
    db = _mock_db(owned_count=0, plan_names=[])
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 0
    assert result.limit == 1
    assert result.remaining == 1


@pytest.mark.asyncio
async def test_free_user_at_cap():
    """Free with 1 owned → used=1, limit=1, remaining=0."""
    db = _mock_db(owned_count=1, plan_names=[PlanName.FREE])
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 1
    assert result.limit == 1
    assert result.remaining == 0


@pytest.mark.asyncio
async def test_basic_user_partial():
    """Basic with 2 owned → used=2, limit=3, remaining=1."""
    db = _mock_db(owned_count=2, plan_names=[PlanName.BASIC, PlanName.BASIC])
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 2
    assert result.limit == 3
    assert result.remaining == 1


@pytest.mark.asyncio
async def test_pro_user_at_cap():
    """Pro with 10 owned → used=10, limit=10, remaining=0."""
    db = _mock_db(owned_count=10, plan_names=[PlanName.PRO] * 10)
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 10
    assert result.limit == 10
    assert result.remaining == 0


@pytest.mark.asyncio
async def test_mixed_tier_uses_highest():
    """User with 1 Basic + 1 Pro owned → cap is 10 (Pro)."""
    db = _mock_db(owned_count=2, plan_names=[PlanName.BASIC, PlanName.PRO])
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 2
    assert result.limit == 10
    assert result.remaining == 8


@pytest.mark.asyncio
async def test_remaining_never_negative():
    """Pathological over-cap state (e.g. legacy data) → remaining clamped to 0.

    Documents what the dashboard surfaces if an existing Free user
    happens to be at 3 owned workspaces when the cap was lowered to 1
    (the pre-enforcement migration scenario Issue #661 plans for).
    """
    db = _mock_db(owned_count=3, plan_names=[PlanName.FREE])
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 3
    assert result.limit == 1
    assert result.remaining == 0  # clamped via max(0, ...)
