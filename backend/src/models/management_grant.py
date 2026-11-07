"""Explicit, expiring delegation to a single public resource incarnation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ManagementGrant(Base):
    """Immutable scope; only the verifier is stored, never a bearer secret."""

    __tablename__ = "resource_management_grants"
    __table_args__ = (
        CheckConstraint(
            "quota_events_per_hour BETWEEN 1 AND 10000", name="ck_management_grant_quota"
        ),
        CheckConstraint(
            "max_active_tokens BETWEEN 1 AND 100", name="ck_management_grant_token_cap"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    resource_pk: Mapped[uuid.UUID] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"))
    context_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contexts.id", ondelete="CASCADE"))
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("public_mcp_endpoints.id", ondelete="CASCADE")
    )
    deployment_id: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str] = mapped_column(String(255))
    operations: Mapped[list[str]] = mapped_column(JSONB)
    approved_fields: Mapped[list[str]] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(Integer)
    schema_hash: Mapped[str] = mapped_column(String(64))
    quota_events_per_hour: Mapped[int] = mapped_column(Integer)
    max_active_tokens: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ManagementOperation(Base):
    """Idempotent outcome and audit metadata; never payloads or bearer secrets."""

    __tablename__ = "resource_management_operations"

    grant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_management_grants.id", ondelete="CASCADE"), primary_key=True
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    action: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
