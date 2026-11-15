"""Durable feed ownership, bounded resume snapshots and exact human confirmations."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.json_types import NullableJSONB
from utils.datetime import utcnow


class SyncManagedTarget(Base):
    """Editing stays fenced through stopping; authorization is a separate lifecycle."""

    __tablename__ = "sync_managed_targets"
    __table_args__ = (
        CheckConstraint("mode IN ('feed', 'stopping', 'manual')", name="ck_sync_managed_mode"),
        CheckConstraint("epoch > 0 AND generation > 0", name="ck_sync_managed_generation"),
        CheckConstraint("event_floor >= 0 AND stop_event_id >= 0", name="ck_sync_managed_offsets"),
    )
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_installations.id", ondelete="CASCADE"), primary_key=True
    )
    # Match the installation's permanent binding: physical target loss must
    # not erase its ownership fence or authorize a recovery to rebind it.
    resource_pk: Mapped[UUID] = mapped_column(Uuid(), unique=True)
    context_id: Mapped[UUID] = mapped_column(Uuid(), unique=True)
    mode: Mapped[str] = mapped_column(String(16), default="feed")
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    generation: Mapped[int] = mapped_column(Integer)
    event_floor: Mapped[int] = mapped_column(BigInteger, default=0)
    stop_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    drain_generation: Mapped[int | None] = mapped_column(Integer)
    stop_reason: Mapped[str | None] = mapped_column(String(40))
    manual_sleep_mode: Mapped[str] = mapped_column(String(16), default="full")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncResumeCandidate(Base):
    """A pinned strict source snapshot; its closed upload no longer consumes a run slot."""

    __tablename__ = "sync_resume_candidates"
    __table_args__ = (
        CheckConstraint(
            "record_count >= 0 AND record_count <= 10000", name="ck_sync_candidate_count"
        ),
        CheckConstraint(
            "byte_count > 0 AND byte_count <= 20971520", name="ck_sync_candidate_bytes"
        ),
        CheckConstraint("length(snapshot_hash) = 64", name="ck_sync_candidate_hash"),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '24 hours'",
            name="ck_sync_candidate_expiry",
        ),
        Index(
            "uq_sync_candidate_active_feed", "feed_id", unique=True, postgresql_where=text("active")
        ),
    )
    id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True
    )
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_installations.id", ondelete="CASCADE"), index=True
    )
    feed_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_feeds.id", ondelete="CASCADE"), index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    epoch: Mapped[int] = mapped_column(Integer)
    generations: Mapped[list[int]] = mapped_column(JSONB)
    manifest: Mapped[dict] = mapped_column(JSONB)
    records: Mapped[list[dict] | None] = mapped_column(NullableJSONB)
    record_count: Mapped[int] = mapped_column(Integer)
    byte_count: Mapped[int] = mapped_column(Integer)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class SyncResumePreview(Base):
    """One replaceable, finite human diff per installation; never reusable authority."""

    __tablename__ = "sync_resume_previews"
    __table_args__ = (
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '15 minutes'",
            name="ck_sync_preview_expiry",
        ),
    )
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_installations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(Uuid(), unique=True, default=uuid4)
    approved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL"), index=True
    )
    ownership_epoch: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    manual_hash: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    diff_hash: Mapped[str] = mapped_column(String(64))
    totals: Mapped[dict] = mapped_column(JSONB)
    diff: Mapped[list[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class SyncModeOperation(Base):
    """Bounded control results; duplicate commands never advance generation twice."""

    __tablename__ = "sync_mode_operations"
    __table_args__ = (
        CheckConstraint(
            "action IN ('stop', 'finalize', 'resume', 'reconnect')",
            name="ck_sync_mode_operation_action",
        ),
    )
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_installations.id", ondelete="CASCADE"), primary_key=True
    )
    operation_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    action: Mapped[str] = mapped_column(String(32))
    request_hash: Mapped[str] = mapped_column(String(64))
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL"), index=True
    )
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncManualRetirement(Base):
    """Only exact human-approved tombstones may be retired by later feed indexing."""

    __tablename__ = "sync_manual_retirements"
    __table_args__ = (
        CheckConstraint(
            "generation > 0 AND memory_version > 0 AND length(content_hash) = 64",
            name="ck_sync_retirement_proof",
        ),
    )
    installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("sync_installations.id", ondelete="CASCADE"), primary_key=True
    )
    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    memory_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    deleted_at: Mapped[datetime] = mapped_column(DateTime)
    approved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL"), index=True
    )
