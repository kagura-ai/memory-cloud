"""Cross-table addon invariant helper for integration tests (#665).

Reusable contract assertion for the SSoT relationship documented in
``AddonCalculatorService`` (Issue #570):

    SUM(WorkspaceAddon active rows) × ADDON_UNIT_VALUES[addon_type]
    == workspace.addon_<addon_type>_bonus

Future quota-related PRs can call ``assert_addon_invariant(db, ws_id)``
after any code path that mutates ``WorkspaceAddon`` rows to confirm the
cache column was refreshed in lock-step. Originally added to satisfy
Issue #665 AC #3.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.admin_plans import _ADDON_FIELD_SPECS
from models.auth import Workspace
from models.resource import WorkspaceAddon
from services.addon_calculator_service import ADDON_UNIT_VALUES
from utils.datetime import utcnow

# Map ``Workspace.addon_*_bonus`` cache column → ``WorkspaceAddon.addon_type``,
# derived from the single source of truth in ``admin_plans._ADDON_FIELD_SPECS``
# so that adding a new addon type in one place propagates to this helper
# automatically — no manual sync, no drift surface.
_CACHE_TO_ADDON_TYPE: dict[str, str] = {
    spec.field_name: spec.addon_type for spec in _ADDON_FIELD_SPECS
}


async def assert_addon_invariant(db: AsyncSession, workspace_id: UUID) -> None:
    """Assert SUM(active WorkspaceAddon rows) matches each cache column.

    Args:
        db: AsyncSession bound to the test database.
        workspace_id: Workspace to verify.

    Raises:
        AssertionError: On any cache vs source mismatch, with the
            offending column and both values in the message so the
            failure points at the addon type that drifted.
    """
    now = utcnow()

    ws_result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = ws_result.scalar_one_or_none()
    assert workspace is not None, f"Workspace {workspace_id} not found"

    for cache_col, addon_type in _CACHE_TO_ADDON_TYPE.items():
        sum_result = await db.execute(
            select(func.coalesce(func.sum(WorkspaceAddon.quantity), 0)).where(
                WorkspaceAddon.workspace_id == workspace_id,
                WorkspaceAddon.addon_type == addon_type,
                WorkspaceAddon.active_from <= now,
                ((WorkspaceAddon.active_until.is_(None)) | (WorkspaceAddon.active_until > now)),
            )
        )
        total_quantity = int(sum_result.scalar() or 0)
        expected_bonus = total_quantity * ADDON_UNIT_VALUES[addon_type]
        actual_bonus = getattr(workspace, cache_col)

        assert actual_bonus == expected_bonus, (
            f"Addon invariant violation: workspace.{cache_col}={actual_bonus} "
            f"but SUM(WorkspaceAddon.quantity WHERE addon_type={addon_type!r}) "
            f"× {ADDON_UNIT_VALUES[addon_type]} = {expected_bonus}. "
            f"Some caller mutated WorkspaceAddon rows without invoking "
            f"AddonCalculatorService.recalculate_workspace_bonuses afterward "
            f"(see #570 contract docstring)."
        )
