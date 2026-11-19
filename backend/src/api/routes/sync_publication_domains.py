"""Private, finite observations of the exact publication bound to an installation."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from api.routes.public_mcp_domains import PublicMcpDomainResponse, domain_response
from auth.sync_identity import SyncPrincipal
from config.settings import get_settings
from models.api_base import TZAwareBaseModel
from models.public_mcp_domain import PublicMcpDomain
from services.domain_tls_observations import (
    CertificateObservation,
    ObservedCertificate,
    current_certificate,
    load_report,
    reporting_configured,
)
from services.sync_authority import registered_service, sync_authority
from services.sync_publication_domain_commands import bound_installation
from utils.datetime import utcnow


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
        # The selection the domain commands admit with (#2921).
        _installation, target = await bound_installation(
            db, workspace_id, installation_id, principal, service
        )
        assert target.public_id is not None

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
            tls: CertificateObservation | ObservedCertificate = CertificateObservation()
            # #2941: the certificate report is read only while reporting is configured.
            if reporting_configured(get_settings()):
                tls = current_certificate(row, await load_report(db, row.id), observed)
            domain = LiveDomain(**live.model_dump(), tls=tls)
            if live.serving and row.serve_until is not None:
                until = min(until, row.serve_until)
            if isinstance(tls, ObservedCertificate):
                # The answer never outlives the evidence it carries, and a
                # valid answer never outlives the certificate. Both are more
                # than the show margin ahead, so these deadlines are still
                # ahead at the commit.
                until = min(until, tls.valid_until.astimezone(UTC).replace(tzinfo=None))
                if tls.status == "valid" and tls.certificate_not_after is not None:
                    end = tls.certificate_not_after.astimezone(UTC).replace(tzinfo=None)
                    until = min(until, end)
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
