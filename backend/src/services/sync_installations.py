"""Recorded owner consent and workspace-owned, idempotent sync provisioning."""

from __future__ import annotations

import hashlib
import json
from contextlib import AsyncExitStack
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal
from auth.workspace_roles import WorkspaceRole
from config.settings import get_settings
from models.auth import Workspace
from models.sync_installation import (
    SYNC_OPERATIONS,
    SyncAuthorization,
    SyncInstallation,
    SyncService,
)
from models.sync_installation_schemas import (
    SyncActivation,
    SyncAuthorizationCreate,
    SyncAuthorizationView,
    SyncInstallationView,
)
from services.capacity_lock import CapacityPolicy
from services.integration_entitlements import paid_integration_write
from services.permission_service import PermissionService
from services.sync_authority import SyncAuthority, audit_sync, registered_service, sync_authority
from services.sync_provisioning import create_sync_target, installation_view
from utils.datetime import utcnow
from utils.exceptions import ConflictError, NotFoundException


def authorization_view(row: SyncAuthorization) -> SyncAuthorizationView:
    """Return consent metadata, never a credential or reusable authority claim."""
    return SyncAuthorizationView(
        authorization_id=row.id,
        workspace_id=row.workspace_id,
        application_id=row.application_id,
        service_id=row.service_id,
        operations=row.operations,
        ownership_epoch=row.ownership_epoch,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        used_at=row.used_at,
    )


async def _approval(db: AsyncSession, wid: UUID, authorization_id: UUID) -> SyncAuthorization:
    row = await db.scalar(
        select(SyncAuthorization)
        .where(
            SyncAuthorization.id == authorization_id,
            SyncAuthorization.workspace_id == wid,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundException("Synchronization authorization")
    return row


async def _installation(db: AsyncSession, wid: UUID) -> SyncInstallation | None:
    return await db.scalar(
        select(SyncInstallation)
        .where(SyncInstallation.workspace_id == wid)
        .execution_options(populate_existing=True)
    )


def _consent_hash(
    body: SyncAuthorizationCreate, authority: SyncAuthority, service: SyncService
) -> str:
    value = {
        "body": body.model_dump(mode="json"),
        "workspace": str(authority.workspace.id),
        "owner": authority.owner.user_id,
        "epoch": authority.workspace.ownership_epoch,
        "service": service.service_id,
        "generation": service.generation,
        "browser_client": service.browser_client_id,
        "service_client": service.service_client_id,
        "operations": SYNC_OPERATIONS,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def create_authorization(
    db: AsyncSession, wid: UUID, principal: SyncPrincipal, body: SyncAuthorizationCreate
) -> SyncAuthorizationView:
    """Record explicit finite consent, superseding pending consent only on a new request."""
    async with sync_authority(db, wid, principal) as authority:
        service = await registered_service(db, browser_client_id=principal.client_id)
        digest = _consent_hash(body, authority, service)
        existing = await db.scalar(
            select(SyncAuthorization).where(
                SyncAuthorization.workspace_id == wid,
                SyncAuthorization.request_id == body.request_id,
            )
        )
        if existing is not None:
            if existing.request_hash != digest:
                raise ConflictError(
                    "Approval request was already used with different authority or content"
                )
            result = authorization_view(existing)
        else:
            now = utcnow()
            pending = (
                await db.scalars(
                    select(SyncAuthorization).where(
                        SyncAuthorization.workspace_id == wid,
                        SyncAuthorization.application_id == body.application_id,
                        SyncAuthorization.service_id == service.service_id,
                        SyncAuthorization.used_at.is_(None),
                        SyncAuthorization.revoked_at.is_(None),
                    )
                )
            ).all()
            for old in pending:
                old.revoked_at = now
                audit_sync(db, authority, "sync.authorization.superseded", old.id)
            approval = SyncAuthorization(
                id=uuid4(),
                workspace_id=wid,
                request_id=body.request_id,
                application_id=body.application_id,
                service_id=service.service_id,
                service_generation=service.generation,
                browser_client_id=service.browser_client_id,
                service_client_id=service.service_client_id,
                operations=list(SYNC_OPERATIONS),
                authorized_by=authority.owner.user_id,
                ownership_epoch=authority.workspace.ownership_epoch,
                request_hash=digest,
                public_name=body.public_name,
                public_description=body.public_description,
                public_usage_guide=body.public_usage_guide,
                created_at=now,
                expires_at=now + timedelta(seconds=get_settings().sync_authorization_ttl_seconds),
            )
            db.add(approval)
            audit_sync(db, authority, "sync.authorization.created", approval.id)
            await db.flush()
            result = authorization_view(approval)
        await db.commit()
        return result


async def revoke_authorization(
    db: AsyncSession, wid: UUID, principal: SyncPrincipal, authorization_id: UUID
) -> SyncAuthorizationView:
    """Withdraw unused consent; active installations require their separate unlink operation."""
    async with sync_authority(db, wid, principal) as authority:
        approval = await _approval(db, wid, authorization_id)
        if approval.used_at is not None:
            raise ConflictError("Authorization was consumed; unlink the installation instead")
        if approval.revoked_at is None:
            approval.revoked_at = utcnow()
            audit_sync(db, authority, "sync.authorization.revoked", approval.id)
        result = authorization_view(approval)
        await db.commit()
        return result


def _validate_pending(
    approval: SyncAuthorization, authority: SyncAuthority, service: SyncService
) -> None:
    if (
        approval.revoked_at is not None
        or approval.used_at is not None
        or approval.expires_at <= utcnow()
        or approval.authorized_by != authority.owner.user_id
        or approval.ownership_epoch != authority.workspace.ownership_epoch
        or approval.service_generation != service.generation
        or approval.browser_client_id != service.browser_client_id
        or approval.service_client_id != service.service_client_id
        or approval.operations != list(SYNC_OPERATIONS)
    ):
        raise ConflictError("Synchronization approval is no longer valid; obtain fresh consent")


def _match_application(
    approval: SyncAuthorization, body: SyncActivation, principal: SyncPrincipal | None
) -> None:
    if approval.application_id != body.application_id:
        raise ConflictError("Application does not match the recorded authorization")
    if principal is not None and principal.client_id != approval.browser_client_id:
        raise NotFoundException("Synchronization authorization")


async def _existing_result(
    db: AsyncSession, row: SyncInstallation, approval: SyncAuthorization
) -> SyncInstallationView:
    if row.authorization_id != approval.id or row.application_id != approval.application_id:
        raise ConflictError("Workspace already has a retained synchronization target")
    result = await installation_view(db, row)
    if not result.target_available:
        raise ConflictError("The retained synchronization target is unavailable")
    return result


async def validate_authorization(
    db: AsyncSession, wid: UUID, body: SyncActivation
) -> SyncAuthorizationView:
    """Pre-charge consent check for billing; not a reservation or provisioning grant."""
    async with sync_authority(db, wid, None) as authority:
        approval = await _approval(db, wid, body.authorization_id)
        _match_application(approval, body, None)
        service = await registered_service(db, service_id=approval.service_id)
        _validate_pending(approval, authority, service)
        if await _installation(db, wid) is not None:
            raise ConflictError("Workspace already has a retained synchronization target")
        authority.deadlines.append(approval.expires_at)
        result = authorization_view(approval)
        await db.commit()
        return result


async def activate_installation(
    db: AsyncSession, wid: UUID, body: SyncActivation, principal: SyncPrincipal | None = None
) -> SyncInstallationView:
    """Provision from owner authority or trusted internal billing, without a human-token dependency."""
    # Only plain reads precede paid_integration_write. Its transaction-id fence
    # (#2801) must remain the first write/row-lock boundary for real creation.
    initial = await _approval(db, wid, body.authorization_id)
    if await _installation(db, wid) is not None:
        async with sync_authority(db, wid, principal):
            approval = await _approval(db, wid, body.authorization_id)
            _match_application(approval, body, principal)
            existing = await _installation(db, wid)
            if existing is None:
                raise ConflictError("Retained synchronization binding changed; retry")
            # Inspection never refreshes consent, generation or active state.
            result = await _existing_result(db, existing, approval)
            await db.commit()
            return result
    service_id = initial.service_id
    # The outer stack retains the authority's before_commit deadline hook
    # until AFTER the inner paid guard has flushed, rechecked and committed.
    async with AsyncExitStack() as boundaries:
        async with paid_integration_write(db, wid, service_id):
            authority = await boundaries.enter_async_context(
                sync_authority(db, wid, principal, paid_guarded=True)
            )
            approval = await _approval(db, wid, body.authorization_id)
            _match_application(approval, body, principal)
            existing = await _installation(db, wid)
            if existing is not None:
                return await _existing_result(db, existing, approval)
            service = await registered_service(db, service_id=service_id)
            if approval.service_id != service_id:
                raise ConflictError("Recorded synchronization service changed; retry")
            _validate_pending(approval, authority, service)
            authority.deadlines.append(approval.expires_at)
            resource, context, endpoint = await create_sync_target(
                db, authority.workspace, approval
            )
            row = SyncInstallation(
                id=uuid4(),
                workspace_id=wid,
                service_id=service.service_id,
                service_client_id=service.service_client_id,
                authorization_id=approval.id,
                application_id=approval.application_id,
                operations=list(approval.operations),
                resource_pk=resource.id,
                context_id=context.id,
                resource_id=resource.resource_id,
                endpoint_id=endpoint.id,
                state="active",
                generation=1,
            )
            db.add(row)
            approval.used_at = utcnow()
            audit_sync(db, authority, "sync.installation.activated", row.id)
            await db.flush()
            result = await installation_view(db, row)
        return result


async def inspect_installation(
    db: AsyncSession, wid: UUID, principal: SyncPrincipal, installation_id: UUID
) -> SyncInstallationView:
    """Inspect only a current membership's visible fixed target, with no paid-rights requirement."""
    permissions = PermissionService(db)
    member = await permissions.check_workspace_access(
        principal.user_id, wid, required_role=WorkspaceRole.VIEWER, require_seat=False
    )
    row = await _installation(db, wid)
    if row is None or row.id != installation_id:
        raise NotFoundException("Synchronization installation")
    workspace = await db.get(Workspace, wid)
    canonical_owner = (
        workspace is not None
        and workspace.owner_user_id == principal.user_id
        and member.role == WorkspaceRole.OWNER
    )
    if not canonical_owner:
        await permissions.check_context_access(
            principal.user_id, row.context_id, capacity=CapacityPolicy.ALLOWED, key_workspace_id=wid
        )
    return await installation_view(db, row)


async def unlink_installation(
    db: AsyncSession, wid: UUID, principal: SyncPrincipal, installation_id: UUID
) -> SyncInstallationView:
    """Advance generation once; retain the installation, data and public endpoint identities."""
    async with sync_authority(db, wid, principal) as authority:
        row = await _installation(db, wid)
        if row is None or row.id != installation_id:
            raise NotFoundException("Synchronization installation")
        if row.state != "revoked":
            row.state, row.revoked_at = "revoked", utcnow()
            row.generation += 1
            audit_sync(db, authority, "sync.installation.revoked", row.id)
        result = await installation_view(db, row)
        await db.commit()
        return result
