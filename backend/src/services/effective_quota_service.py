"""Effective Quota Service for Variable Quotas.

Issue #238: Implements effective quota calculation (Base + Addons).

Responsibilities:
- Calculate effective quotas from plan tier + active addons
- Support variable quotas via addon purchases
- Separate MCP/REST/Public API quotas
- Cache quota calculations for performance
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import get_plan_tier
from models.auth import Workspace
from utils.logger import get_logger

logger = get_logger(__name__)


class EffectiveQuotaService:
    """Service for calculating effective quotas (Base + Addons).

    Issue #238: Variable quota system with addon support.

    Effective quota = Base quota (from plan tier) + Addon bonuses
    """

    def __init__(self, db: AsyncSession):
        """Initialize effective quota service.

        Args:
            db: Database session
        """
        self.db = db

    async def get_effective_quotas(self, workspace_id: UUID) -> dict[str, int]:
        """Get effective quotas for workspace.

        Calculates: Base (plan tier) + Addons

        Args:
            workspace_id: Workspace ID

        Returns:
            Dictionary with effective quotas:
            {
                "memory_limit": int,
                "mcp_calls_per_day": int,
                "rest_calls_per_day": int,
                "public_calls_per_day": int,
                "max_members": int,
                "max_contexts": int
            }

        Raises:
            ValueError: If workspace not found
        """
        # Get workspace with addon bonuses
        result = await self.db.execute(select(Workspace).where(Workspace.id == workspace_id))
        workspace = result.scalar_one_or_none()

        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")

        # Check if addon bonuses need to be calculated
        # If all bonuses are 0, recalculate from active addons
        if (
            workspace.addon_memory_bonus == 0
            and workspace.addon_mcp_quota_bonus == 0
            and workspace.addon_rest_quota_bonus == 0
            and workspace.addon_public_quota_bonus == 0
            and workspace.addon_member_bonus == 0
        ):
            # Import here to avoid circular dependency
            from services.addon_calculator_service import AddonCalculatorService

            calculator = AddonCalculatorService(self.db)
            await calculator.recalculate_workspace_bonuses(workspace_id)

            # Re-fetch workspace with updated bonuses
            result = await self.db.execute(select(Workspace).where(Workspace.id == workspace_id))
            workspace = result.scalar_one_or_none()

        # Get base quotas from plan tier
        plan_tier = get_plan_tier(workspace.plan_name)

        # Calculate effective quotas (base + addons)
        effective_quotas = {
            # Memory
            "memory_limit": workspace.memory_limit + workspace.addon_memory_bonus,
            # API Quotas (separated by type)
            "mcp_calls_per_day": self._get_base_mcp_quota(plan_tier)
            + workspace.addon_mcp_quota_bonus,
            "rest_calls_per_day": self._get_base_rest_quota(plan_tier)
            + workspace.addon_rest_quota_bonus,
            "public_calls_per_day": self._get_base_public_quota(plan_tier)
            + workspace.addon_public_quota_bonus,
            # Team
            "max_members": plan_tier.max_members_per_workspace + workspace.addon_member_bonus,
            "max_contexts": plan_tier.max_contexts_per_workspace + workspace.addon_context_bonus,
        }

        logger.debug(
            "effective_quotas_calculated",
            extra={
                "workspace_id": str(workspace_id),
                "plan_name": workspace.plan_name,
                "effective_quotas": effective_quotas,
            },
        )

        return effective_quotas

    async def get_addon_summary(self, workspace_id: UUID) -> dict[str, int]:
        """Get summary of active addon bonuses.

        Args:
            workspace_id: Workspace ID

        Returns:
            Dictionary with addon bonuses:
            {
                "addon_memory_bonus": int,
                "addon_mcp_quota_bonus": int,
                "addon_rest_quota_bonus": int,
                "addon_public_quota_bonus": int,
                "addon_member_bonus": int
            }
        """
        result = await self.db.execute(select(Workspace).where(Workspace.id == workspace_id))
        workspace = result.scalar_one_or_none()

        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")

        return {
            "addon_memory_bonus": workspace.addon_memory_bonus,
            "addon_mcp_quota_bonus": workspace.addon_mcp_quota_bonus,
            "addon_rest_quota_bonus": workspace.addon_rest_quota_bonus,
            "addon_public_quota_bonus": workspace.addon_public_quota_bonus,
            "addon_member_bonus": workspace.addon_member_bonus,
        }

    # ========================================================================
    # Private Helper Methods
    # ========================================================================

    def _get_base_mcp_quota(self, plan_tier) -> int:
        """Get base MCP calls/day from plan tier.

        Args:
            plan_tier: Plan tier object

        Returns:
            Base MCP calls per day
        """
        return plan_tier.mcp_calls_per_day

    def _get_base_rest_quota(self, plan_tier) -> int:
        """Get base REST calls/day from plan tier.

        Args:
            plan_tier: Plan tier object

        Returns:
            Base REST calls per day
        """
        return plan_tier.rest_calls_per_day

    def _get_base_public_quota(self, plan_tier) -> int:
        """Get base Public REST calls/day from plan tier.

        Args:
            plan_tier: Plan tier object

        Returns:
            Base Public REST calls per day
        """
        return plan_tier.public_calls_per_day
