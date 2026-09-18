"""User-level workspace-cap resolution (#674 sub-A, #675, #1550).

Returns a ``WorkspaceCapSummary`` for a user where

    cap = BASE_CAP (1) + users.workspace_slot_bonus + tier_grant

and ``tier_grant`` is ``PlanTier.owned_workspace_grant`` of the HIGHEST
tier among the workspaces the user owns (#1550: free 0 / basic 0 /
pro 2 / promax 19 → 1 / 1 / 3 / 20 with a zero bonus). The formula is
held inside this module — ``resolve_workspace_cap`` is the only place
that computes it — so callers never assemble the cap themselves.

Why the tier grants slots instead of capping (#674 reasons still hold):
    Plan tier is a WORKSPACE property, the cap is a USER property. A
    grant composes with admin / referral slot bonuses and keeps one
    resolution site; a tier-derived hard cap would need its own
    downgrade rules. Downgrading the highest-tier workspace drops the
    cap but never removes workspaces — only creating another is
    refused (block-new-only), see ``QuotaService.check_workspace_creation_allowed``.

Why a single SELECT:
    Both the creation gate and the dashboards need the numbers in
    lockstep. One JOIN (count + bonus + ``array_agg`` of the owned plan
    names) avoids duplicate queries and the read-skew window sequential
    SELECTs would expose. The pattern was established in #661 and is
    preserved through the #675 pivot and #1550.

Soft-delete:
    Workspaces with ``deleted_at IS NOT NULL`` are excluded from the
    count AND from the tier lookup — a tombstoned pro workspace grants
    nothing, matching the runtime predicate the migration uses.

Missing user (defensive):
    If the User row is not found, returns a summary with ``owned_count=0``
    and ``cap=BASE_CAP`` (lowest tier, no bonus). The caller has already
    passed authentication, so this branch is theoretically unreachable;
    treating it as "no usage, base cap" fails safely rather than crashing.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import PLAN_ORDER, PlanTier, get_plan_tier, plan_rank
from models.auth import User, Workspace

# Every user gets one workspace for free, independent of plan tier or
# slot purchases. Held here so the cap formula is not duplicated at
# call sites; callers receive ``cap`` directly from the helper.
BASE_CAP = 1


@dataclass(frozen=True, slots=True)
class WorkspaceCapSummary:
    """Resolved owned-workspace cap for one user (#1550).

    ``cap == base + slot_bonus + tier_grant`` always holds; the parts are
    surfaced so dashboards can explain the number and the gate can build
    an upsell-ready refusal without recomputing anything.

    Attributes:
        owned_count: Live (non-deleted) workspaces the user owns.
        cap: Effective owned-workspace cap.
        base: ``BASE_CAP``.
        slot_bonus: ``users.workspace_slot_bonus`` (admin / referral grants).
        tier_grant: ``owned_workspace_grant`` of ``tier``.
        tier: Plan key of the highest tier among the owned workspaces
            (lowest tier when nothing is owned).
    """

    owned_count: int
    cap: int
    base: int
    slot_bonus: int
    tier_grant: int
    tier: str


def tier_owned_workspace_cap(tier: PlanTier) -> int:
    """Owned-workspace cap a user on ``tier`` gets with zero slot bonus (1 + grant).

    This is the ``owned_workspaces`` row of the plan matrix
    (``/workspaces/plans/tiers``, ``/admin/plans/tiers``).
    """
    return BASE_CAP + tier.owned_workspace_grant


def cap_on_tier(summary: WorkspaceCapSummary, tier_name: str) -> int:
    """The cap ``summary``'s user would have if their highest owned tier were ``tier_name``.

    Same formula as ``resolve_workspace_cap`` with the grant swapped — used
    for the "upgrade to X to own up to N" upsell so the gate never assembles
    ``base + bonus + grant`` itself.
    """
    return summary.base + summary.slot_bonus + get_plan_tier(tier_name).owned_workspace_grant


def next_tier_with_more_workspaces(tier_name: str) -> str | None:
    """Lowest tier above ``tier_name`` whose owned-workspace grant is larger.

    Skips tiers that grant the same (basic == free), so the upsell in the
    refusal message always points at a tier that actually adds slots.
    ``None`` when no higher tier grants more (already on the top tier).
    """
    current = get_plan_tier(tier_name).owned_workspace_grant
    for name in PLAN_ORDER[plan_rank(tier_name) + 1 :]:
        if get_plan_tier(name).owned_workspace_grant > current:
            return str(name)  # plain key, not the PlanName enum member
    return None


def resolve_workspace_cap(
    owned_count: int,
    slot_bonus: int,
    owned_plan_names: Iterable[str | None],
) -> WorkspaceCapSummary:
    """THE cap formula: ``BASE_CAP + slot_bonus + grant(highest owned tier)``.

    Pure — takes the numbers a fetcher already read. ``owned_plan_names``
    is the ``plan_name`` of every live owned workspace; unknown names and
    ``None`` (PostgreSQL ``array_agg`` yields ``[NULL]`` for a no-match
    LEFT JOIN) rank as the lowest tier via ``plan_rank``.
    """
    # str() so the summary carries the plain plan key (PLAN_ORDER holds the
    # PlanName enum members) — cleaner in error details and structured logs.
    tier = str(PLAN_ORDER[max((plan_rank(name) for name in owned_plan_names), default=0)])
    tier_grant = get_plan_tier(tier).owned_workspace_grant
    return WorkspaceCapSummary(
        owned_count=owned_count,
        cap=BASE_CAP + slot_bonus + tier_grant,
        base=BASE_CAP,
        slot_bonus=slot_bonus,
        tier_grant=tier_grant,
        tier=tier,
    )


async def get_user_workspace_cap_summary(db: AsyncSession, user_id: str) -> WorkspaceCapSummary:
    """Resolve one user's owned-workspace cap in a single SELECT.

    Args:
        db: Async database session.
        user_id: OAuth ``sub`` claim (string), NOT the integer ``users.id`` PK.

    Returns:
        ``WorkspaceCapSummary`` (owned non-deleted count, effective cap and
        its parts). ``owned_count=0, cap=BASE_CAP`` if the user row does
        not exist.
    """
    # LEFT OUTER JOIN so a user with zero owned workspaces still produces
    # a row (with count = 0); INNER JOIN would silently drop them.
    # COUNT(Workspace.id) skips the NULL produced by the no-match LEFT JOIN
    # so the zero-workspace case correctly counts 0 rather than 1.
    # array_agg(plan_name) rides the same JOIN so the tier lookup (#1550)
    # costs no extra round-trip; its [NULL] no-match shape is handled by
    # ``resolve_workspace_cap``.
    stmt = (
        select(
            func.count(Workspace.id).label("owned_count"),
            User.workspace_slot_bonus,
            func.array_agg(Workspace.plan_name).label("owned_plan_names"),
        )
        .outerjoin(
            Workspace,
            (Workspace.owner_user_id == User.user_id) & (Workspace.deleted_at.is_(None)),
        )
        .where(User.user_id == user_id)
        # PostgreSQL allows non-aggregated SELECT columns when grouped by
        # the table's primary key (workspace_slot_bonus is functionally
        # dependent on User.id). Grouping by User.id alone is sufficient
        # and documents that the row is unique per user.
        .group_by(User.id)
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return resolve_workspace_cap(0, 0, ())
    return resolve_workspace_cap(row.owned_count, row.workspace_slot_bonus, row.owned_plan_names)
