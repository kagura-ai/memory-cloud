"""Owner-initiated workspace ownership transfer (#1094).

Adds the first *voluntary* ownership transfer path. Until now the only way a
workspace changed owner was the account-erasure auto-transfer
(``account_erasure_service``); a sole owner was a single point of failure for
every owner-gated billing/admin action.

Single-owner invariant
-----------------------
A live workspace has exactly **one** OWNER. OWNER promotion on an *existing*
workspace happens **only** through :meth:`WorkspaceOwnershipService.transfer_ownership`,
which takes a ``SELECT ... FOR UPDATE`` lock on the workspace row as the
synchronization point. The other OWNER assignments in the codebase are confined
to workspace *creation* (``workspace_service`` / ``context_service`` personal
workspace) and the erasure auto-transfer — none of which can create a second
owner on a live, concurrently-transferred workspace. Do not introduce another
code path that assigns ``WorkspaceRole.OWNER`` on an existing workspace without
routing through this service (or taking the same row lock).

Ownership epoch
---------------
Each successful transfer bumps ``workspaces.ownership_epoch``. That is the
*producer* side of a contract; the *consumer* (invalidating external sessions /
billing-handoff tokens bound to the previous owner) is a separate follow-up and
is intentionally NOT faked here.
"""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import AuditLog, Workspace, WorkspaceMember
from utils.exceptions import BadRequestError, ConflictError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)

OWNERSHIP_TRANSFER_ACTION = "workspace_ownership_transfer"


class OwnershipTransferResult(NamedTuple):
    """Outcome of a transfer attempt.

    ``changed`` is ``False`` for the idempotent no-op (the target already owns
    the workspace) — in that case ``ownership_epoch`` is unchanged and no audit
    row is written.
    """

    workspace_id: UUID
    previous_owner_id: str
    new_owner_id: str
    ownership_epoch: int
    changed: bool


class WorkspaceOwnershipService:
    """Transactional workspace ownership transfer."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def transfer_ownership(
        self,
        *,
        workspace_id: UUID,
        current_owner_id: str,
        target_user_id: str,
        performed_by_email: str,
    ) -> OwnershipTransferResult:
        """Transfer ownership of ``workspace_id`` to ``target_user_id``.

        The caller MUST have already verified that ``current_owner_id`` is the
        owner (the route does this via ``PermissionService.check_workspace_owner``).
        That check runs *before* the row lock, so this method re-verifies the
        owner **under the lock** to close the check→lock TOCTOU window.

        Steps (all under the workspace row lock):
          1. Lock the workspace row (``FOR UPDATE``) — serializes concurrent
             transfers; 404 if missing/soft-deleted.
          2. Re-verify ``current_owner_id`` still owns it → else 409.
          3. Idempotent no-op if the target already owns it (no epoch bump,
             no audit).
          4. Require the target to be an existing member → else 400.
          5. Promote target → OWNER, demote previous owner → ADMIN (if a member
             row exists), flip ``owner_user_id``, bump ``ownership_epoch``.
          6. Write the audit row and commit atomically.

        Args:
            workspace_id: The workspace whose ownership is moving.
            current_owner_id: The authenticated caller (must currently own it).
            target_user_id: The member to promote to owner.
            performed_by_email: Email of the actor, for the audit row.

        Returns:
            The transfer outcome (``changed=False`` for the idempotent no-op).

        Raises:
            NotFoundException: workspace missing or soft-deleted (404).
            ConflictError: ownership changed concurrently (409).
            BadRequestError: target is not a member of this workspace (400).
        """
        # 1. Lock the workspace row first — the single-owner synchronization point.
        workspace = (
            await self.db.execute(
                select(Workspace)
                .where(Workspace.id == workspace_id, Workspace.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if workspace is None:
            raise NotFoundException("Workspace")

        # 2. TOCTOU close: the route's owner check ran before this lock. If the
        # owner changed in between, refuse rather than transfer on a stale premise.
        if workspace.owner_user_id != current_owner_id:
            raise ConflictError("Workspace ownership changed concurrently; reload and retry")

        # 3. Idempotent no-op: already owned by the target.
        if workspace.owner_user_id == target_user_id:
            return OwnershipTransferResult(
                workspace_id=workspace_id,
                previous_owner_id=workspace.owner_user_id,
                new_owner_id=target_user_id,
                ownership_epoch=workspace.ownership_epoch,
                changed=False,
            )

        # 4. Target must be an existing member of THIS workspace.
        target_member = (
            await self.db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.user_id == target_user_id,
                )
            )
        ).scalar_one_or_none()
        if target_member is None:
            # Well-formed request, but the named user is not a member of THIS
            # workspace — a state precondition failure (400), not a shape error.
            raise BadRequestError(
                "Target user must be an existing workspace member",
                error_code="WS-OWNER-001",
            )

        previous_owner_id = workspace.owner_user_id

        # 5. Demote the previous owner's member row to ADMIN — if it exists. Some
        # legacy workspaces carry owner_user_id without a matching member row;
        # there is nothing to demote in that case, so flip ownership regardless.
        previous_member = (
            await self.db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.user_id == previous_owner_id,
                )
            )
        ).scalar_one_or_none()
        if previous_member is not None:
            previous_member.role = WorkspaceRole.ADMIN

        target_member.role = WorkspaceRole.OWNER
        workspace.owner_user_id = target_user_id
        # Capture the new epoch in a local — attributes may be expired after
        # commit, and the result/log must not trigger a post-commit lazy load.
        new_epoch = (workspace.ownership_epoch or 0) + 1
        workspace.ownership_epoch = new_epoch

        # 6. Audit row in the SAME transaction → the transfer and its audit trail
        # commit (or roll back) atomically. user_id columns are stable OAuth subs
        # and are stored legibly (the audit consumer needs "who became owner");
        # the #481 HMAC rule targets *mutable* identifiers, not these.
        self.db.add(
            AuditLog(
                user_email=performed_by_email,
                user_id=current_owner_id,
                action=OWNERSHIP_TRANSFER_ACTION,
                resource=f"workspace:{workspace_id}",
                old_value_hash=previous_owner_id,
                new_value_hash=target_user_id,
                user_metadata={
                    "ownership_epoch": new_epoch,
                    "performed_by": current_owner_id,
                },
            )
        )
        await self.db.commit()

        logger.info(
            "workspace_ownership_transferred",
            workspace_id=str(workspace_id),
            previous_owner=previous_owner_id,
            new_owner=target_user_id,
            ownership_epoch=new_epoch,
        )
        return OwnershipTransferResult(
            workspace_id=workspace_id,
            previous_owner_id=previous_owner_id,
            new_owner_id=target_user_id,
            ownership_epoch=new_epoch,
            changed=True,
        )
