"""Tests for utils.plan_resolver (#674 sub-A, #675).

Pure unit tests — no DB, no live settings. The single ``execute()``
call inside ``get_user_workspace_cap_summary`` is mocked so each
test fully owns the ``(owned_count, cap)`` output.

The mock simulates ``result.one_or_none()`` on the JOIN query: it
returns a Row-shaped object whose attribute access via
``row.owned_count`` and ``row.workspace_slot_bonus`` matches the
SQLAlchemy result interface used in the helper. The helper then
computes ``cap = 1 + workspace_slot_bonus`` internally.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.plan_resolver import get_user_workspace_cap_summary


def _mock_db(owned_count: int | None, slot_bonus: int = 0):
    """Mock AsyncSession.execute returning a Row(owned_count, slot_bonus).

    Set ``owned_count=None`` to simulate the missing-user case where
    ``one_or_none()`` returns ``None``.
    """
    db = MagicMock()
    db.execute = AsyncMock()
    execute_result = MagicMock()
    if owned_count is None:
        execute_result.one_or_none = MagicMock(return_value=None)
    else:
        row = MagicMock()
        row.owned_count = owned_count
        row.workspace_slot_bonus = slot_bonus
        execute_result.one_or_none = MagicMock(return_value=row)
    db.execute.return_value = execute_result
    return db


@pytest.mark.asyncio
async def test_no_workspaces_no_bonus_returns_base_cap():
    """Brand-new user: 0 owned, 0 bonus → cap = 1 (base)."""
    db = _mock_db(owned_count=0, slot_bonus=0)
    count, cap = await get_user_workspace_cap_summary(db, "user-1")
    assert count == 0
    assert cap == 1


@pytest.mark.asyncio
async def test_one_owned_no_bonus_at_cap():
    """Base case: 1 owned workspace, 0 bonus → count == cap."""
    db = _mock_db(owned_count=1, slot_bonus=0)
    count, cap = await get_user_workspace_cap_summary(db, "user-1")
    assert count == 1
    assert cap == 1


@pytest.mark.asyncio
async def test_grandfathered_five_owned_bonus_four_at_cap():
    """Grandfather case: 5 owned, bonus=4 → cap = 5, at cap."""
    db = _mock_db(owned_count=5, slot_bonus=4)
    count, cap = await get_user_workspace_cap_summary(db, "user-1")
    assert count == 5
    assert cap == 5


@pytest.mark.asyncio
async def test_admin_granted_bonus_no_workspaces_yet():
    """Phase 1 admin grant before user creates: 0 owned, 3 bonus → cap 4."""
    db = _mock_db(owned_count=0, slot_bonus=3)
    count, cap = await get_user_workspace_cap_summary(db, "user-1")
    assert count == 0
    assert cap == 4


@pytest.mark.asyncio
async def test_missing_user_returns_zero_owned_base_cap():
    """Defensive: helper returns ``(0, 1)`` if the User row is not found.

    Theoretically unreachable because the caller has already passed
    authentication, but a fail-safe default avoids crashing the gate
    or the dashboard if something upstream returns a stale user_id.
    """
    db = _mock_db(owned_count=None)
    count, cap = await get_user_workspace_cap_summary(db, "user-1")
    assert count == 0
    assert cap == 1
