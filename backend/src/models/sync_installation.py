"""Workspace-owned synchronization control records, never worker credentials."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from utils.datetime import utcnow

SYNC_OPERATIONS = ("schema:read", "records:write", "results:read")


class SyncService(Base):
    """Operator-registered immutable client binding, with a revocation generation."""

    __tablename__ = "sync_services"
    __table_args__ = (CheckConstraint("generation > 0", name="ck_sync_service_generation"),)

    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_client_id: Mapped[str] = mapped_column(String(128), unique=True)
    service_client_id: Mapped[str] = mapped_column(String(128), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncAuthorization(Base):
    """Finite explicit consent. Deleting an account clears its historical identity."""

    __tablename__ = "sync_authorizations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "request_id", name="uq_sync_authorization_request"),
        CheckConstraint("ownership_epoch >= 0", name="ck_sync_authorization_epoch"),
        CheckConstraint("service_generation > 0", name="ck_sync_authorization_service_generation"),
        CheckConstraint("expires_at > created_at", name="ck_sync_authorization_window"),
        CheckConstraint("length(request_hash) = 64", name="ck_sync_authorization_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[UUID] = mapped_column(Uuid())
    application_id: Mapped[str] = mapped_column(String(128), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("sync_services.service_id"))
    service_generation: Mapped[int] = mapped_column(Integer)
    browser_client_id: Mapped[str] = mapped_column(String(128))
    service_client_id: Mapped[str] = mapped_column(String(128))
    operations: Mapped[list[str]] = mapped_column(JSONB)
    authorized_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL")
    )
    ownership_epoch: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    public_name: Mapped[str] = mapped_column(String(200))
    public_description: Mapped[str | None] = mapped_column(Text)
    public_usage_guide: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class SyncInstallation(Base):
    """One permanent binding per workspace, including deleted-target tombstones.

    Target UUIDs and authorization IDs intentionally do not cascade from their
    targets. Losing a target or consent history must never free a binding for
    automatic replacement. Every consumer must recheck all target identities.
    """

    __tablename__ = "sync_installations"
    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_sync_installation_generation"),
        CheckConstraint(
            "state IN ('active', 'paused', 'revoked')", name="ck_sync_installation_state"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), unique=True
    )
    service_id: Mapped[str] = mapped_column(ForeignKey("sync_services.service_id"))
    service_client_id: Mapped[str] = mapped_column(String(128))
    authorization_id: Mapped[UUID] = mapped_column(Uuid(), unique=True)
    application_id: Mapped[str] = mapped_column(String(128))
    operations: Mapped[list[str]] = mapped_column(JSONB)
    resource_pk: Mapped[UUID] = mapped_column(Uuid())
    context_id: Mapped[UUID] = mapped_column(Uuid())
    endpoint_id: Mapped[UUID] = mapped_column(Uuid())
    resource_id: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16), default="active")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
