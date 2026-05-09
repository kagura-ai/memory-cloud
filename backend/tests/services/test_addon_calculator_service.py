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

    @pytest.mark.asyncio
    async def test_recalculate_is_idempotent(self, service, mock_db, workspace_id):
        """Issue #570 invariant: ``recalculate_workspace_bonuses`` is idempotent.

        Calling the method twice with the same active-addon snapshot must
        produce the same bonus dict and the same persisted column values
        (no accumulation, no drift). This pins the SUM-from-source aggregate
        contract documented on ``AddonCalculatorService``'s class docstring,
        which is the safety basis for replacing the GET-time self-heal with
        explicit write-path invalidation. Concurrent recalcs (same source
        snapshot, different orderings) converge on the same final state.
        """
        addons = [
            _make_addon("extra_storage", quantity=4),
            _make_addon("extra_memory", quantity=2),
            _make_addon("extra_sleep_contexts", quantity=3),
        ]
        # Each call performs 2 execute()s (addons + workspace), so wire up
        # 4 results for two consecutive calls. The same workspace mock is
        # returned on the second pass — that's the shared cache row whose
        # idempotency we're proving.
        workspace = _make_workspace(workspace_id)
        mock_db.execute.side_effect = _make_side_effects(addons, workspace) + _make_side_effects(
            addons, workspace
        )

        first = await service.recalculate_workspace_bonuses(workspace_id)
        second = await service.recalculate_workspace_bonuses(workspace_id)

        assert first == second, "Bonus dict drifted between identical recalcs"
        assert workspace.addon_storage_bonus_mb == first["addon_storage_bonus_mb"]
        assert workspace.addon_memory_bonus == first["addon_memory_bonus"]
        assert workspace.addon_sleep_contexts_bonus == first["addon_sleep_contexts_bonus"]
        # SUM-from-source means the value equals the per-addon contribution
        # exactly — not a multiple of it from accidental accumulation.
        assert first["addon_storage_bonus_mb"] == 4 * ADDON_UNIT_VALUES["extra_storage"]
        assert first["addon_memory_bonus"] == 2 * ADDON_UNIT_VALUES["extra_memory"]
        assert first["addon_sleep_contexts_bonus"] == 3 * ADDON_UNIT_VALUES["extra_sleep_contexts"]
        assert mock_db.commit.await_count == 2
