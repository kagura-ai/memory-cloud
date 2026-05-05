"""Tests for AddonCalculatorService.

Issue #485 prerequisite: ``recalculate_workspace_bonuses`` writes
``workspace.addon_storage_bonus_mb`` (since the service was originally
authored alongside the ``extra_storage`` addon type), but the column
was never added to the ``Workspace`` model nor any prior migration.
The first commit of #485 closes that gap; these tests guard against
regression.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.addon_calculator_service import (
    ADDON_UNIT_VALUES,
    AddonCalculatorService,
)
from utils.datetime import utcnow


def _make_addon(addon_type: str, quantity: int):
    """Build a minimal active WorkspaceAddon mock."""
    addon = MagicMock()
    addon.addon_type = addon_type
    addon.quantity = quantity
    addon.active_from = utcnow() - timedelta(days=1)
    addon.active_until = None
    return addon


def _make_workspace(workspace_id):
    """Build a Workspace mock with all addon_*_bonus columns settable."""
    workspace = MagicMock()
    workspace.id = workspace_id
    workspace.addon_storage_bonus_mb = 0
    workspace.addon_memory_bonus = 0
    workspace.addon_mcp_quota_bonus = 0
    workspace.addon_rest_quota_bonus = 0
    workspace.addon_public_quota_bonus = 0
    workspace.addon_member_bonus = 0
    workspace.addon_context_bonus = 0
    workspace.addon_analysis_bonus = 0
    return workspace


def _make_side_effects(addons, workspace):
    """Return the 2-element side_effect list for db.execute.

    Call order inside ``recalculate_workspace_bonuses``:
      1. select(WorkspaceAddon) — active addons
      2. select(Workspace)      — target workspace row
    """
    addon_result = MagicMock()
    addon_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=addons)))

    workspace_result = MagicMock()
    workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

    return [addon_result, workspace_result]


class TestAddonCalculatorStorageBonus:
    """Persist ``addon_storage_bonus_mb`` without ``AttributeError`` (#485)."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return AddonCalculatorService(mock_db)

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_extra_storage_addon_bonus_is_persisted(self, service, mock_db, workspace_id):
        """Single ``extra_storage`` addon (qty=3) → 300 MB bonus column."""
        unit_mb = ADDON_UNIT_VALUES["extra_storage"]
        assert unit_mb == 100, "Unit value drift — review pricing assumptions"

        addons = [_make_addon("extra_storage", quantity=3)]
        workspace = _make_workspace(workspace_id)
        mock_db.execute.side_effect = _make_side_effects(addons, workspace)

        bonuses = await service.recalculate_workspace_bonuses(workspace_id)

        assert bonuses["addon_storage_bonus_mb"] == 300
        assert workspace.addon_storage_bonus_mb == 300
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_addons_yields_zero_storage_bonus(self, service, mock_db, workspace_id):
        """No active addons → zero bonus, no AttributeError on persist."""
        workspace = _make_workspace(workspace_id)
        mock_db.execute.side_effect = _make_side_effects([], workspace)

        bonuses = await service.recalculate_workspace_bonuses(workspace_id)

        assert bonuses["addon_storage_bonus_mb"] == 0
        assert workspace.addon_storage_bonus_mb == 0

    @pytest.mark.asyncio
    async def test_mixed_addons_isolate_storage_bonus(self, service, mock_db, workspace_id):
        """Storage + memory addons must not cross-pollinate bonus dimensions."""
        addons = [
            _make_addon("extra_storage", quantity=2),
            _make_addon("extra_memory", quantity=5),
        ]
        workspace = _make_workspace(workspace_id)
        mock_db.execute.side_effect = _make_side_effects(addons, workspace)

        bonuses = await service.recalculate_workspace_bonuses(workspace_id)

        assert bonuses["addon_storage_bonus_mb"] == 200  # 2 × 100 MB
        assert workspace.addon_storage_bonus_mb == 200
        assert bonuses["addon_memory_bonus"] == 5 * ADDON_UNIT_VALUES["extra_memory"]
