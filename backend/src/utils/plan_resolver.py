"""User-level plan resolution from owned workspaces.

Issue #661: Determines a user's effective plan tier by inspecting their
owned (deleted_at IS NULL) workspaces and returning the highest-tier
plan_name plus the count of owned rows. Used for cross-workspace quotas
like ``max_owned_workspaces`` where the tier must be derived from the
user's holdings rather than a single workspace.

Why "highest tier among owned" and not ``UserPlan.plan_name``:
    ``UserPlan`` (models/auth.py) defaults to ``free`` and is not
    currently updated by Stripe billing — only ``Workspace.plan_name``
    is (see ``services/stripe_service._apply_plan_change``). So the
    workspace-level plan column is the canonical billing source of
    truth, and the user's effective tier is the highest one across
    their owned workspaces.

Single-query design (Issue #661 review):
    ``get_user_workspace_summary`` returns both the owned count and
    the resolved plan in one SELECT. Both ``QuotaService`` (gate)
    and ``/usage/current`` (dashboard) need both values; folding them
    into one helper avoids duplicate queries and the small race
    window that two sequential SELECTs would expose.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import PlanName
from models.auth import Workspace
from utils.logger import get_logger

logger = get_logger(__name__)

# Tier ranking — higher wins. Aligned with PlanName values.
# Unknown plan_name values rank below FREE (-1) so degenerate data
# cannot accidentally elevate a user's effective tier during the
# ``max()`` selection inside ``get_user_workspace_summary``.
_TIER_RANK: dict[str, int] = {
    PlanName.FREE: 0,
    PlanName.BASIC: 1,
    PlanName.PRO: 2,
}


async def get_user_workspace_summary(db: AsyncSession, user_id: str) -> tuple[int, str]:
    """Return ``(owned_count, effective_plan_name)`` for the user.

    Algorithm:
        1. Single SELECT of all ``Workspace.plan_name`` rows where the
           user is the owner and the workspace is not soft-deleted.
        2. Count = number of returned rows.
        3. Effective plan = highest-tier plan_name per ``_TIER_RANK``.
        4. Empty result → ``(0, PlanName.FREE)``.

    Corruption guard:
        If the resolved plan_name is not a known ``PlanName`` member
        (e.g. legacy ``"enterprise"`` from the older UserPlan model
        leaking in, or a manual DB edit), this falls back to
        ``PlanName.FREE`` and emits a ``plan_resolver_unknown_plan_names``
        warning so the corruption surfaces in ops monitoring rather
        than crashing the user's dashboard or workspace-creation
        flow with ``ValueError`` from a downstream ``get_plan_tier``
        lookup.

    Args:
        db: Async database session.
        user_id: User ID to resolve.

    Returns:
        Tuple of (owned workspace count, plan_name string).
    """
    result = await db.execute(
        select(Workspace.plan_name).where(
            Workspace.owner_user_id == user_id,
            Workspace.deleted_at.is_(None),
        )
    )
    plan_names = list(result.scalars().all())
    count = len(plan_names)

    if not plan_names:
        return (0, PlanName.FREE)

    effective = max(plan_names, key=lambda p: _TIER_RANK.get(p, -1))

    if effective not in _TIER_RANK:
        logger.warning(
            "plan_resolver_unknown_plan_names",
            user_id=user_id,
            plan_names=plan_names,
            falling_back_to=PlanName.FREE,
        )
        return (count, PlanName.FREE)

    return (count, effective)
