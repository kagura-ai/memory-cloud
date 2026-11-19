"""Private, finite observations of the exact publication bound to an installation."""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from api.routes.public_mcp_domains import PublicMcpDomainResponse, domain_response
from auth.sync_identity import SyncPrincipal
from models.api_base import TZAwareBaseModel
from models.public_mcp_domain import PublicMcpDomain
from models.sync_installation import SyncAuthorization, SyncInstallation
from services.domain_tls_observations import (
    CertificateObservation,
    ObservedCertificate,
    current_certificate,
)
from services.sync_authority import registered_service, sync_authority
from services.sync_provisioning import installation_view
from utils.datetime import utcnow
from utils.exceptions import NotFoundException


class LiveDomain(PublicMcpDomainResponse):
    tls: CertificateObservation | ObservedCertificate


class ReleasedDomain(TZAwareBaseModel):
    state: Literal["released"] = "released"
    domain_id: UUID
    hostname: str
    released_at: datetime
    cooldown_until: datetime


class PublicationDomainObservation(TZAwareBaseModel):
    schema_version: Literal["public-domain-observation-v1"] = "public-domain-observation-v1"
    subject: str
    workspace_id: UUID
    installation_id: UUID
    application_id: str
    resource_id: str
    resource_pk: UUID
    context_id: UUID
    endpoint_id: UUID
    public_id: str
    generation: int
    observed_at: datetime
    valid_until: datetime
    domain: LiveDomain | ReleasedDomain | None


async def observe_publication_domain(
    db: AsyncSession, workspace_id: UUID, installation_id: UUID, principal: SyncPrincipal
) -> PublicationDomainObservation:
    """Read current owner, registered client, target identity and one domain in the transaction."""
    async with sync_authority(db, workspace_id, principal) as authority:
        service = await registered_service(db, browser_client_id=principal.client_id)
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
        )
        if installation is None:
            raise NotFoundException("Synchronization installation")
        target = await installation_view(db, installation)
        if not target.public_id:
            raise NotFoundException("Publication target")

        # Cooldown is per workspace. The same owner may already have verified
        # this hostname on a different endpoint, even before cooldown ends.
        successor = aliased(PublicMcpDomain)
        superseded = (
            select(successor.id)
            .where(
                successor.hostname == PublicMcpDomain.hostname,
                successor.id != PublicMcpDomain.id,
                successor.verified_at >= PublicMcpDomain.released_at,
            )
            .exists()
        )
        observed = utcnow()
        row = await db.scalar(
            select(PublicMcpDomain)
            .where(
                PublicMcpDomain.workspace_id == workspace_id,
                PublicMcpDomain.endpoint_id == target.endpoint_id,
                (PublicMcpDomain.state != "released")
                | ((PublicMcpDomain.cooldown_until > observed) & ~superseded),
            )
            .order_by(
                (PublicMcpDomain.state == "released"),
                PublicMcpDomain.released_at.desc(),
                PublicMcpDomain.id,
            )
            .limit(1)
            .with_for_update(read=True, nowait=True)
        )
        assert authority.principal is not None
        until = min(observed + timedelta(seconds=30), authority.principal.expires_at)
        domain: LiveDomain | ReleasedDomain | None = None
        if row is not None and row.state == "released":
            assert row.released_at is not None and row.cooldown_until is not None
            domain = ReleasedDomain(
                domain_id=row.id,
                hostname=row.hostname,
                released_at=row.released_at,
                cooldown_until=row.cooldown_until,
            )
            until = min(until, row.cooldown_until)
        elif row is not None:
            live = await domain_response(db, row)
            domain = LiveDomain(**live.model_dump(), tls=current_certificate(row, observed))
            if live.serving and row.serve_until is not None:
                until = min(until, row.serve_until)
        authority.deadlines.append(until)
        result = PublicationDomainObservation(
            subject=principal.user_id,
            workspace_id=workspace_id,
            installation_id=installation_id,
            application_id=target.application_id,
            resource_id=target.resource_id,
            resource_pk=target.resource_pk,
            context_id=target.context_id,
            endpoint_id=target.endpoint_id,
            public_id=target.public_id,
            generation=target.generation,
            observed_at=observed,
            valid_until=until,
            domain=domain,
        )
        await db.commit()
        return result
