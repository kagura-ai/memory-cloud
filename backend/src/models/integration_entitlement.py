"""Billing-owned finite rights for workspace service integrations (#2798)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from utils.datetime import utcnow


class WorkspaceIntegrationState(Base):
    """Latest complete billing snapshot; never a payment subscription record."""

    __tablename__ = "workspace_integration_states"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_integration_state_revision"),
        CheckConstraint("source = 'external_billing'", name="ck_integration_state_source"),
        CheckConstraint(
            "(max_valid_from IS NULL AND max_valid_until IS NULL AND max_evidence_ref IS NULL) OR "
            "(max_valid_from IS NOT NULL AND max_valid_until IS NOT NULL AND "
            "max_evidence_ref IS NOT NULL AND max_valid_until > max_valid_from)",
            name="ck_integration_state_max_window",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="external_billing")
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    max_valid_from: Mapped[datetime | None] = mapped_column(DateTime)
    max_valid_until: Mapped[datetime | None] = mapped_column(DateTime)
    max_evidence_ref: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class WorkspaceIntegrationLicense(Base):
    """One named service's granted interval; expiry never deletes content."""

    __tablename__ = "workspace_integration_licenses"
    __table_args__ = (
        CheckConstraint("valid_until > valid_from", name="ck_integration_license_window"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspace_integration_states.workspace_id", ondelete="CASCADE"),
        primary_key=True,
    )
    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(128), nullable=False)


class WorkspaceIntegrationEvent(Base):
    """Immutable applied-delivery receipt for retry and revision conflict detection."""

    __tablename__ = "workspace_integration_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "revision", name="uq_integration_event_revision"),
        CheckConstraint("revision > 0", name="ck_integration_event_revision"),
        CheckConstraint("length(payload_digest) = 64", name="ck_integration_event_digest"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspace_integration_states.workspace_id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
