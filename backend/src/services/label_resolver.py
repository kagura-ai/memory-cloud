"""Batch label resolution for list rows: ids → what an operator reads (#1861).

The sleep-reports routes resolve ``context_name`` / ``user_email`` per
response; the cost-aggregation routes need the same for users and
workspaces. One place, one query per kind, no N+1. Ids with no row are
left out of the maps so the UI falls back to a shortened id.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, Workspace, WorkspaceMember


async def resolve_user_labels(
    db: AsyncSession, user_ids: set[str], *, member_of: UUID | None = None
) -> dict[str, str]:
    """``user_id → email`` for ids that resolve to a ``users`` row.

    The join key is ``User.user_id`` (the OAuth ``sub`` claim) — not the
    integer PK — because usage and sleep rows store the ``sub``.

    ``member_of`` narrows the answer to current members of that workspace:
    a workspace owner may learn a member's email (``/members`` shows it) but
    not that of an account that wrote in the workspace and left, or that
    only ever wrote through a connector. Admin surfaces pass ``None``.
    """
    if not user_ids:
        return {}
    query = select(User.user_id, User.email).where(User.user_id.in_(user_ids))
    if member_of is not None:
        query = query.where(
            User.user_id.in_(
                select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == member_of)
            )
        )
    return dict((await db.execute(query)).all())


async def resolve_workspace_names(db: AsyncSession, workspace_ids: set[UUID]) -> dict[UUID, str]:
    """``workspace_id → name`` for live workspaces; a soft-deleted one is left
    out so its rows fall back to the id instead of wearing a live-looking name."""
    if not workspace_ids:
        return {}
    result = await db.execute(
        select(Workspace.id, Workspace.name).where(
            Workspace.id.in_(workspace_ids), Workspace.deleted_at.is_(None)
        )
    )
    return dict(result.all())
