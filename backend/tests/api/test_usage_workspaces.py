"""Tests for the user-level workspace cap field in /api/v1/usage/current.

Issue #675 (epic #674 sub-A): cap is now ``1 + users.workspace_slot_bonus``.
The helper ``_build_workspaces_usage`` issues a single SELECT via
``get_user_workspace_cap_summary`` which returns ``(owned_count, slot_bonus)``.
Tests mock exactly one ``execute()`` call returning a Row with those
two attribute fields.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.usage import _build_workspaces_usage


def _mock_db(owned_count: int, slot_bonus: int):
    """Mock AsyncSession whose execute() returns a Row(owned_count, slot_bonus)."""
    db = MagicMock()
    db.execute = AsyncMock()
    row = MagicMock()
    row.owned_count = owned_count
    row.workspace_slot_bonus = slot_bonus
    result = MagicMock()
    result.one_or_none = MagicMock(return_value=row)
    db.execute.return_value = result
    return db


@pytest.mark.asyncio
async def test_zero_owned_no_bonus():
    """0 owned, 0 bonus → used=0, limit=1, remaining=1 (base cap)."""
    db = _mock_db(owned_count=0, slot_bonus=0)
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 0
    assert result.limit == 1
    assert result.remaining == 1


@pytest.mark.asyncio
async def test_one_owned_no_bonus_at_cap():
    """1 owned, 0 bonus → used=1, limit=1, remaining=0."""
    db = _mock_db(owned_count=1, slot_bonus=0)
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 1
    assert result.limit == 1
    assert result.remaining == 0


@pytest.mark.asyncio
async def test_two_owned_bonus_two_partial():
    """2 owned, 2 bonus → used=2, limit=3, remaining=1."""
    db = _mock_db(owned_count=2, slot_bonus=2)
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 2
    assert result.limit == 3
    assert result.remaining == 1


@pytest.mark.asyncio
async def test_grandfathered_five_owned_bonus_four_at_cap():
    """Migration grandfather: 5 owned, 4 bonus → used=5, limit=5, remaining=0."""
    db = _mock_db(owned_count=5, slot_bonus=4)
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 5
    assert result.limit == 5
    assert result.remaining == 0


@pytest.mark.asyncio
async def test_admin_granted_bonus_no_workspaces():
    """Phase 1 admin pre-grant: 0 owned, 9 bonus → cap=10, all remaining."""
    db = _mock_db(owned_count=0, slot_bonus=9)
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 0
    assert result.limit == 10
    assert result.remaining == 10


@pytest.mark.asyncio
async def test_remaining_never_negative():
    """Pathological over-cap (3 owned, 0 bonus → cap=1) clamps remaining to 0.

    Surfaces what the dashboard shows if a user's bonus is reduced below
    their current ownership (e.g. admin error). The cap-creation gate
    prevents *adding* more, but display must not go negative.
    """
    db = _mock_db(owned_count=3, slot_bonus=0)
    result = await _build_workspaces_usage(db, "user-1")
    assert result.used == 3
    assert result.limit == 1
    assert result.remaining == 0  # clamped via max(0, ...)
