"""The owner's custom-domain commands for one installation's publication (#2921).

Experimental. The workspace's canonical owner, signed in through the
installation's registered browser client with ``integrations:manage``, claims,
verifies and removes the custom domain of the installation's public endpoint.
A command names the binding of the installation the owner last read
(``expected``) and, for verify and remove, the domain row and its state; it is
refused when that state is gone. Nothing is stored per command.

The commands run the custom-domain lifecycle of ``services.public_mcp_domains``
with a command guard (I8 there): every write transaction admits the caller
again as its first locking statement, with the owner, the token, the
installation, the binding and the ownership epoch the first admission saw. A
caller who is not admitted gets one 404 before any counter, lookup or paid
check (the pre-admission).

Lock order of an admission. Outside the paid guard: the ``users`` row
``FOR KEY SHARE NOWAIT``, then the workspace row ``FOR UPDATE NOWAIT`` (tried
again for a moment), then the OIDC client, grant and token rows, the owner's
membership, the service registration and its client, the installation and its
authorization (all ``FOR SHARE NOWAIT``). Inside the paid guard: the workspace
row ``FOR UPDATE`` (the guard's, which waits), the same row again ``NOWAIT``,
the ``users`` row ``NOWAIT``, then the same rest. No transaction holding a
``users`` row waits on a workspace row.

This module creates, republishes or rebinds no publication, resource, context
or installation: an unavailable target is a 404 and is never repaired.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.sync_identity import SyncPrincipal
from models.sync_installation import SyncAuthorization, SyncInstallation, SyncService
from models.sync_installation_schemas import SyncInstallationView
from models.sync_publication_domain_schemas import (
    DomainRowState,
    OpenDomainState,
    PublicationBindingExpectation,
    PublicationDomainClaim,
    PublicationDomainCommand,
    PublicationDomainCommandResult,
)
from services.public_mcp_domains import (
    PublicMcpDomainCommandStaleError,
    PublicMcpDomainOwnerRequiredError,
    attach_domain,
    detach_domain,
    verify_domain,
)
from services.sync_authority import registered_service, sync_authority
from services.sync_provisioning import installation_view
from utils.db_helpers import is_lock_not_available
from utils.exceptions import NotFoundException, SyncLockBusyError

__all__ = [
    "DomainCommand",
    "InstallationDomainGuard",
    "bound_installation",
    "claim_publication_domain",
    "remove_publication_domain",
    "verify_publication_domain",
]


async def bound_installation(
    db: AsyncSession,
    workspace_id: UUID,
    installation_id: UUID,
    principal: SyncPrincipal,
    service: SyncService,
) -> tuple[SyncInstallation, SyncInstallationView]:
    """The workspace's installation of this browser client and service generation, and its target.

    The one selection the observation and the commands share. The installation
    row and its authorization are read ``FOR SHARE NOWAIT``; the caller decides
    what a busy row answers. No such installation, or a target that is not live
    and exact, is a 404 (never repaired). The row is read into the session as
    it is now, also when an earlier admission of the same session loaded it.
    """
    installation = await db.scalar(
        select(SyncInstallation)
        .join(SyncAuthorization, SyncAuthorization.id == SyncInstallation.authorization_id)
        .where(
            SyncInstallation.id == installation_id,
            SyncInstallation.workspace_id == workspace_id,
            SyncInstallation.service_id == service.service_id,
            SyncInstallation.service_client_id == service.service_client_id,
            SyncAuthorization.browser_client_id == principal.client_id,
            SyncAuthorization.service_generation == service.generation,
        )
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    if installation is None:
        raise NotFoundException("Synchronization installation")
    target = await installation_view(db, installation)
    if not target.public_id:
        raise NotFoundException("Publication target")
    return installation, target


@dataclass(frozen=True)
class DomainCommand:
    """One command: the path's selection, the caller and the binding it confirmed."""

    workspace_id: UUID
    installation_id: UUID
    principal: SyncPrincipal
    expected: PublicationBindingExpectation


def _binding(target: SyncInstallationView) -> tuple[object, ...]:
    return (
        target.application_id,
        target.resource_id,
        target.resource_pk,
        target.context_id,
        target.endpoint_id,
        target.public_id,
        target.generation,
    )


def _expected(expected: PublicationBindingExpectation) -> tuple[object, ...]:
    return (
        expected.application_id,
        expected.resource_id,
        expected.resource_pk,
        expected.context_id,
        expected.endpoint_id,
        expected.public_id,
        expected.generation,
    )


@asynccontextmanager
async def _admitted(
    db: AsyncSession, command: DomainCommand, *, paid_guarded: bool
) -> AsyncIterator[int]:
    """Admit the caller in the current transaction and yield the workspace's ownership epoch.

    The canonical owner and ``OWNER`` member with a live ``integrations:manage``
    token, the registered browser client, the installation of the path and
    the binding exactly as the command names it. The locks are held until the
    transaction ends.
    """
    async with sync_authority(
        db, command.workspace_id, command.principal, paid_guarded=paid_guarded
    ) as authority:
        service = await registered_service(db, browser_client_id=command.principal.client_id)
        try:
            _installation, target = await bound_installation(
                db, command.workspace_id, command.installation_id, command.principal, service
            )
        except DBAPIError as exc:
            if is_lock_not_available(exc):
                raise SyncLockBusyError() from exc
            raise
        if _binding(target) != _expected(command.expected):
            raise PublicMcpDomainCommandStaleError()
        yield authority.workspace.ownership_epoch


async def _precheck(db: AsyncSession, command: DomainCommand) -> int:
    """The pre-admission: decide before any counter, lookup or paid check; return the epoch."""
    async with _admitted(db, command, paid_guarded=False) as epoch:
        await db.commit()
    return epoch


@dataclass(frozen=True)
class InstallationDomainGuard:
    """The command guard the lifecycle admits in each write transaction.

    It binds the command to the publication of ``expected`` and to the
    ownership epoch of its pre-admission: an epoch that changed during the
    command is a stale command.
    """

    command: DomainCommand
    epoch: int
    expected_state: str | None

    @property
    def endpoint_id(self) -> UUID:
        """The only publication the command may touch."""
        return self.command.expected.endpoint_id

    @property
    def audit_fields(self) -> tuple[tuple[str, str], ...]:
        """The browser client and the installation, on each audit row of the command."""
        return (
            ("client_id", self.command.principal.client_id),
            ("installation_id", str(self.command.installation_id)),
        )

    @asynccontextmanager
    async def admitted(self, db: AsyncSession, *, paid_guarded: bool) -> AsyncIterator[None]:
        """Admit the caller again; held until the lifecycle's commit."""
        async with _admitted(db, self.command, paid_guarded=paid_guarded) as epoch:
            if epoch != self.epoch:
                raise PublicMcpDomainCommandStaleError()
            yield


@asynccontextmanager
async def _uniform_refusals(db: AsyncSession) -> AsyncIterator[None]:
    """Release the request's connection on every refusal; one 404 for a lost owner.

    The lifecycle's own owner read can refuse only between an admission and
    its next one; it answers like every other caller who is not admitted.
    """
    try:
        yield
    except BaseException as exc:
        await db.rollback()
        if isinstance(exc, PublicMcpDomainOwnerRequiredError):
            raise NotFoundException("Workspace") from exc
        raise


def _open_domain(domain_id: UUID, state: str) -> DomainRowState:
    return DomainRowState(domain_id=domain_id, state=cast(OpenDomainState, state))


async def claim_publication_domain(
    db: AsyncSession,
    workspace_id: UUID,
    installation_id: UUID,
    principal: SyncPrincipal,
    body: PublicationDomainClaim,
) -> PublicationDomainCommandResult:
    """Claim ``body.hostname`` for the installation's publication (experimental).

    Created only when the publication has no open domain; the claim is
    ``pending`` with a new TXT value.
    """
    command = DomainCommand(workspace_id, installation_id, principal, body.expected)
    async with _uniform_refusals(db):
        epoch = await _precheck(db, command)
        guard = InstallationDomainGuard(command, epoch, None)
        row = await attach_domain(
            db,
            principal.user_id,
            workspace_id,
            body.expected.resource_id,
            body.hostname,
            guard=guard,
        )
    return PublicationDomainCommandResult(action="claim", domain=_open_domain(row.id, row.state))


async def verify_publication_domain(
    db: AsyncSession,
    workspace_id: UUID,
    installation_id: UUID,
    principal: SyncPrincipal,
    body: PublicationDomainCommand,
) -> PublicationDomainCommandResult:
    """Check the named domain's proof now and apply the result (experimental).

    The answer is the state the command's own write left; ``domain`` is
    ``null`` when that check released the domain.
    """
    command = DomainCommand(workspace_id, installation_id, principal, body.expected)
    async with _uniform_refusals(db):
        epoch = await _precheck(db, command)
        guard = InstallationDomainGuard(command, epoch, body.domain.state)
        state = await verify_domain(
            db,
            principal.user_id,
            workspace_id,
            body.expected.resource_id,
            body.domain.domain_id,
            guard=guard,
        )
    domain = None if state == "released" else _open_domain(body.domain.domain_id, state)
    return PublicationDomainCommandResult(action="verify", domain=domain)


async def remove_publication_domain(
    db: AsyncSession,
    workspace_id: UUID,
    installation_id: UUID,
    principal: SyncPrincipal,
    body: PublicationDomainCommand,
) -> PublicationDomainCommandResult:
    """Remove the named domain while it is in the named state (experimental).

    ``pending``: deleted. ``active`` / ``suspended``: released with the cooldown.
    """
    command = DomainCommand(workspace_id, installation_id, principal, body.expected)
    async with _uniform_refusals(db):
        epoch = await _precheck(db, command)
        guard = InstallationDomainGuard(command, epoch, body.domain.state)
        await detach_domain(
            db,
            principal.user_id,
            workspace_id,
            body.expected.resource_id,
            body.domain.domain_id,
            guard=guard,
        )
    return PublicationDomainCommandResult(action="remove", domain=None)
