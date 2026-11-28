"""Who may read an installation's public usage statistics, and of which endpoint (#3035).

``GET /api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/publication-usage-stats``
gives the workspace's canonical owner, signed in through the installation's
registered browser client with ``workspaces:read``, the statistics the owner
route gives for the installation's public endpoint: the route answers through
``api.routes.public_mcp_usage.usage_stats_response`` (the same rate bucket,
read, time budget and body). Experimental; off unless the operator turns
``SYNC_PUBLICATION_USAGE_STATS_ENABLED`` on.

The admission is the custom-domain observation's chain, in one short
transaction that is committed before the statistics are read, so no
authority lock is held during the read: ``sync_authority`` (the canonical
owner, the workspace slot gate), ``registered_service`` and
``bound_installation`` (this browser client's installation of the service's
generation, in any state, and its live target). An installation whose public
endpoint is not live is the 404.

A caller who is not the canonical owner is told so (403 ``owner_required``)
only when they are a current member of the workspace who reaches the
installation: the member read of the installation (``inspect_installation``)
admits them, and the installation's registered service is bound to their
browser client. Every other caller gets the uniform 404. Nothing is written.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal
from auth.workspace_roles import WorkspaceRole
from models.auth import Workspace
from models.sync_installation import SyncInstallation, SyncService
from services.permission_service import CapacityPolicy, PermissionService
from services.public_mcp_usage_stats import LiveEndpoint
from services.sync_authority import registered_service, sync_authority
from services.sync_publication_domain_commands import bound_installation
from utils.db_helpers import is_lock_not_available
from utils.exceptions import (
    AuthorizationError,
    NotFoundException,
    PublicationUsageStatsOwnerRequiredError,
    SyncLockBusyError,
)

__all__ = ["installation_stats_endpoint"]


async def _member_reaches(
    db: AsyncSession, workspace_id: UUID, installation_id: UUID, principal: SyncPrincipal
) -> bool:
    """Whether a current member of the workspace reaches the installation.

    The member read of the installation (``inspect_installation``): a
    membership of the live workspace, any role, and the installation's context
    readable; and the installation's registered service bound to the caller's
    browser client. A slot-suspended workspace raises ``SLOT-001`` as there.
    """
    permissions = PermissionService(db)
    try:
        await permissions.check_workspace_access(
            principal.user_id, workspace_id, required_role=WorkspaceRole.VIEWER, require_seat=False
        )
        context_id = await db.scalar(
            select(SyncInstallation.context_id)
            .join(SyncService, SyncService.service_id == SyncInstallation.service_id)
            .where(
                SyncInstallation.id == installation_id,
                SyncInstallation.workspace_id == workspace_id,
                SyncService.browser_client_id == principal.client_id,
            )
        )
        if context_id is None:
            return False
        await permissions.check_context_access(
            principal.user_id,
            context_id,
            capacity=CapacityPolicy.ALLOWED,
            key_workspace_id=workspace_id,
        )
    except (AuthorizationError, NotFoundException):
        return False
    return True


async def installation_stats_endpoint(
    db: AsyncSession, workspace_id: UUID, installation_id: UUID, principal: SyncPrincipal
) -> tuple[LiveEndpoint, str]:
    """The installation's live public endpoint and its resource slug, for the canonical owner.

    Args:
        db: The request's session; committed (owner) or rolled back (refusal)
            before this returns, so the read that follows starts a transaction
            of its own.
        workspace_id: The path workspace.
        installation_id: The path installation.
        principal: The ``workspaces:read`` login.

    Returns:
        ``(endpoint, resource_id)``.

    Raises:
        PublicationUsageStatsOwnerRequiredError: A current member who reaches
            the installation and is not the canonical owner (403).
        NotFoundException: Every other caller, workspace or installation, and
            an installation whose public endpoint is not live (404).
        WorkspaceSlotSuspendedError: The workspace is slot-suspended (403).
        ConflictError: A row of the admission is held (409, the installation
            row included: ``SyncLockBusyError``), or the client's service
            registration is unavailable, as on the observation.
    """
    try:
        owner_id = await db.scalar(
            select(Workspace.owner_user_id).where(Workspace.id == workspace_id)
        )
        if owner_id is not None and owner_id != principal.user_id:
            if await _member_reaches(db, workspace_id, installation_id, principal):
                raise PublicationUsageStatsOwnerRequiredError()
            raise NotFoundException("Synchronization installation")
    except BaseException:
        await db.rollback()
        raise
    async with sync_authority(db, workspace_id, principal):
        service = await registered_service(db, browser_client_id=principal.client_id)
        try:
            _installation, target = await bound_installation(
                db, workspace_id, installation_id, principal, service
            )
        except DBAPIError as exc:
            # The installation row and its authorization are read NOWAIT; a
            # held one is the retry answer of every other admission row.
            if is_lock_not_available(exc):
                raise SyncLockBusyError() from exc
            raise
        assert target.public_id is not None
        endpoint = LiveEndpoint(
            id=target.endpoint_id,
            public_id=target.public_id,
            resource_pk=target.resource_pk,
            context_id=target.context_id,
        )
        resource_id = target.resource_id
        await db.commit()
    return endpoint, resource_id
