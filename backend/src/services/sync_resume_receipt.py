"""Observe original resume outcomes without invoking any publication path."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal
from auth.workspace_roles import ContextRole
from models.auth import Context
from models.management_schemas import digest
from models.sync_management import SyncManagedTarget, SyncModeOperation
from models.sync_resume_receipt_schemas import (
    SyncResumeModeReceipt,
    SyncResumeReceiptCommand,
    SyncResumeReceiptView,
)
from services.capacity_lock import CapacityPolicy
from services.permission_service import PermissionService
from services.sync_feed_authority import locked
from services.sync_managed_guard import target_fence
from services.sync_management import management_boundary
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException


async def read_resume_receipt(
    db: AsyncSession,
    wid: UUID,
    iid: UUID,
    principal: SyncPrincipal,
    command: SyncResumeReceiptCommand,
) -> SyncResumeReceiptView:
    """Return exact original metadata using fresh authority and transition locks.

    No paid-write guard, enrollment, preview lookup or confirmation is called.
    The current Editor may reconcile a previous Editor's operation without using
    or renewing that original person's credentials. Errors never imply absence.
    """
    async with management_boundary(db, wid, iid, principal) as (
        installation,
        _workspace,
        _fence,
    ):
        current = await target_fence(db, installation.context_id)
        if current.mode is None:
            if await db.get(SyncManagedTarget, iid) is not None:
                raise ConflictError("Synchronization ownership changed")
        elif (
            current.mode.installation_id,
            current.mode.resource_pk,
            current.mode.context_id,
            current.mode.generation,
        ) != (iid, installation.resource_pk, installation.context_id, installation.generation):
            raise ConflictError("Synchronization ownership changed")

        # Membership locks do not fence a Context privacy change by its owner.
        # Refresh and retain this metadata lock through observation/commit, then
        # recheck reach. Unlike exact_target, this does not require a live public
        # endpoint or a published resource slug to recover historical outcomes.
        context = await locked(db, Context, Context.id == installation.context_id)
        if context is None or context.deleted_at is not None:
            raise NotFoundException("Synchronization target")
        await PermissionService(db).check_context_access(
            principal.user_id,
            installation.context_id,
            required_role=ContextRole.EDITOR,
            capacity=CapacityPolicy.ALLOWED,
            key_workspace_id=wid,
        )

        row = await db.get(SyncModeOperation, (iid, command.operation_id))
        request_hash = digest(
            {"preview_id": str(command.preview_id), "diff_hash": command.diff_hash}
        )
        matches = row is not None and (
            row.action == "resume"
            and row.request_hash == request_hash
            and row.actor_user_id == command.original_subject
        )
        result = SyncResumeReceiptView(
            contract_version="sync-resume-receipt-v1",
            workspace_id=wid,
            installation_id=iid,
            operation_id=command.operation_id,
            preview_id=command.preview_id,
            diff_hash=command.diff_hash,
            original_subject=command.original_subject,
            result="not_found" if row is None else "found" if matches else "mismatch",
            receipt=SyncResumeModeReceipt.model_validate(row.result) if matches and row else None,
            as_of=utcnow(),
        )
        # The existing before-commit fence rechecks live credential expiry and
        # rollout flags while membership and transition locks are still held.
        await db.commit()
        return result
