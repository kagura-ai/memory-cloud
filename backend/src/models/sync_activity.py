"""Finite, server-authenticated provenance for a human-requested service run."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from utils.datetime import utcnow


class SyncManualRequest(Base):
    """An attribution receipt, never worker authority or a transferable human token."""

    __tablename__ = "sync_manual_requests"
    __table_args__ = (
        UniqueConstraint(
            "installation_id", "operation_id", name="uq_sync_manual_request_operation"
        ),
        CheckConstraint(
            "generation > 0 AND ownership_epoch >= 0", name="ck_sync_manual_request_epoch"
        ),
        CheckConstraint("length(source_version_hash) = 64", name="ck_sync_manual_request_source"),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '15 minutes'",
            name="ck_sync_manual_request_expiry",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_installations.id", ondelete="CASCADE"), index=True
    )
    operation_id: Mapped[UUID] = mapped_column(Uuid())
    requested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL"), index=True
    )
    token_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("oidc_tokens.id", ondelete="SET NULL"), index=True
    )
    browser_client_id: Mapped[str] = mapped_column(String(255))
    source_version_hash: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    ownership_epoch: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class SyncManualRun(Base):
    """One feed run per authenticated request without rewriting the existing run table."""

    __tablename__ = "sync_manual_runs"
    __table_args__ = (UniqueConstraint("request_id", "feed_id", name="uq_sync_manual_run_feed"),)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True
    )
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sync_manual_requests.id", ondelete="SET NULL"), index=True
    )
    feed_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_feeds.id", ondelete="CASCADE"), index=True
    )


SyncActivityAction = Literal[
    "sync.service.registered",
    "sync.service.enabled",
    "sync.service.disabled",
    "sync.authority.denied",
    "sync.authorization.created",
    "sync.authorization.revoked",
    "sync.authorization.superseded",
    "sync.setup_handoff.created",
    "sync.client.disabled",
    "sync.client.enabled",
    "sync.client.registered",
    "sync.diff.approved",
    "sync.feed.configured",
    "sync.installation.activated",
    "sync.installation.reconnected",
    "sync.installation.revoked",
    "sync.key.added",
    "sync.key.revoked",
    "sync.management.manual",
    "sync.management.resumed",
    "sync.management.stopping",
    "sync.request.created",
    "sync.resume.candidate.created",
    "sync.run.aborted",
    "sync.run.accepted",
    "sync.run.committed",
    "sync.run.prepared",
    "sync.schema.upgraded",
    "sync.token.issued",
]
