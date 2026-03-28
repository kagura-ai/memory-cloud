"""Addon Calculator Service for workspace addon bonus calculation.

Issue #238: Calculates and updates cached addon bonus columns in workspaces table.

Responsibilities:
- Calculate sum of active addons per workspace
- Update workspace.addon_*_bonus columns
- Support for different addon types with unit values
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from models.resource import WorkspaceAddon
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Addon unit values (must match migration 048 documentation)
ADDON_UNIT_VALUES = {
    "extra_storage": 100,  # MB per unit
    "extra_memory": 10000,  # memories per unit
    "extra_mcp_quota": 5000,  # calls/day per unit
    "extra_rest_quota": 1000,  # calls/day per unit
    "extra_public_quota": 500,  # calls/day per unit
    "extra_members": 5,  # members per unit
}


class AddonCalculatorService:
    """Service for calculating workspace addon bonuses."""

    def __init__(self, db: AsyncSession):
        """Initialize addon calculator service.

        Args:
            db: Database session
        """
        self.db = db

    async def recalculate_workspace_bonuses(self, workspace_id: UUID) -> dict[str, int]:
        """Recalculate and update all addon bonuses for an workspace.

        Args:
            workspace_id: Workspace ID

        Returns:
            Dictionary of calculated bonuses
        """
        now = utcnow()

        # Fetch all active addons for this workspace
        result = await self.db.execute(
            select(WorkspaceAddon).where(
                WorkspaceAddon.workspace_id == workspace_id,
                WorkspaceAddon.active_from <= now,
                (WorkspaceAddon.active_until.is_(None) | (WorkspaceAddon.active_until > now)),
            )
        )
        active_addons = list(result.scalars().all())

        # Calculate bonuses
        bonuses = {
            "addon_storage_bonus_mb": 0,
            "addon_memory_bonus": 0,
            "addon_mcp_quota_bonus": 0,
            "addon_rest_quota_bonus": 0,
            "addon_public_quota_bonus": 0,
            "addon_member_bonus": 0,
        }

        for addon in active_addons:
            addon_type = addon.addon_type
            unit_value = ADDON_UNIT_VALUES.get(addon_type, 0)
            total_bonus = unit_value * addon.quantity

            # Map addon_type to bonus column
            if addon_type == "extra_storage":
                bonuses["addon_storage_bonus_mb"] += total_bonus
            elif addon_type == "extra_memory":
                bonuses["addon_memory_bonus"] += total_bonus
            elif addon_type == "extra_mcp_quota":
                bonuses["addon_mcp_quota_bonus"] += total_bonus
            elif addon_type == "extra_rest_quota":
                bonuses["addon_rest_quota_bonus"] += total_bonus
            elif addon_type == "extra_public_quota":
                bonuses["addon_public_quota_bonus"] += total_bonus
            elif addon_type == "extra_members":
                bonuses["addon_member_bonus"] += total_bonus

        # Update workspace table
        result = await self.db.execute(select(Workspace).where(Workspace.id == workspace_id))
        workspace = result.scalar_one_or_none()

        if not workspace:
            logger.error("workspace_not_found", workspace_id=workspace_id)
            return bonuses

        workspace.addon_storage_bonus_mb = bonuses["addon_storage_bonus_mb"]
        workspace.addon_memory_bonus = bonuses["addon_memory_bonus"]
        workspace.addon_mcp_quota_bonus = bonuses["addon_mcp_quota_bonus"]
        workspace.addon_rest_quota_bonus = bonuses["addon_rest_quota_bonus"]
        workspace.addon_public_quota_bonus = bonuses["addon_public_quota_bonus"]
        workspace.addon_member_bonus = bonuses["addon_member_bonus"]

        await self.db.commit()

        logger.info(
            "addon_bonuses_recalculated",
            workspace_id=workspace_id,
            active_addons=len(active_addons),
            bonuses=bonuses,
        )

        return bonuses

    async def recalculate_all_workspaces(self) -> int:
        """Recalculate addon bonuses for all workspaces.

        Useful for:
        - Initial migration
        - Periodic cleanup job
        - Manual correction

        Returns:
            Number of workspaces updated
        """
        result = await self.db.execute(select(Workspace.id))
        workspace_ids = [row[0] for row in result.all()]

        count = 0
        for workspace_id in workspace_ids:
            await self.recalculate_workspace_bonuses(workspace_id)
            count += 1

        logger.info("addon_bonuses_recalculated_all", workspaces=count)
        return count
