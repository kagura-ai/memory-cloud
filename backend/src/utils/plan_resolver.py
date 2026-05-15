"""User-level plan resolution from owned workspaces.

Issue #661: Determines a user's effective plan tier by inspecting their
owned (deleted_at IS NULL) workspaces and returning the highest-tier
plan_name. Used for cross-workspace quotas like ``max_owned_workspaces``
where the tier must be derived from the user's holdings rather than a
single workspace.

Why "highest tier among owned" and not ``UserPlan.plan_name``:
    ``UserPlan`` (models/auth.py) defaults to ``free`` and is not
    currently updated by Stripe billing — only ``Workspace.plan_name``
    is (see ``services/stripe_service._apply_plan_change``). So the
    workspace-level plan column is the canonical billing source of
    truth, and the user's effective tier is the highest one across
    their owned workspaces.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import PlanName
from models.auth import Workspace
from utils.logger import get_logger

logger = get_logger(__name__)

# Tier ranking — higher wins. Aligned with PlanName values.
# Unknown plan_name values rank below FREE (-1) so degenerate data
# cannot accidentally elevate a user's effective tier.
_TIER_RANK: dict[str, int] = {
    PlanName.FREE: 0,
    PlanName.BASIC: 1,
    PlanName.PRO: 2,
}


async def get_user_effective_plan(db: AsyncSession, user_id: str) -> str:
    """Return the user's effective plan_name based on owned workspaces.

    Algorithm:
        1. Query all ``Workspace.plan_name`` rows where the user is the
           owner and the workspace is not soft-deleted.
        2. Return the highest-tier plan_name per ``_TIER_RANK``.
        3. Return ``PlanName.FREE`` when the user owns zero workspaces.

    Args:
        db: Async database session.
        user_id: User ID to resolve.

    Returns:
        Plan name string (one of ``PlanName`` values). Defaults to
        ``PlanName.FREE`` when the user has no owned workspaces.
    """
    result = await db.execute(
        select(Workspace.plan_name).where(
            Workspace.owner_user_id == user_id,
            Workspace.deleted_at.is_(None),
        )
    )
    plan_names = list(result.scalars().all())

    if not plan_names:
        return PlanName.FREE

    return max(plan_names, key=lambda p: _TIER_RANK.get(p, -1))
