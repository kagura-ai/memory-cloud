"""Finite edge evidence attached to one exact KMC domain claim."""

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

from models.public_mcp_domain import PublicMcpDomain


class CertificateObservation(BaseModel):
    status: Literal["unreported"] = "unreported"


class ObservedCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "valid", "issuance_failed", "renewal_failed", "expired"]
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    certificate_not_after: AwareDatetime | None

    @field_validator("observed_at", "valid_until", "certificate_not_after")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class DomainTLSReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_id: UUID
    endpoint_id: UUID
    hostname: str = Field(min_length=1, max_length=253)
    tls: ObservedCertificate

    @model_validator(mode="after")
    def check_window(self) -> Self:
        tls = self.tls
        if not timedelta(0) < tls.valid_until - tls.observed_at <= timedelta(minutes=5):
            raise ValueError("Evidence must expire within five minutes of observation")
        expiry = tls.certificate_not_after
        if tls.status == "valid" and (expiry is None or expiry <= tls.observed_at):
            raise ValueError("A valid certificate needs a future expiry")
        if tls.status == "expired" and (expiry is None or expiry > tls.observed_at):
            raise ValueError("An expired certificate needs an elapsed expiry")
        return self

    def matches(self, row: PublicMcpDomain) -> bool:
        return (
            row.state != "released"
            and (self.domain_id, self.endpoint_id, self.hostname)
            == (row.id, row.endpoint_id, row.hostname)
            and self.tls.observed_at >= row.created_at.replace(tzinfo=UTC)
        )


def current_certificate(
    row: PublicMcpDomain, now: datetime
) -> CertificateObservation | ObservedCertificate:
    try:
        report = DomainTLSReport.model_validate(row.tls_report)
    except ValidationError:
        return CertificateObservation()
    instant = now.replace(tzinfo=UTC)
    tls = report.tls
    if not report.matches(row) or not tls.observed_at <= instant < tls.valid_until:
        return CertificateObservation()
    if tls.status == "valid" and tls.certificate_not_after and tls.certificate_not_after <= instant:
        return tls.model_copy(update={"status": "expired"})
    return tls
