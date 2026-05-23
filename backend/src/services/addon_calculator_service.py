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
    "extra_contexts": 5,  # contexts per unit
    "extra_analysis_runs": 1,  # Issue #494: +1 broadlistening run/day per unit
    "extra_sleep_contexts": 1,  # Issue #560: +1 sleep-enabled context per unit (PRO-only)
}


class AddonCalculatorService:
    """Service for calculating workspace addon bonuses.

    Cache-invalidation contract (Issue #570):
        ``Workspace.addon_*_bonus`` columns cache the SUM of active
        ``WorkspaceAddon`` rows. They are read by
        ``EffectiveQuotaService.get_effective_quotas`` on every quota
        check and exposed via ``Workspace.effective_*`` properties.

        Any code that mutates ``WorkspaceAddon`` rows
        (INSERT / UPDATE active_until / DELETE / future Stripe webhook
        handlers / admin scripts / the admin HTTP handler
        ``PUT /admin/plans/workspaces/{id}/quotas``) **MUST** call
        ``recalculate_workspace_bonuses(workspace_id)`` after staging
        the mutation in the session (``db.add(...)`` / ``db.delete(...)``).
        Skipping the call leaves the cache stale and every downstream
        quota check returns wrong numbers until something else
        triggers a recalc.

        Provenance discriminator (Issue #665):
            ``WorkspaceAddon.source`` distinguishes ``'stripe'`` rows
            (purchase flow) from ``'admin_grant'`` rows (admin manual
            override). The composite UNIQUE
            ``(workspace_id, addon_type, source)`` ensures admin grants
            and Stripe purchases co-exist without overwriting each other
            — this method's SUM aggregation reads BOTH sources, so the
            cache always reflects the union. The admin handler
            UPSERTs the ``(workspace_id, addon_type, 'admin_grant')``
            row and then calls this method, exactly as Stripe webhooks
            will when they land.

        Commit semantics: ``recalculate_workspace_bonuses`` calls
        ``db.commit()`` internally, which flushes BOTH the caller's
        staged ``WorkspaceAddon`` mutation AND the recomputed
        ``addon_*_bonus`` columns in a single transaction. After
        the method returns, the session has no pending writes — so
        any subsequent commit, whether explicit or implicit (e.g.
        FastAPI's ``get_db`` dependency commits at request-end,
        ``db/base.py``), is a harmless no-op. The constraint this
        places on callers is composability: do not call this method
        from inside a larger explicit transaction you intend to
        commit or roll back as a unit, because the internal commit
        will finalize the addon write before the outer block has
        a chance to roll back. This atomic-on-recalc pairing is the
        safety basis for replacing the old GET-time self-heal with
        explicit write-path invalidation.

        ``recalculate_workspace_bonuses`` is idempotent: it computes
        each bonus column as a SUM-from-source aggregate and writes
        the absolute value, so concurrent recalcs converge on the
        same final state regardless of ordering.
    """

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
            "addon_context_bonus": 0,
            "addon_analysis_bonus": 0,
            "addon_sleep_contexts_bonus": 0,  # Issue #560
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
            elif addon_type == "extra_contexts":
                bonuses["addon_context_bonus"] += total_bonus
            elif addon_type == "extra_analysis_runs":
                bonuses["addon_analysis_bonus"] += total_bonus
            elif addon_type == "extra_sleep_contexts":
                bonuses["addon_sleep_contexts_bonus"] += total_bonus  # Issue #560
            else:
                # Issue #665 review-finding #3: a WorkspaceAddon row exists
                # for an addon_type that has no corresponding cache-column
                # mapping here. The row passes the check_addon_type CHECK
                # constraint, so it's a valid enum value the spec table
                # (admin_plans._ADDON_FIELD_SPECS) added without updating
                # this method. The contribution is silently dropped; cache
                # stays at 0; downstream quota check uses the base-tier
                # value as if the admin grant never happened. Log loudly
                # so the drift surfaces in production logs at the first
                # recalc for an affected workspace.
                # Cast UUID to str — utils/logger.py uses JSONRenderer in
                # production (LOG_COLORIZE=false) without a default=str
                # serializer, so raw UUID kwargs would fail JSON encoding
                # and the warning would silently drop. Copilot review #797.
                logger.warning(
                    "addon_type_no_bonus_column_mapping",
                    addon_type=addon_type,
                    workspace_id=str(workspace_id),
                    addon_id=addon.id,
                    quantity=addon.quantity,
                    note=(
                        "WorkspaceAddon row exists for an addon_type with "
                        "no entry in the if/elif chain above. The grant "
                        "will not appear in the workspace.addon_*_bonus "
                        "cache. Update both this method AND "
                        "admin_plans._ADDON_FIELD_SPECS when introducing "
                        "a new addon type."
                    ),
                )

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
        workspace.addon_context_bonus = bonuses["addon_context_bonus"]
        workspace.addon_analysis_bonus = bonuses["addon_analysis_bonus"]
        workspace.addon_sleep_contexts_bonus = bonuses["addon_sleep_contexts_bonus"]  # Issue #560

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
