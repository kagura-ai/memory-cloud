"""Edge certificate reports bound to one exact domain claim (#2941).

The contract is ``docs/public-mcp-domains.md``, "Edge certificate reports". The
certificate operator reports what it observed about the certificate of one
claim (``PUT /internal/public-mcp-domains/tls-observation``); the owner's
installation-bound observation shows the report as the domain's ``tls``.

- A report names the claim's id, its canonical host name and its endpoint id.
  It is accepted and shown only while that row holds its name (``active`` or
  ``suspended``) and only when it was observed at or after the row was
  created: a new claim of the same name is a new row and starts
  ``unreported``.
- One row per claim in ``public_mcp_domain_tls_reports``, replaced only by a
  report observed later, in one conflict-aware statement. The domain row is
  never locked or written, so the lifecycle's rules hold unchanged.
- Evidence is finite: it expires at most five minutes after it was observed,
  and past its ``valid_until`` it is ``unreported``. A ``valid`` certificate
  past its end is shown as ``expired``; ``valid`` without an end is
  ``unreported``; every other status is shown as reported.
- Nothing reads or writes the table while the feature is not configured
  (``reporting_configured``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from models.public_mcp_domain import HELD_STATES, PublicMcpDomain
from models.public_mcp_domain_tls import REPORT_FRESHNESS_SECONDS, PublicMcpDomainTlsReport

__all__ = [
    "SHOW_MARGIN_SECONDS",
    "CertificateObservation",
    "DomainTLSReport",
    "ObservedCertificate",
    "current_certificate",
    "load_report",
    "reporting_configured",
    "store_report",
]

TlsStatus = Literal["pending", "valid", "issuance_failed", "renewal_failed", "expired"]

# Evidence is shown only while its valid_until is more than this after the
# observation, and a certificate is shown as valid only while its end is more
# than this ahead: every deadline the observation then registers is still
# ahead when it commits and when the answer arrives.
SHOW_MARGIN_SECONDS = 5

# A report time: date, "T", time with optional fraction, and "Z" or an offset.
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")


def reporting_configured(settings: Settings) -> bool:
    """Whether reports are accepted and shown: the reporter credential and both switches."""
    return (
        bool(settings.mcp_public_domains_tls_reporter_token)
        and bool(settings.mcp_public_domains_enabled)
        and bool(settings.mcp_public_endpoints_enabled)
    )


def _utc(value: datetime) -> datetime:
    """An aware UTC instant; a naive value is UTC (the database and application clocks)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CertificateObservation(BaseModel):
    """No current certificate report for this claim."""

    status: Literal["unreported"] = "unreported"


class ObservedCertificate(BaseModel):
    """A current report: four keys, the times in UTC with a trailing ``Z``."""

    model_config = ConfigDict(extra="forbid")

    status: TlsStatus
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    certificate_not_after: AwareDatetime | None

    @field_validator("observed_at", "valid_until", "certificate_not_after", mode="before")
    @classmethod
    def rfc3339(cls, value: object) -> object:
        """RFC 3339 strings with an offset; a number is never read as seconds since the epoch."""
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str) and _RFC3339.fullmatch(value):
            return value
        raise ValueError("A time is an RFC 3339 string with an offset")

    @field_validator("observed_at", "valid_until", "certificate_not_after")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        """Every time in UTC; one outside the representable range is invalid."""
        if value is None:
            return None
        try:
            return value.astimezone(UTC)
        except OverflowError:
            raise ValueError("Timestamp out of range") from None


class DomainTLSReport(BaseModel):
    """The reporter's request body."""

    model_config = ConfigDict(extra="forbid")

    domain_id: UUID
    endpoint_id: UUID
    hostname: str = Field(min_length=1, max_length=253)
    tls: ObservedCertificate

    @model_validator(mode="after")
    def check_window(self) -> Self:
        """A finite window, and a certificate end that agrees with the status."""
        tls = self.tls
        window = tls.valid_until - tls.observed_at
        if not timedelta(0) < window <= timedelta(seconds=REPORT_FRESHNESS_SECONDS):
            raise ValueError("Evidence must expire within five minutes of observation")
        expiry = tls.certificate_not_after
        if tls.status == "valid" and (expiry is None or expiry <= tls.observed_at):
            raise ValueError("A valid certificate needs a future expiry")
        if tls.status == "expired" and (expiry is None or expiry > tls.observed_at):
            raise ValueError("An expired certificate needs an elapsed expiry")
        return self

    def matches(self, row: PublicMcpDomain) -> bool:
        """Whether the report names ``row`` and ``row`` may carry it."""
        return _binds(row, self.domain_id, self.endpoint_id, self.hostname, self.tls.observed_at)


def _binds(
    row: PublicMcpDomain,
    domain_id: UUID,
    endpoint_id: UUID,
    hostname: str,
    observed_at: datetime,
) -> bool:
    """The binding rule: a claim that holds its name, named exactly, observed since its creation."""
    return (
        row.state in HELD_STATES
        and domain_id == row.id
        and hostname == row.hostname
        and endpoint_id == row.endpoint_id
        and _utc(observed_at) >= _utc(row.created_at)
    )


async def store_report(db: AsyncSession, report: DomainTLSReport) -> bool:
    """Store ``report`` as its claim's report, in one statement; whether it is the stored one now.

    The claim's existing report is replaced only by one observed later; the
    same report sent again (an exact retry) is written back unchanged. Anything
    else (observed earlier, or observed at the same instant with other
    content) leaves the stored report and answers False. The caller commits.
    """
    tls = report.tls
    statement = insert(PublicMcpDomainTlsReport).values(
        domain_id=report.domain_id,
        endpoint_id=report.endpoint_id,
        hostname=report.hostname,
        status=tls.status,
        observed_at=tls.observed_at,
        valid_until=tls.valid_until,
        certificate_not_after=tls.certificate_not_after,
        received_at=func.now(),
    )
    incoming = statement.excluded
    stored = PublicMcpDomainTlsReport
    same = and_(
        stored.observed_at == incoming.observed_at,
        stored.endpoint_id == incoming.endpoint_id,
        stored.hostname == incoming.hostname,
        stored.status == incoming.status,
        stored.valid_until == incoming.valid_until,
        stored.certificate_not_after.is_not_distinct_from(incoming.certificate_not_after),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[stored.domain_id],
        set_={
            "endpoint_id": incoming.endpoint_id,
            "hostname": incoming.hostname,
            "status": incoming.status,
            "observed_at": incoming.observed_at,
            "valid_until": incoming.valid_until,
            "certificate_not_after": incoming.certificate_not_after,
            "received_at": incoming.received_at,
        },
        where=or_(stored.observed_at < incoming.observed_at, same),
    ).returning(stored.domain_id)
    return (await db.execute(statement)).scalar_one_or_none() is not None


async def load_report(db: AsyncSession, domain_id: UUID) -> PublicMcpDomainTlsReport | None:
    """The stored report of one claim (a plain read)."""
    return await db.scalar(
        select(PublicMcpDomainTlsReport)
        .where(PublicMcpDomainTlsReport.domain_id == domain_id)
        .execution_options(populate_existing=True)
    )


def current_certificate(
    row: PublicMcpDomain, report: PublicMcpDomainTlsReport | None, observed: datetime
) -> CertificateObservation | ObservedCertificate:
    """The ``tls`` of ``row`` in an observation made at ``observed``.

    ``unreported`` unless the report names this row (its id, host name and
    endpoint), the row holds its name, the report was observed at or after the
    row was created and not after ``observed``, and its ``valid_until`` is more
    than ``SHOW_MARGIN_SECONDS`` after ``observed``. A ``valid`` report whose
    certificate end is not more than the margin ahead is ``expired``; ``valid``
    without an end is ``unreported``.
    """
    if report is None:
        return CertificateObservation()
    instant = _utc(observed)
    observed_at = _utc(report.observed_at)
    valid_until = _utc(report.valid_until)
    if not _binds(row, report.domain_id, report.endpoint_id, report.hostname, observed_at):
        return CertificateObservation()
    margin = timedelta(seconds=SHOW_MARGIN_SECONDS)
    if observed_at > instant:
        return CertificateObservation()
    if instant + margin >= valid_until:
        return CertificateObservation()
    not_after = _utc(report.certificate_not_after) if report.certificate_not_after else None
    status = report.status
    if status == "valid":
        if not_after is None:
            return CertificateObservation()
        if not_after <= instant + margin:
            status = "expired"
    try:
        return ObservedCertificate.model_validate(
            {
                "status": status,
                "observed_at": observed_at,
                "valid_until": valid_until,
                "certificate_not_after": not_after,
            }
        )
    except ValidationError:  # pragma: no cover - the table's CHECK holds the status
        return CertificateObservation()
