"""Verified custom domains of public MCP endpoints (#2809).

A row binds one host name to one publication (``public_mcp_endpoints.id``,
the publication incarnation) of one workspace. The contract is
``docs/public-mcp-domains.md``; the constants below are repeated there.

No foreign key: the migration locks no existing table, and a row survives the
hard delete of its workspace as a tombstone that the maintenance job releases
with a cooldown. No person column: actors go to ``audit_logs``. The host name
lives on this row only and is deleted with it.

Every time on the row is the database clock (``timezone('UTC',
statement_timestamp())``, naive UTC).

States: ``pending`` (claimed, holds nothing), ``active`` (served while
``serve_until`` is in the future), ``suspended`` (held, not served),
``released`` (not held; another workspace waits for ``cooldown_until``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.json_types import NullableJSONB
from utils.public_hostname import MAX_HOSTNAME_LENGTH

__all__ = [
    "ACTIVATIONS_PER_DAY",
    "CHALLENGE_LABEL",
    "CHALLENGE_VALUE_PREFIX",
    "CHECK_TIMEOUT_SECONDS",
    "DOMAIN_SERVICE_ID",
    "HELD_STATES",
    "MAINTENANCE_BATCH",
    "MAINTENANCE_CONCURRENCY",
    "MAINTENANCE_INTERVAL_SECONDS",
    "MAX_HOSTNAME_LENGTH",
    "PENDING_TTL_DAYS",
    "PROOF_GRACE_HOURS",
    "PROOF_MIN_MISSING_CHECKS",
    "PublicMcpDomain",
    "RECHECK_AGE_SECONDS",
    "SERVE_LEASE_HOURS",
    "SUSPENDED_HOLD_DAYS",
    "VERIFY_LIMIT_PER_HOUR",
]

# The paid integration license that grants the right (reserved service id).
DOMAIN_SERVICE_ID = "public-mcp-domains"
# Proof of control: TXT ``<CHALLENGE_LABEL>.<hostname>`` = prefix + token.
CHALLENGE_LABEL = "_kagura-public-mcp"
CHALLENGE_VALUE_PREFIX = "kagura-public-mcp-verification="
# A proof is lost after this many confirmed absences AND this many hours
# since it was last seen.
PROOF_GRACE_HOURS = 72
PROOF_MIN_MISSING_CHECKS = 3
# A suspended name stays held this long, then it is released.
SUSPENDED_HOLD_DAYS = 30
# An unverified claim is deleted after this long.
PENDING_TTL_DAYS = 7
# Every completed check under an active right serves the alias this long.
SERVE_LEASE_HOURS = 72
# The maintenance job re-checks a held row older than this.
RECHECK_AGE_SECONDS = 3600
MAINTENANCE_INTERVAL_SECONDS = 900
MAINTENANCE_BATCH = 200
MAINTENANCE_CONCURRENCY = 4
# Upper bound of one DNS check (both lookups), enforced by the caller.
CHECK_TIMEOUT_SECONDS = 8
# Owner action limits per workspace (Redis counters).
VERIFY_LIMIT_PER_HOUR = 20
ACTIVATIONS_PER_DAY = 6

# The states in which a row holds its host name.
HELD_STATES: tuple[str, ...] = ("active", "suspended")

_DB_NOW = "timezone('UTC', statement_timestamp())"


class PublicMcpDomain(Base):
    """One host name bound to one public MCP endpoint publication.

    Attributes:
        id: The ``domain_id`` of the API, logs and audit.
        hostname: Normalised host name (``utils.public_hostname``).
        workspace_id: Owning workspace (no foreign key).
        endpoint_id: The bound publication (``public_mcp_endpoints.id``),
            never changed.
        state: ``pending``, ``active``, ``suspended`` or ``released``.
        suspended_reason: ``proof_lost``, ``right_ended`` or ``reserved``;
            set exactly while ``suspended``.
        verification_token: Random token of the TXT proof; one per row.
        serve_until: The lease; an ``active`` row is served only before it.
        check_seq: Incremented by every UPDATE; the fence of a lookup result.
        last_proof_result: ``ok``, ``missing`` or ``unavailable``.
        routing_status: ``ok``, ``mismatch`` or ``unresolved`` (informational).
        proof_ok_at: The last time a check found the proof.
        proof_missing_checks: Confirmed absences since ``proof_ok_at``.
        created_at: Claim time.
        verified_at: Activation time.
        last_checked_at: Last completed check.
        suspended_at: Start of the current suspension.
        released_at: Release time.
        cooldown_until: Until when another workspace cannot take the name.
    """

    __tablename__ = "public_mcp_domains"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'active', 'suspended', 'released')",
            name="ck_public_mcp_domains_state",
        ),
        CheckConstraint(
            "suspended_reason IN ('proof_lost', 'right_ended', 'reserved')",
            name="ck_public_mcp_domains_suspended_reason",
        ),
        CheckConstraint(
            "(state = 'suspended') = (suspended_reason IS NOT NULL)",
            name="ck_public_mcp_domains_suspended",
        ),
        CheckConstraint(
            "(state = 'released') = (released_at IS NOT NULL AND cooldown_until IS NOT NULL)",
            name="ck_public_mcp_domains_released",
        ),
        CheckConstraint(
            "state <> 'active' OR (serve_until IS NOT NULL AND verified_at IS NOT NULL "
            "AND proof_ok_at IS NOT NULL)",
            name="ck_public_mcp_domains_active_lease",
        ),
        CheckConstraint(
            "hostname = lower(hostname) AND hostname NOT LIKE '%.' "
            "AND char_length(hostname) BETWEEN 4 AND 234",
            name="ck_public_mcp_domains_hostname",
        ),
        CheckConstraint(
            "last_proof_result IN ('ok', 'missing', 'unavailable')",
            name="ck_public_mcp_domains_last_proof_result",
        ),
        CheckConstraint(
            "routing_status IN ('ok', 'mismatch', 'unresolved')",
            name="ck_public_mcp_domains_routing_status",
        ),
        # One holder per host name across all workspaces.
        Index(
            "ux_public_mcp_domains_hostname_held",
            "hostname",
            unique=True,
            postgresql_where=text("state IN ('active', 'suspended')"),
        ),
        # One open domain per publication.
        Index(
            "ux_public_mcp_domains_endpoint_open",
            "endpoint_id",
            unique=True,
            postgresql_where=text("state <> 'released'"),
        ),
        Index("ix_public_mcp_domains_hostname", "hostname"),
        Index("ix_public_mcp_domains_workspace_id", "workspace_id"),
        Index(
            "ix_public_mcp_domains_due",
            "last_checked_at",
            postgresql_where=text("state IN ('active', 'suspended')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    tls_report: Mapped[dict | None] = mapped_column(NullableJSONB, nullable=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    suspended_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verification_token: Mapped[str] = mapped_column(String(64), nullable=False)
    serve_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    check_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_proof_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    routing_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    proof_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    proof_missing_checks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text(_DB_NOW)
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
