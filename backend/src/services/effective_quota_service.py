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
                "mcp_calls_per_week": int,
                "rest_calls_per_day": int,
                "rest_calls_per_week": int,
                "public_calls_per_day": int,
                "public_calls_per_week": int,
                "max_members": int,
                "max_contexts": int,
                "analysis_runs_per_day": int,  # Issue #494
                "storage_bytes_limit": int,  # Issue #485
                "sleep_enabled_contexts_limit": int  # Issue #560
            }

        Raises:
            ValueError: If workspace not found
        """
        # Pure read: SELECT the workspace and compute effective quotas from
        # cached `addon_*_bonus` columns. Issue #570: removed the lazy
        # self-heal recalc that COMMITted from this GET path. Any code that
        # mutates `WorkspaceAddon` rows must call
        # `AddonCalculatorService.recalculate_workspace_bonuses(workspace_id)`
        # post-mutation to keep the cache consistent.
        result = await self.db.execute(select(Workspace).where(Workspace.id == workspace_id))
        workspace = result.scalar_one_or_none()

        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")

        # Calculate effective quotas via model properties
        # Issue #198 (Bug C): include weekly fields so callers don't need to fall
        # back on the broken `daily * 7` heuristic.
        effective_quotas = {
            "memory_limit": workspace.effective_memory_limit,
            "mcp_calls_per_day": workspace.effective_mcp_calls_per_day,
            "mcp_calls_per_week": workspace.effective_mcp_calls_per_week,
            "rest_calls_per_day": workspace.effective_rest_calls_per_day,
            "rest_calls_per_week": workspace.effective_rest_calls_per_week,
            "public_calls_per_day": workspace.effective_public_calls_per_day,
            "public_calls_per_week": workspace.effective_public_calls_per_week,
            "max_members": workspace.effective_max_members,
            "max_contexts": workspace.effective_max_contexts,
            "analysis_runs_per_day": workspace.effective_analysis_runs_per_day,
            "storage_bytes_limit": workspace.effective_storage_limit_bytes,
            "sleep_enabled_contexts_limit": workspace.effective_sleep_enabled_contexts_limit,
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
                "addon_member_bonus": int,
                "addon_context_bonus": int,
                "addon_analysis_bonus": int,  # Issue #494
                "addon_sleep_contexts_bonus": int  # Issue #560
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
            "addon_context_bonus": workspace.addon_context_bonus,
            "addon_analysis_bonus": workspace.addon_analysis_bonus,
            "addon_sleep_contexts_bonus": workspace.addon_sleep_contexts_bonus,
        }
