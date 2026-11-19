"""Edge certificate reports of verified custom domains (#2941).

One row per domain claim (``public_mcp_domains.id``): the latest report of the
certificate operator about that claim, as the reporter sent it. The contract is
``docs/public-mcp-domains.md``, "Edge certificate reports".

A report names the claim's id, host name and endpoint id; it is shown only for
the row it names while that row holds its name (``active`` / ``suspended``) and
only inside its own ``valid_until``. No foreign key and no person column: the
migration locks no existing table, and a report about a removed claim is never
shown and is deleted by the domain maintenance pass once its ``valid_until``
has passed. Operational data: never exported, nothing in it for account
erasure. Nothing reads or writes the table while the feature is not configured.

The four times of the report are the reporter's clock; ``received_at`` is the
database clock. All are stored with a time zone.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

__all__ = [
    "REPORT_FRESHNESS_SECONDS",
    "STATUS_CHECK",
    "TLS_REPORT_STATUSES",
    "PublicMcpDomainTlsReport",
]

# What a reporter may say about a claim's certificate.
TLS_REPORT_STATUSES: tuple[str, ...] = (
    "pending",
    "valid",
    "issuance_failed",
    "renewal_failed",
    "expired",
)
# A report expires at most this long after it was observed.
REPORT_FRESHNESS_SECONDS = 300

# The CHECK of the table (and of its revision), spelled out.
STATUS_CHECK = "status IN ('pending', 'valid', 'issuance_failed', 'renewal_failed', 'expired')"


class PublicMcpDomainTlsReport(Base):
    """The latest certificate report about one domain claim.

    Attributes:
        domain_id: The claim the report names (``public_mcp_domains.id``).
        endpoint_id: The publication the report names.
        hostname: The canonical host name the report names.
        status: ``pending``, ``valid``, ``issuance_failed``, ``renewal_failed``
            or ``expired``.
        observed_at: When the reporter observed the certificate.
        valid_until: When the report stops being evidence.
        certificate_not_after: The certificate's end, when known.
        received_at: When the report was stored (database clock).
    """

    __tablename__ = "public_mcp_domain_tls_reports"
    __table_args__ = (
        CheckConstraint(STATUS_CHECK, name="ck_public_mcp_domain_tls_report_status"),
        Index("ix_public_mcp_domain_tls_reports_valid_until", "valid_until"),
    )

    domain_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    certificate_not_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
