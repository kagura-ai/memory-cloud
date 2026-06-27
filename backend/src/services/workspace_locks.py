"""Shared workspace-row lock — the single synchronization point for every
owner-mutating path (#1102).

Workspace ownership is reassigned from three places: ``WorkspaceOwnershipService.
transfer_ownership`` (voluntary), ``WorkspaceService.update_member_role`` (role
promotion/demotion that touches OWNER), and ``account_erasure_service`` (owner
erasure auto-transfer). All three acquire THIS ``SELECT ... FOR UPDATE`` on the
workspace row before mutating ownership, so concurrent owner-mutations on the
same workspace **serialize** (row-level mutual exclusion) instead of racing —
e.g. two paths can no longer read the same ``ownership_epoch`` and both write
``epoch + 1``, losing an increment. (The lock serializes the *paths*; it does not
itself reconcile the ``owner_user_id`` pointer with ``WorkspaceMember.role`` —
see ``workspace_ownership_service`` for that representation contract.)
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from utils.exceptions import NotFoundException


async def lock_workspace_for_update(db: AsyncSession, workspace_id: UUID) -> Workspace:
    """``SELECT ... FOR UPDATE`` the live (non-soft-deleted) workspace row.

    The lock is the single-owner synchronization point: every path that reassigns
    OWNER must hold it, so two owner-mutations on the same workspace serialize
    instead of racing. The row lock is held until the surrounding transaction
    commits or rolls back (it cannot be released early — callers must keep the
    mutation in the same transaction).

    Args:
        db: The request-scoped async session.
        workspace_id: The workspace to lock.

    Returns:
        The locked :class:`Workspace` row.

    Raises:
        NotFoundException: the workspace is missing or soft-deleted (404).
    """
    workspace = (
        await db.execute(
            select(Workspace)
            .where(Workspace.id == workspace_id, Workspace.deleted_at.is_(None))
            .with_for_update()
            # populate_existing is load-bearing: if the row is ALREADY in this
            # session's identity map (e.g. account_erasure loaded it with an
            # unlocked SELECT), a plain re-SELECT returns the cached instance with
            # its STALE attributes — the FOR UPDATE takes the DB lock but discards
            # the freshly-read values. populate_existing forces the under-lock row
            # to overwrite the cached attributes, so callers see committed concurrent
            # changes (owner_user_id / ownership_epoch) and read-modify-write is safe.
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if workspace is None:
        raise NotFoundException("Workspace")
    return workspace
