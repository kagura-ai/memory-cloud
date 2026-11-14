"""Authority locks and final time boundaries shared by sync control mutations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from auth.sync_identity import SyncPrincipal, load_sync_identity
from auth.workspace_roles import WorkspaceRole
from models.auth import AuditLog, User, Workspace, WorkspaceMember
from models.oidc import OIDCClient
from models.sync_installation import SyncService
from services.permission_service import PermissionService
from services.workspace_locks import lock_workspace_for_update
from utils.datetime import utcnow
from utils.db_helpers import is_lock_not_available
from utils.exceptions import ConflictError, NotFoundException


@dataclass
class SyncAuthority:
    """Held rows and finite bounds, valid only for the current DB transaction."""

    workspace: Workspace
    owner: User
    principal: SyncPrincipal | None
    deadlines: list[datetime] = field(default_factory=list)

    def before_commit(self, _session: Session) -> None:
        """Run after all async work, immediately at the commit authorization boundary."""
        if self.principal is not None:
            self.principal.assert_live()
        if any(utcnow() >= end for end in self.deadlines):
            raise ConflictError("Synchronization approval expired; obtain fresh consent")


async def _lock_user(db: AsyncSession, user_id: str) -> User:
    try:
        user = await db.scalar(
            select(User)
            .where(User.user_id == user_id)
            .with_for_update(read=True, key_share=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        if is_lock_not_available(exc):
            raise ConflictError("Account authority is changing; retry") from exc
        raise
    if user is None:
        raise NotFoundException("Workspace")
    return user


@asynccontextmanager
async def sync_authority(
    db: AsyncSession,
    workspace_id: UUID,
    principal: SyncPrincipal | None,
    *,
    paid_guarded: bool = False,
) -> AsyncIterator[SyncAuthority]:
    """Fence user erasure, ownership and scopes; the mutation owns the commit.

    Paid provisioning enters the paid guard before all row locks, then takes
    User authority NOWAIT. Non-paid controls fence User before Workspace
    NOWAIT, as existing erasure-safe owner operations do. Neither order waits
    while holding the opposing authority lock.
    Internal activation checks the current owner without reusing a human grant.
    """
    authority = None
    try:
        owner_id = await db.scalar(
            select(Workspace.owner_user_id).where(Workspace.id == workspace_id)
        )
        if owner_id is None or (principal is not None and principal.user_id != owner_id):
            raise NotFoundException("Workspace")
        if paid_guarded:
            if not db.info.get("paid_integration_write"):
                raise RuntimeError("Paid synchronization authority requires the transaction guard")
            workspace = await lock_workspace_for_update(db, workspace_id, nowait=True)
            owner = await _lock_user(db, owner_id)
        else:
            owner = await _lock_user(db, owner_id)
            workspace = await lock_workspace_for_update(db, workspace_id, nowait=True)
        if workspace.owner_user_id != owner_id:
            raise ConflictError("Workspace ownership changed; retry")
        if principal is not None:
            original_client = principal.client_id
            principal = await load_sync_identity(
                db, principal.token_hash, principal.scope, lock=True
            )
            if principal.user_id != owner_id or principal.client_id != original_client:
                raise NotFoundException("Workspace")
        member = await PermissionService(db).check_workspace_access(
            owner_id, workspace_id, required_role=WorkspaceRole.OWNER, require_seat=False
        )
        member = await db.scalar(
            select(WorkspaceMember)
            .where(WorkspaceMember.id == member.id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        if member is None or member.role != WorkspaceRole.OWNER:
            raise NotFoundException("Workspace")
        authority = SyncAuthority(workspace, owner, principal)
        event.listen(db.sync_session, "before_commit", authority.before_commit)
        yield authority
    except BaseException:
        await db.rollback()
        raise
    finally:
        if authority is not None:
            event.remove(db.sync_session, "before_commit", authority.before_commit)


async def registered_service(
    db: AsyncSession, *, browser_client_id: str | None = None, service_id: str | None = None
) -> SyncService:
    """Lock an enabled operator binding and its live, correctly scoped browser client."""
    predicate = (
        (SyncService.browser_client_id == browser_client_id)
        if browser_client_id
        else (SyncService.service_id == service_id)
    )
    try:
        service = await db.scalar(
            select(SyncService)
            .where(predicate)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        if service is None or not service.enabled:
            raise ConflictError("Synchronization service is unavailable")
        client = await db.scalar(
            select(OIDCClient)
            .where(OIDCClient.client_id == service.browser_client_id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        if is_lock_not_available(exc):
            raise ConflictError("Service authority is changing; retry") from exc
        raise
    if (
        client is None
        or client.revoked_at is not None
        or "integrations:manage" not in client.scope.split()
    ):
        raise ConflictError("Synchronization service is unavailable")
    return service


def audit_sync(db: AsyncSession, authority: SyncAuthority, action: str, target: UUID) -> None:
    """Use established erasure-aware audit storage; never record public text or credentials."""
    human = authority.principal is not None
    db.add(
        AuditLog(
            user_id=authority.owner.user_id if human else "service:billing-publisher",
            user_email=authority.owner.email if human else "system:billing-publisher",
            action=action,
            resource=f"sync:{target}",
            user_metadata={"workspace_id": str(authority.workspace.id)},
        )
    )
