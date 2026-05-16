"""User-level workspace-cap resolution (#674 sub-A, #675).

Returns ``(owned_count, workspace_slot_bonus)`` for a user so callers
can compute the effective cap as ``1 + workspace_slot_bonus`` and
compare it against ``owned_count`` (#674's slot pivot).

Why a single SELECT:
    Both ``QuotaService.check_workspace_creation_allowed`` (gate) and
    ``/usage/current`` (dashboard) need both numbers in lockstep. One
    JOIN avoids a duplicate query and the small read-skew window two
    sequential SELECTs would expose. The pattern was established for
    the previous tier-derivation helper in #661 and is preserved
    through the #675 pivot.

Soft-delete:
    Workspaces with ``deleted_at IS NOT NULL`` are excluded from the
    count — this matches the runtime predicate the migration uses and
    keeps the gate from blocking owners whose remaining workspaces are
    tombstones.

Missing user (defensive):
    If the User row is not found, returns ``(0, 0)``. The caller has
    already passed authentication, so this branch is theoretically
    unreachable; treating it as "no quota, no bonus" fails safely
    rather than crashing.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, Workspace


async def get_user_workspace_cap_summary(db: AsyncSession, user_id: str) -> tuple[int, int]:
    """Return ``(owned_count, workspace_slot_bonus)`` for the user.

    Args:
        db: Async database session.
        user_id: OAuth ``sub`` claim (string), NOT the integer ``users.id`` PK.

    Returns:
        Tuple of ``(owned non-deleted workspace count, slot bonus column value)``.
        Returns ``(0, 0)`` if the user row does not exist.
    """
    # LEFT OUTER JOIN so a user with zero owned workspaces still produces
    # a row (with count = 0); INNER JOIN would silently drop them.
    # COUNT(Workspace.id) skips the NULL produced by the no-match LEFT JOIN
    # so the zero-workspace case correctly counts 0 rather than 1.
    stmt = (
        select(
            func.count(Workspace.id).label("owned_count"),
            User.workspace_slot_bonus,
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
        return (0, 0)
    return (row.owned_count, row.workspace_slot_bonus)
