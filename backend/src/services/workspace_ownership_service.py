"""Owner-initiated workspace ownership transfer (#1094).

Adds the first *voluntary* ownership transfer path. Until now the only way a
workspace changed owner was the account-erasure auto-transfer
(``account_erasure_service``); a sole owner was a single point of failure for
every owner-gated billing/admin action.

Single-owner invariant
-----------------------
A live workspace has exactly **one** OWNER. Since #1102 every owner-mutating path
serializes on the **same** ``SELECT ... FOR UPDATE`` workspace-row lock — the
shared ``services.workspace_locks.lock_workspace_for_update`` — so the invariant
is *structural* (row-level serialization) rather than *emergent* (each path's
local guards happening to line up). This voluntary transfer path takes the lock,
then re-checks the caller is still owner *under* the lock. The other paths:

* workspace *creation* (``workspace_service`` / ``context_service`` personal
  workspace) — a brand-new workspace, never a live concurrent-transfer target.
* ``WorkspaceService.update_member_role`` — takes the shared lock whenever the
  role change adds OR removes an OWNER (#1102), in addition to its own
  single-owner guard. NOTE: it adjusts ``WorkspaceMember.role`` only, never
  ``owner_user_id`` (that canonical pointer is moved solely by this service and
  by erasure). Demoting the ``owner_user_id`` holder via this path therefore
  leaves the two representations out of sync — a pre-existing gap the lock does
  NOT close; tracked separately, not in #1102's scope.
* the account-erasure auto-transfer (``account_erasure_service``) — takes the
  shared ``lock_workspace_for_update`` per workspace before its auto-transfer
  (#1102; it loads workspaces with an unlocked SELECT, so the lock is acquired at
  mutation time, then re-checks the erased user still owns the row) and bumps
  ``ownership_epoch`` so the epoch contract below holds for every ``owner_user_id``
  change.

Do not move ``owner_user_id`` on an existing workspace without taking
``lock_workspace_for_update`` and bumping ``ownership_epoch``.

Ownership epoch
---------------
Each successful transfer bumps ``workspaces.ownership_epoch``. That is the
*producer* side of a contract; the *consumer* (invalidating external sessions /
billing-handoff tokens bound to the previous owner) is a separate follow-up and
is intentionally NOT faked here.
"""

from __future__ import annotations

from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.workspace_roles import WorkspaceRole
from models.auth import (
    FORCE_TRANSFER_STATUS_APPROVED,
    FORCE_TRANSFER_STATUS_CANCELLED,
    FORCE_TRANSFER_STATUS_PENDING,
    FORCE_TRANSFER_STATUS_SUPERSEDED,
    AuditLog,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceOwnershipForceTransferRequest,
)
from services.workspace_locks import lock_workspace_for_update
from utils.datetime import utcnow
from utils.exceptions import BadRequestError, ConflictError, NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)

OWNERSHIP_TRANSFER_ACTION = "workspace_ownership_transfer"
FORCE_OWNERSHIP_TRANSFER_ACTION = "workspace_ownership_force_transfer"


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
        # 1. Lock the workspace row first — the single-owner synchronization point,
        # shared with update_member_role + account_erasure via lock_workspace_for_update
        # (#1102) so every owner-mutating path serializes on the same row.
        workspace = await lock_workspace_for_update(self.db, workspace_id)

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

        previous_owner_id = workspace.owner_user_id

        # 4 + 5. Fetch BOTH the target and previous-owner member rows in a single
        # round-trip — keeps the critical section under the workspace lock short.
        # target_user_id != previous_owner_id here (the idempotent no-op above
        # already returned when they are equal), so this is two distinct rows.
        member_rows = (
            (
                await self.db.execute(
                    select(WorkspaceMember).where(
                        WorkspaceMember.workspace_id == workspace_id,
                        WorkspaceMember.user_id.in_([target_user_id, previous_owner_id]),
                    )
                )
            )
            .scalars()
            .all()
        )
        members_by_user = {m.user_id: m for m in member_rows}

        target_member = members_by_user.get(target_user_id)
        if target_member is None:
            # Well-formed request, but the named user is not a member of THIS
            # workspace — a state precondition failure (400), not a shape error.
            raise BadRequestError(
                "Target user must be an existing workspace member",
                error_code="WS-OWNER-001",
            )

        # Demote the previous owner's member row to ADMIN — if it exists. Some
        # legacy workspaces carry owner_user_id without a matching member row;
        # there is nothing to demote in that case, so flip ownership regardless.
        previous_member = members_by_user.get(previous_owner_id)
        if previous_member is not None:
            previous_member.role = WorkspaceRole.ADMIN

        target_member.role = WorkspaceRole.OWNER
        workspace.owner_user_id = target_user_id
        # Capture the new epoch in a local — attributes may be expired after
        # commit, and the result/log must not trigger a post-commit lazy load.
        # ownership_epoch is NOT NULL (server_default "0") and this row was just
        # loaded under the FOR UPDATE lock, so it is never None here.
        new_epoch = workspace.ownership_epoch + 1
        workspace.ownership_epoch = new_epoch

        # 6. Audit row in the SAME transaction → the transfer and its audit trail
        # commit (or roll back) atomically. The previous/new owner user_ids go in
        # user_metadata (JSON, unbounded), NOT old_value_hash/new_value_hash —
        # those columns are String(64) (sized for SHA256/HMAC hex) and a
        # federated OAuth sub can exceed 64 chars (User.user_id is String(255)),
        # which would truncate-error on INSERT.
        self.db.add(
            AuditLog(
                user_email=performed_by_email,
                user_id=current_owner_id,
                action=OWNERSHIP_TRANSFER_ACTION,
                resource=f"workspace:{workspace_id}",
                user_metadata={
                    "previous_owner_id": previous_owner_id,
                    "new_owner_id": target_user_id,
                    "ownership_epoch": new_epoch,
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

    async def force_transfer_ownership(
        self,
        *,
        workspace_id: UUID,
        target_user_id: str,
        performed_by_user_id: str,
        performed_by_email: str,
        reason: str,
    ) -> OwnershipTransferResult:
        """Break-glass system-admin force-transfer of ownership (#1101).

        For when the current owner is unavailable. Unlike ``transfer_ownership``
        there is **no current-owner check** — the route enforces the system-admin
        gate (``require_admin``) instead. Like the voluntary path it serializes on
        the shared workspace row lock and bumps ``ownership_epoch`` (so the #1100
        consumer invalidates the displaced owner's billing-handoff tokens /
        sessions).

        Differs from the voluntary path in two ways:
          * The target need NOT already be a member — a non-member is **added** as
            OWNER, which handles the zero-member workspace (no eligible member to
            promote). The target MUST still be a real ``User`` (never hand
            ownership to a phantom/typo'd id, which would orphan the workspace).
          * A **mandatory** audit row records ``break_glass=true`` + the operator's
            ``reason`` + ``target_was_member`` so a privileged override is always
            forensically attributable. It commits in the SAME transaction as the
            transfer (fail-closed: no audit ⇒ no transfer).

        Args:
            workspace_id: The workspace whose ownership is being seized.
            target_user_id: The user to install as owner.
            performed_by_user_id: The acting system admin (the audit actor).
            performed_by_email: The acting admin's email, for the audit row.
            reason: Non-empty justification (route enforces non-empty).

        Returns:
            The transfer outcome (``changed=False`` for the idempotent no-op).

        Raises:
            NotFoundException: workspace missing or soft-deleted (404).
            BadRequestError: the target user does not exist (400, WS-OWNER-002).
        """
        # 1. Lock the workspace row — the shared single-owner synchronization point
        # (#1102): break-glass serializes with the voluntary transfer, role change,
        # and erasure auto-transfer on the SAME FOR UPDATE row.
        workspace = await lock_workspace_for_update(self.db, workspace_id)

        # 2. Single-control path (#1101): apply the seizure on the locked row and
        # commit. The shared core (_apply_force_transfer) stages the swap + epoch
        # bump + mandatory audit; this path preserves #1101 behavior exactly — the
        # idempotent no-op neither commits nor audits (its log line is the signal).
        result = await self._apply_force_transfer(
            workspace=workspace,
            target_user_id=target_user_id,
            performed_by_user_id=performed_by_user_id,
            performed_by_email=performed_by_email,
            reason=reason,
        )
        if result.changed:
            await self.db.commit()
            logger.info(
                "workspace_ownership_force_transferred",
                workspace_id=str(workspace_id),
                previous_owner=result.previous_owner_id,
                new_owner=result.new_owner_id,
                ownership_epoch=result.ownership_epoch,
                performed_by=performed_by_user_id,
            )
        return result

    async def _apply_force_transfer(
        self,
        *,
        workspace: Workspace,
        target_user_id: str,
        performed_by_user_id: str,
        performed_by_email: str,
        reason: str,
        audit_extra: dict[str, Any] | None = None,
    ) -> OwnershipTransferResult:
        """Apply a break-glass ownership seizure on an ALREADY-LOCKED workspace.

        Shared core of ``force_transfer_ownership`` (#1101, single-control) and
        ``approve_force_transfer`` (#1113, dual-control). The caller MUST already
        hold the workspace row lock (``lock_workspace_for_update``) and owns the
        final ``commit`` — this method stages the membership swap, ``ownership_epoch``
        bump, and the mandatory audit row but does NOT commit, so the caller can
        atomically include its own mutations (e.g. marking the dual-control request
        approved) in the same transaction.

        Idempotent no-op (target already owns it): logs the alertable no-op line
        and returns ``changed=False`` WITHOUT staging an audit row, mirroring the
        #1101 voluntary-path behavior.

        ``audit_extra`` is merged into the AuditLog ``user_metadata`` so the
        dual-control path can record both the initiator and the approver.

        Raises:
            BadRequestError: the target user does not exist (400, WS-OWNER-002).
        """
        workspace_id = workspace.id

        # Idempotent no-op: already owned by the target (no epoch bump / audit).
        # The log line is the alertable signal even when nothing changes.
        if workspace.owner_user_id == target_user_id:
            logger.info(
                "workspace_ownership_force_transfer_noop",
                workspace_id=str(workspace_id),
                target=target_user_id,
                performed_by=performed_by_user_id,
            )
            return OwnershipTransferResult(
                workspace_id=workspace_id,
                previous_owner_id=workspace.owner_user_id,
                new_owner_id=target_user_id,
                ownership_epoch=workspace.ownership_epoch,
                changed=False,
            )

        # The target must be a real account — handing ownership to a phantom
        # user_id would permanently orphan the workspace. Best-effort existence
        # check (``owner_user_id`` has no FK to ``users.user_id``).
        target_user = (
            await self.db.execute(select(User).where(User.user_id == target_user_id))
        ).scalar_one_or_none()
        if target_user is None:
            raise BadRequestError(
                "Target user does not exist",
                error_code="WS-OWNER-002",
            )

        previous_owner_id = workspace.owner_user_id

        # Load BOTH the target and previous-owner member rows in one round-trip.
        member_rows = (
            (
                await self.db.execute(
                    select(WorkspaceMember).where(
                        WorkspaceMember.workspace_id == workspace_id,
                        WorkspaceMember.user_id.in_([target_user_id, previous_owner_id]),
                    )
                )
            )
            .scalars()
            .all()
        )
        members_by_user = {m.user_id: m for m in member_rows}

        target_member = members_by_user.get(target_user_id)
        target_was_member = target_member is not None
        if target_member is None:
            # Break-glass elevated path: the target is not a member (e.g. a
            # zero-member workspace). Add them as OWNER. The (workspace_id, user_id)
            # UNIQUE constraint (#1101) makes a concurrent duplicate insert fail
            # cleanly rather than silently creating two membership rows.
            self.db.add(
                WorkspaceMember(
                    workspace_id=workspace_id,
                    user_id=target_user_id,
                    role=WorkspaceRole.OWNER,
                    joined_at=utcnow(),
                )
            )
        else:
            target_member.role = WorkspaceRole.OWNER

        # Demote the previous owner's member row to ADMIN if it exists (some legacy
        # workspaces carry owner_user_id without a matching member row).
        previous_member = members_by_user.get(previous_owner_id)
        if previous_member is not None:
            previous_member.role = WorkspaceRole.ADMIN

        workspace.owner_user_id = target_user_id
        new_epoch = workspace.ownership_epoch + 1
        workspace.ownership_epoch = new_epoch

        # Mandatory audit staged in the caller's transaction (fail-closed). The
        # actor is the acting ADMIN (NOT the displaced owner). ``audit_extra`` adds
        # the dual-control identities (initiator + approver) when present.
        metadata: dict[str, Any] = {
            "break_glass": True,
            "reason": reason,
            "previous_owner_id": previous_owner_id,
            "new_owner_id": target_user_id,
            "ownership_epoch": new_epoch,
            "target_was_member": target_was_member,
            # Self-dealing (the acting admin installs themselves) is allowed by
            # break-glass but flagged explicitly so forensics needn't compare the
            # actor column against new_owner_id by hand.
            "self_transfer": performed_by_user_id == target_user_id,
        }
        if audit_extra:
            metadata.update(audit_extra)
        self.db.add(
            AuditLog(
                user_email=performed_by_email,
                user_id=performed_by_user_id,
                action=FORCE_OWNERSHIP_TRANSFER_ACTION,
                resource=f"workspace:{workspace_id}",
                user_metadata=metadata,
            )
        )
        return OwnershipTransferResult(
            workspace_id=workspace_id,
            previous_owner_id=previous_owner_id,
            new_owner_id=target_user_id,
            ownership_epoch=new_epoch,
            changed=True,
        )

    # ------------------------------------------------------------------
    # Dual-control force-transfer (#1113)
    # ------------------------------------------------------------------

    async def initiate_force_transfer(
        self,
        *,
        workspace_id: UUID,
        target_user_id: str,
        performed_by_user_id: str,
        performed_by_email: str,
        reason: str,
    ) -> WorkspaceOwnershipForceTransferRequest:
        """File a PENDING dual-control force-transfer request (#1113).

        Used when ``require_dual_control_force_transfer`` is enabled: instead of
        seizing ownership immediately, the initiating admin records intent here
        and a second, distinct admin must ``approve_force_transfer`` before it
        commits. Snapshots ``ownership_epoch`` so approval can reject a stale
        request whose ownership moved since filing.

        Supersedes any existing pending request for the workspace (marks it
        ``superseded``) — this is the unblock path for an abandoned request, and
        keeps the one-pending-per-workspace partial-unique invariant.

        Raises:
            NotFoundException: workspace missing or soft-deleted (404).
            BadRequestError: the target user does not exist (400, WS-OWNER-002).
        """
        # Lock the workspace row (validates exists / not soft-deleted) and pin the
        # epoch snapshot under the same serialization as every owner-mutating path.
        workspace = await lock_workspace_for_update(self.db, workspace_id)

        # Don't file a request for a phantom target — same guard as the apply path.
        target_user = (
            await self.db.execute(select(User).where(User.user_id == target_user_id))
        ).scalar_one_or_none()
        if target_user is None:
            raise BadRequestError(
                "Target user does not exist",
                error_code="WS-OWNER-002",
            )

        # Supersede any existing pending request for this workspace, then flush so
        # the partial-unique (one pending per workspace) is satisfied before the
        # new insert.
        existing = (
            (
                await self.db.execute(
                    select(WorkspaceOwnershipForceTransferRequest).where(
                        WorkspaceOwnershipForceTransferRequest.workspace_id == workspace_id,
                        WorkspaceOwnershipForceTransferRequest.status
                        == FORCE_TRANSFER_STATUS_PENDING,
                    )
                )
            )
            .scalars()
            .all()
        )
        for prior in existing:
            prior.status = FORCE_TRANSFER_STATUS_SUPERSEDED
            prior.decided_by_user_id = performed_by_user_id
            prior.decided_by_email = performed_by_email
            prior.decided_at = utcnow()
        if existing:
            await self.db.flush()

        request = WorkspaceOwnershipForceTransferRequest(
            workspace_id=workspace_id,
            target_user_id=target_user_id,
            reason=reason,
            ownership_epoch_at_initiation=workspace.ownership_epoch,
            initiated_by_user_id=performed_by_user_id,
            initiated_by_email=performed_by_email,
            status=FORCE_TRANSFER_STATUS_PENDING,
        )
        self.db.add(request)
        await self.db.commit()
        await self.db.refresh(request)

        logger.info(
            "workspace_ownership_force_transfer_initiated",
            workspace_id=str(workspace_id),
            request_id=str(request.id),
            target=target_user_id,
            initiated_by=performed_by_user_id,
            superseded=len(existing),
        )
        return request

    async def approve_force_transfer(
        self,
        *,
        request_id: UUID,
        approver_user_id: str,
        approver_email: str,
    ) -> OwnershipTransferResult:
        """Approve a pending dual-control request and apply the transfer (#1113).

        Four-eyes control: the approver MUST be a *different* system admin than
        the initiator. Under the workspace row lock, re-checks that ownership has
        not moved since the request was filed (staleness) before applying the
        seizure; the apply, the request status update, and the mandatory audit
        (recording BOTH identities) commit atomically.

        Security notes:
          * Four-eyes is enforced by ``user_id`` inequality. It assumes the
            operational invariant "one human ⇒ at most one system-admin account";
            a single operator holding two admin accounts can still satisfy it.
            That invariant is an identity-provisioning concern, not enforced here.
          * The *policy* gate (whether dual-control applies at all) lives at the
            route via ``require_dual_control_force_transfer``; this service method
            is the *mechanism*. ``force_transfer_ownership`` performs the immediate
            single-control seizure and is NOT itself flag-gated, so any future
            direct caller of it bypasses four-eyes — route it through ``initiate``
            when the flag is on.

        Raises:
            NotFoundException: no such request (404).
            ConflictError: request not pending (409) or ownership moved since
                filing (409, stale — re-initiate).
            BadRequestError: approver is the initiator (400, WS-OWNER-DUAL-001)
                or the target user no longer exists (400, WS-OWNER-002).
        """
        request = (
            await self.db.execute(
                select(WorkspaceOwnershipForceTransferRequest).where(
                    WorkspaceOwnershipForceTransferRequest.id == request_id
                )
            )
        ).scalar_one_or_none()
        if request is None:
            raise NotFoundException("Force-transfer request", str(request_id))
        if request.status != FORCE_TRANSFER_STATUS_PENDING:
            raise ConflictError(f"Force-transfer request is not pending (status={request.status})")

        # No self-approval — the whole point of dual-control is a second pair of
        # eyes. Checked before the lock (cheap, request-only) so we fail fast. This
        # is the exact attack the control exists to stop, so log the denial as an
        # alertable security signal.
        if approver_user_id == request.initiated_by_user_id:
            logger.warning(
                "workspace_ownership_force_transfer_self_approval_denied",
                workspace_id=str(request.workspace_id),
                request_id=str(request.id),
                actor=approver_user_id,
            )
            raise BadRequestError(
                "A force-transfer must be approved by a different system admin "
                "than the one who initiated it",
                error_code="WS-OWNER-DUAL-001",
            )

        # Lock the workspace — the shared serialization point that every owner- and
        # request-mutating path takes (approve, cancel, initiate-supersede).
        workspace = await lock_workspace_for_update(self.db, request.workspace_id)

        # Re-validate the request status UNDER the lock to close the TOCTOU between
        # the pre-lock read above and here: a concurrent cancel or initiate-
        # supersede (both hold this same lock) may have decided the request, and the
        # epoch check alone cannot catch that — cancel/supersede do not move the
        # epoch. refresh() re-SELECTs the row; under READ COMMITTED a concurrent
        # committed transition is now visible, and because the mutators serialize on
        # this lock the re-check is authoritative.
        await self.db.refresh(request)
        if request.status != FORCE_TRANSFER_STATUS_PENDING:
            raise ConflictError(
                f"Force-transfer request is no longer pending (status={request.status})"
            )

        # Staleness: if ownership moved since the request was filed, the approver
        # would be acting on a stale premise (confused deputy). Refuse and require a
        # fresh initiate; log as an alertable signal.
        if workspace.ownership_epoch != request.ownership_epoch_at_initiation:
            logger.warning(
                "workspace_ownership_force_transfer_stale_denied",
                workspace_id=str(request.workspace_id),
                request_id=str(request.id),
                epoch_at_initiation=request.ownership_epoch_at_initiation,
                current_epoch=workspace.ownership_epoch,
                actor=approver_user_id,
            )
            raise ConflictError(
                "Workspace ownership changed since this force-transfer was "
                "initiated; re-initiate the request and have it re-approved"
            )

        result = await self._apply_force_transfer(
            workspace=workspace,
            target_user_id=request.target_user_id,
            performed_by_user_id=approver_user_id,
            performed_by_email=approver_email,
            reason=request.reason,
            audit_extra={
                "dual_control": True,
                "force_transfer_request_id": str(request.id),
                "initiated_by_user_id": request.initiated_by_user_id,
                "initiated_by_email": request.initiated_by_email,
                "approved_by_user_id": approver_user_id,
            },
        )

        # Mark the request approved atomically with the transfer.
        request.status = FORCE_TRANSFER_STATUS_APPROVED
        request.decided_by_user_id = approver_user_id
        request.decided_by_email = approver_email
        request.decided_at = utcnow()
        await self.db.commit()

        logger.info(
            "workspace_ownership_force_transfer_approved",
            workspace_id=str(request.workspace_id),
            request_id=str(request.id),
            initiated_by=request.initiated_by_user_id,
            approved_by=approver_user_id,
            changed=result.changed,
        )
        return result

    async def cancel_force_transfer(
        self,
        *,
        request_id: UUID,
        cancelled_by_user_id: str,
        cancelled_by_email: str,
    ) -> WorkspaceOwnershipForceTransferRequest:
        """Cancel a pending dual-control request (#1113).

        Lets an admin retract a pending force-transfer (e.g. filed in error)
        without having to initiate a fresh one to supersede it. Only a pending
        request can be cancelled.

        Raises:
            NotFoundException: no such request (404).
            ConflictError: request is not pending (409).
        """
        request = (
            await self.db.execute(
                select(WorkspaceOwnershipForceTransferRequest).where(
                    WorkspaceOwnershipForceTransferRequest.id == request_id
                )
            )
        ).scalar_one_or_none()
        if request is None:
            raise NotFoundException("Force-transfer request", str(request_id))
        if request.status != FORCE_TRANSFER_STATUS_PENDING:
            raise ConflictError(f"Force-transfer request is not pending (status={request.status})")

        # Serialize with approve / initiate-supersede on the workspace row so a
        # cancel cannot race an in-flight approval (TOCTOU): whoever takes the lock
        # second re-reads the status under it and sees the other's decision. (A
        # request whose workspace is gone is moot — lock_workspace_for_update 404s.)
        await lock_workspace_for_update(self.db, request.workspace_id)
        await self.db.refresh(request)
        if request.status != FORCE_TRANSFER_STATUS_PENDING:
            raise ConflictError(
                f"Force-transfer request is no longer pending (status={request.status})"
            )

        request.status = FORCE_TRANSFER_STATUS_CANCELLED
        request.decided_by_user_id = cancelled_by_user_id
        request.decided_by_email = cancelled_by_email
        request.decided_at = utcnow()
        await self.db.commit()

        logger.info(
            "workspace_ownership_force_transfer_cancelled",
            workspace_id=str(request.workspace_id),
            request_id=str(request.id),
            cancelled_by=cancelled_by_user_id,
        )
        return request

    async def get_force_transfer_request(
        self, *, request_id: UUID
    ) -> WorkspaceOwnershipForceTransferRequest:
        """Fetch a force-transfer request by id (#1113).

        Lets the SECOND admin inspect what they are approving (target, reason,
        initiator) before calling ``approve_force_transfer`` — without this, the
        four-eyes control degrades to a blind second button-press. Read-only.

        Raises:
            NotFoundException: no such request (404).
        """
        request = (
            await self.db.execute(
                select(WorkspaceOwnershipForceTransferRequest).where(
                    WorkspaceOwnershipForceTransferRequest.id == request_id
                )
            )
        ).scalar_one_or_none()
        if request is None:
            raise NotFoundException("Force-transfer request", str(request_id))
        return request
